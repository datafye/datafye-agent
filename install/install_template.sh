#!/bin/bash

# Copyright 2025 Datafye
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# Datafye Agent - Installer / Upgrader
#
# Installs or upgrades the Datafye Agent natively on a Linux machine.
# The agent runs as a systemd service (Python + FastAPI) directly on the host.
# Docker is installed for Datafye environment containers (managed by the agent).
#
# Two modes:
#   hosted     - For Rumi cloud sandbox instances (no nginx, no SSL, proxied by jump server)
#   standalone - For marketplace/DIY instances (includes nginx + optional SSL)
#
# Usage (version is baked in by publish_installer.sh):
#   # Hosted mode (sandbox in Rumi cloud)
#   sudo ./install.sh --mode hosted
#   sudo ./install.sh --mode hosted --anthropic-key sk-ant-...
#
#   # Standalone mode (marketplace/DIY)
#   sudo ./install.sh --mode standalone --dns agent.mycompany.com --anthropic-key sk-ant-...
#
#   # Upgrade (auto-upgrade downloads latest installer with new version baked in)
#   # Credentials, mode, and workspace are preserved automatically
#
#   # Force reinstall same version (useful for SNAPSHOT builds)
#   sudo ./install.sh --mode hosted --force
#
#   # Build hosted AMI (install + cleanup for snapshot)
#   sudo ./install.sh --mode hosted --ami-cleanup
#
#   # Pin to a specific released version (non-SNAPSHOT)
#   sudo ./install.sh --mode hosted --version 2.0.5
#
#   # Install a SNAPSHOT build (internal testing). Requires a GitHub token with
#   # read access to the private datafye-docs repo, and a locally-installed
#   # Datafye CLI matching the SNAPSHOT version.
#   sudo ./install.sh --mode hosted --version 2.0-SNAPSHOT --github-token ghp_...
#

set -e

# Default TMPDIR to /var/tmp (disk-backed) so any downloads + extracts in
# this installer AND in the Datafye CLI installer this one curl-pipes
# don't get stuck on the tmpfs /tmp. AL2023 mounts /tmp as tmpfs capped
# at ~50% of memory — too small for the Datafye CLI distribution tarball
# + its extracted libs/ on small instances. Caller-supplied TMPDIR wins.
export TMPDIR="${TMPDIR:-/var/tmp}"
mkdir -p "$TMPDIR"

# ── Defaults ──────────────────────────────────────────────────────
VERSION="__VERSION__"
VERSION_EXPLICIT=false
MODE=""
DNS_NAME=""
ANTHROPIC_API_KEY=""
GITHUB_TOKEN=""
FORCE=false
AMI_CLEANUP=false
AGENT_PORT=18780
INSTALL_DIR="/opt/datafye/agent"
WORKSPACE_DIR="/home/datafye/workspace"
DOCS_DIR="/opt/datafye/docs"
SAMPLES_DIR="/opt/datafye/samples"
CLI_BASE="/usr/local/opt/datafye/cli"
VENV_DIR="/opt/datafye/agent/venv"
AGENT_REPO="https://github.com/datafye/datafye-agent.git"
SAMPLES_REPO="https://github.com/datafye/datafye-samples.git"
DOCS_REPO="https://github.com/datafye/datafye-docs.git"
DOCS_DOWNLOAD_BASE="https://downloads.n5corp.com/datafye/docs"

# ── Colors ────────────────────────────────────────────────────────
RED="\033[0;31m"; GREEN="\033[0;32m"; YELLOW="\033[1;33m"; CYAN="\033[0;36m"; RESET="\033[0m"
info()  { echo -e "${CYAN}$*${RESET}"; }
warn()  { echo -e "${YELLOW}$*${RESET}"; }
ok()    { echo -e "${GREEN}  ok: $*${RESET}"; }
error() { echo -e "${RED}ERROR: $*${RESET}" >&2; }

# ── Parse arguments ───────────────────────────────────────────────
AGENT_SOURCE_DIR=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode)           MODE="$2"; shift 2 ;;
        --dns)            DNS_NAME="$2"; shift 2 ;;
        --anthropic-key)  ANTHROPIC_API_KEY="$2"; shift 2 ;;
        --version)        VERSION="$2"; VERSION_EXPLICIT=true; shift 2 ;;
        --github-token)   GITHUB_TOKEN="$2"; shift 2 ;;
        --agent-source)   AGENT_SOURCE_DIR="$2"; shift 2 ;;
        --force)          FORCE=true; shift ;;
        --ami-cleanup)    AMI_CLEANUP=true; shift ;;
        --port)           AGENT_PORT="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Datafye Agent Installer

Usage:
  install.sh --mode <hosted|standalone> [OPTIONS]

Options:
  --mode <mode>         Installation mode (required for fresh install):
                          hosted     - Rumi cloud sandbox (no nginx, no SSL)
                          standalone - Marketplace/DIY (nginx + SSL)
  --dns <name>          DNS name (standalone mode, e.g., agent.mycompany.com)
  --anthropic-key <k>   Anthropic API key (can be set later or via EC2 user data)
  --version <v>         Override the baked-in version. Accepts X.Y.Z for
                        released builds or X.Y-SNAPSHOT for internal testing.
                        Passing --version pins the install; auto-upgrade is
                        disabled until the pin is cleared.
  --github-token <t>    GitHub token with read access to datafye-docs.
                        Required for SNAPSHOT installs (docs repo is private).
  --agent-source <dir>  Skip the agent-source git clone and seed the agent
                        directory from a local checkout. Intended for the
                        AMI bake, where the build commit isn't yet tagged on
                        the remote. The local checkout's remote URL is
                        rewritten to the canonical AGENT_REPO so auto-upgrade
                        keeps working.
  --force               Reinstall even if same version (useful for SNAPSHOT)
  --ami-cleanup         Clean up for AMI snapshot (clear keys, logs, history)
  --port <port>         Agent port (default: 18780)
  -h, --help            Show this help
EOF
            exit 0
            ;;
        *)  error "Unknown option: $1"; exit 1 ;;
    esac
done

# Note: sentinel is split so sed's __VERSION__ substitution doesn't replace it
if [ "$VERSION" = "__""VERSION__" ]; then
    error "This is the installer template. Use the published installer from downloads.n5corp.com,"
    error "pass --version to override the baked-in value, or run publish_installer.sh to create"
    error "a versioned installer."
    exit 1
fi

# ── Check root ────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    error "This installer must be run as root (sudo)"
    exit 1
fi

# ── SNAPSHOT handling ─────────────────────────────────────────────
is_snapshot() { [[ "$1" == *"-SNAPSHOT"* ]]; }

# Resolve git refs (tags for releases, branches for SNAPSHOTs) and validate
# SNAPSHOT prerequisites up front so we fail fast.
if is_snapshot "$VERSION"; then
    if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+-SNAPSHOT$ ]]; then
        error "SNAPSHOT version must be X.Y-SNAPSHOT (e.g., 2.0-SNAPSHOT). Got: $VERSION"
        exit 1
    fi
    if [ -z "$GITHUB_TOKEN" ]; then
        error "SNAPSHOT installs require --github-token (private datafye-docs access)."
        exit 1
    fi
    SNAPSHOT_BRANCH="${VERSION%-SNAPSHOT}"
    DOCS_REF="${SNAPSHOT_BRANCH}"
    AGENT_REF="${SNAPSHOT_BRANCH}"
    SAMPLES_REF="${SNAPSHOT_BRANCH}"
    DOCS_CLONE_URL="https://${GITHUB_TOKEN}@github.com/datafye/datafye-docs.git"
else
    DOCS_REF="${VERSION}"
    AGENT_REF="${VERSION}"
    SAMPLES_REF="${VERSION}"
    DOCS_CLONE_URL=""   # not used for released versions (docs come from the downloads tarball)
fi

# datafye-agent is currently a private repo. Build a token-embedded clone URL
# so authenticated clones work; falls back to the anonymous URL once the repo
# is made public (token-embedded form is harmless for public repos).
if [ -n "${GITHUB_TOKEN}" ]; then
    AGENT_CLONE_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/datafye/datafye-agent.git"
else
    AGENT_CLONE_URL="${AGENT_REPO}"
fi

# ── Detect existing installation ──────────────────────────────────
CURRENT_VERSION=""
IS_UPGRADE=false
ENV_FILE="${INSTALL_DIR}/agent.env"

if [ -f "${INSTALL_DIR}/version" ]; then
    CURRENT_VERSION=$(cat "${INSTALL_DIR}/version")

    if [ "${CURRENT_VERSION}" = "${VERSION}" ] && [ "$FORCE" = false ] && ! is_snapshot "$VERSION"; then
        info "Datafye Agent v${VERSION} is already installed."
        info "Use --force to reinstall, or specify a different --version."
        exit 0
    fi

    IS_UPGRADE=true
fi

# ── Preserve existing config on upgrade ───────────────────────────
if [ "$IS_UPGRADE" = true ] && [ -f "${ENV_FILE}" ]; then
    info "Preserving existing configuration..."
    if [ -z "$ANTHROPIC_API_KEY" ]; then
        ANTHROPIC_API_KEY=$(grep -oP '^DATAFYE_AGENT_ANTHROPIC_API_KEY=\K.*' "${ENV_FILE}" || true)
    fi
    EXISTING_PORT=$(grep -oP '^DATAFYE_AGENT_PORT=\K.*' "${ENV_FILE}" || true)
    if [ -n "$EXISTING_PORT" ]; then
        AGENT_PORT="${EXISTING_PORT}"
    fi
    # Preserve mode from existing config
    if [ -z "$MODE" ]; then
        MODE=$(grep -oP '^DATAFYE_AGENT_MODE=\K.*' "${ENV_FILE}" || true)
    fi
    # Preserve DNS from existing config
    if [ -z "$DNS_NAME" ]; then
        DNS_NAME=$(grep -oP '^DATAFYE_AGENT_DNS=\K.*' "${ENV_FILE}" || true)
    fi
fi

# ── Validate mode ────────────────────────────────────────────────
if [ -z "$MODE" ]; then
    error "--mode is required for fresh install (hosted or standalone)"
    exit 1
fi

if [ "$MODE" != "hosted" ] && [ "$MODE" != "standalone" ]; then
    error "Invalid mode: $MODE. Must be 'hosted' or 'standalone'."
    exit 1
fi

# ── Banner ────────────────────────────────────────────────────────
if [ "$IS_UPGRADE" = true ]; then
    echo ""
    info "================================================"
    info "  Datafye Agent Upgrade: ${CURRENT_VERSION} -> ${VERSION}"
    info "  Mode: ${MODE}"
    info "================================================"
else
    echo ""
    info "================================================"
    info "  Datafye Agent Install: v${VERSION}"
    info "  Mode: ${MODE}"
    info "================================================"
fi
echo ""

# ── Detect platform ──────────────────────────────────────────────
detect_platform() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

PLATFORM=$(detect_platform)
TOTAL_STEPS=11
[ "$MODE" = "standalone" ] && TOTAL_STEPS=13
STEP=0

next_step() { STEP=$((STEP + 1)); }

# ── Step: Install system dependencies ────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing system dependencies..."

# PYTHON_BIN is the interpreter used to create the agent's venv. The agent
# requires Python >= 3.10 (claude-agent-sdk constraint). AL2023's default
# python3 is 3.9, so install python3.11 there and pin PYTHON_BIN to it.
PYTHON_BIN="python3"
case $PLATFORM in
    amzn)
        # --allowerasing lets dnf swap curl-minimal (which AL2023 ships) for the
        # full curl package without aborting on the conflict. On AL2 (which has
        # full curl already), --allowerasing is a no-op.
        yum install -y --allowerasing python3.11 python3.11-pip git curl java-17-amazon-corretto-headless
        PYTHON_BIN="python3.11"
        ;;
    ubuntu|debian)
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv git curl openjdk-17-jre-headless
        ;;
    rhel|centos|fedora|rocky|almalinux)
        # See note above on --allowerasing.
        yum install -y --allowerasing python3.11 python3.11-pip git curl java-17-openjdk-headless
        PYTHON_BIN="python3.11"
        ;;
    *)
        error "Unsupported platform: ${PLATFORM}"
        exit 1
        ;;
esac

# Maven (needed to build samples)
if ! command -v mvn &> /dev/null; then
    info "Installing Maven..."
    MAVEN_VERSION="3.9.6"
    curl -fsSL "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
        | tar -xz -C /opt
    ln -sf "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" /usr/local/bin/mvn
fi

ok "Python: $(${PYTHON_BIN} --version) (${PYTHON_BIN})"
ok "Java: $(java -version 2>&1 | head -1)"
ok "Maven: $(mvn --version 2>/dev/null | head -1)"
ok "Git: $(git --version)"

# ── Step: Install Docker (for Datafye environment containers) ────
install_docker() {
    if command -v docker &> /dev/null; then
        if docker info &>/dev/null; then
            ok "Docker: $(docker --version)"
            return
        fi
        systemctl start docker 2>/dev/null || true
        if docker info &>/dev/null; then
            ok "Docker daemon started"
            return
        fi
    fi

    info "Installing Docker..."
    case $PLATFORM in
        amzn)
            yum install -y docker
            ;;
        ubuntu|debian)
            apt-get install -y -qq docker.io
            ;;
        rhel|centos|fedora|rocky|almalinux)
            yum install -y yum-utils
            yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
            yum install -y docker-ce docker-ce-cli containerd.io
            ;;
    esac

    systemctl enable docker
    systemctl start docker
    ok "Docker installed: $(docker --version)"
}

# AL2023's docker package doesn't bundle the Compose v2 plugin, so install it
# directly from the docker/compose release for the host architecture. The CLI
# uses 'docker compose' for foundry local provisioning.
install_docker_compose() {
    if docker compose version &>/dev/null; then
        ok "Docker Compose: $(docker compose version | head -1)"
        return
    fi
    local version="v2.27.0"
    local arch
    arch=$(uname -m)
    local plugin_dir="/usr/libexec/docker/cli-plugins"
    info "Installing Docker Compose plugin ${version} (${arch})..."
    mkdir -p "${plugin_dir}"
    curl -fsSL --retry 3 \
        "https://github.com/docker/compose/releases/download/${version}/docker-compose-linux-${arch}" \
        -o "${plugin_dir}/docker-compose"
    chmod +x "${plugin_dir}/docker-compose"
    ok "Docker Compose: $(docker compose version | head -1)"
}

next_step
info "[${STEP}/${TOTAL_STEPS}] Docker (for Datafye environment containers)..."
install_docker
install_docker_compose

# ── Step: Create directories and user ────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Directories and user..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${DOCS_DIR}"
mkdir -p "${SAMPLES_DIR}"

if ! id -u datafye &>/dev/null; then
    useradd -u 1000 -m -d /home/datafye -s /bin/bash datafye
fi
# Create the workspace AFTER useradd so it's owned by datafye outright;
# also force-chown /home/datafye in case an earlier install pass (or any
# other step that mkdir's a path under it) had created the home dir as
# root, which makes useradd -m skip the chown and leaves the home tree
# unwritable by the datafye runtime user (Rumi CLI's local provisioner
# fails to mkdir /home/datafye/.rumi when this happens).
mkdir -p "${WORKSPACE_DIR}"
chown datafye:datafye /home/datafye
chown -R datafye:datafye "${WORKSPACE_DIR}"
# Allow datafye user to run docker
usermod -aG docker datafye 2>/dev/null || true
ok "Workspace: ${WORKSPACE_DIR}"

# ── Step: Stop existing service on upgrade ───────────────────────
next_step
if [ "$IS_UPGRADE" = true ]; then
    info "[${STEP}/${TOTAL_STEPS}] Stopping existing agent..."
    systemctl stop datafye-agent 2>/dev/null || true
    ok "Agent service stopped"
else
    info "[${STEP}/${TOTAL_STEPS}] Fresh install (no existing service)"
fi

# ── Step: Install Claude Code CLI ────────────────────────────────
# claude.ai/install.sh always lays files under the invoking user's
# ~/.local (binary + supporting state), and the binary resolves its
# install root at runtime — so a root install + /usr/local/bin symlink
# would not work. Install as the datafye user (the runtime user) so
# claude lands at /home/datafye/.local/{bin,share}/claude.
CLAUDE_BIN="/home/datafye/.local/bin/claude"
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing Claude Code CLI (as datafye user)..."
if [ -x "${CLAUDE_BIN}" ]; then
    ok "Claude Code CLI already installed: ${CLAUDE_BIN}"
else
    sudo -u datafye -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
    if [ ! -x "${CLAUDE_BIN}" ]; then
        error "Claude CLI not found at ${CLAUDE_BIN} after install"
        exit 1
    fi
    ok "Claude Code CLI: ${CLAUDE_BIN}"
fi

# ── Step: Install / validate Datafye CLI ─────────────────────────
next_step
if is_snapshot "$VERSION"; then
    info "[${STEP}/${TOTAL_STEPS}] Validating local Datafye CLI (SNAPSHOT mode)..."
    if ! command -v datafye &>/dev/null; then
        error "Datafye CLI not found on PATH. SNAPSHOT installs require a locally-installed"
        error "CLI matching version ${VERSION}."
        exit 1
    fi
    INSTALLED_CLI_VERSION=$(datafye version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?(-[A-Za-z0-9.-]+)?' | head -1 || true)
    if [ "$INSTALLED_CLI_VERSION" != "$VERSION" ]; then
        error "Local Datafye CLI version '${INSTALLED_CLI_VERSION}' does not match requested"
        error "SNAPSHOT '${VERSION}'. Install or update the local CLI first."
        exit 1
    fi
    CLI_PATH=$(command -v datafye)
    ok "Using local Datafye CLI: ${CLI_PATH} (v${INSTALLED_CLI_VERSION})"
else
    info "[${STEP}/${TOTAL_STEPS}] Installing Datafye CLI v${VERSION}..."
    curl -fsSL "https://downloads.n5corp.com/datafye/cli/${VERSION}/install.sh" | bash
    # The CLI installer drops files at ${CLI_BASE}/versions/datafye-cli-<v>/
    # and maintains a ${CLI_BASE}/current symlink to the active version.
    # Use the stable symlink so we don't have to track the bundle-name format.
    CLI_PATH="${CLI_BASE}/current/bin/datafye"
    if [ ! -x "${CLI_PATH}" ]; then
        error "Datafye CLI not found at ${CLI_PATH} after install"
        exit 1
    fi
    ok "Datafye CLI: ${CLI_PATH} -> $(readlink -f "${CLI_PATH}")"
fi

# ── Step: Install/update docs, samples, and agent source ─────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing docs, samples, and agent source..."

clone_or_update_repo() {
    local repo_url="$1"
    local target_dir="$2"
    local git_ref="$3"
    local label="$4"

    if [ -d "${target_dir}/.git" ]; then
        cd "${target_dir}"
        git remote set-url origin "${repo_url}"
        git fetch --depth 1 origin "${git_ref}" \
            || { error "${label}: failed to fetch ${git_ref}"; exit 1; }
        git checkout -qf FETCH_HEAD
        cd - > /dev/null
    else
        rm -rf "${target_dir}"
        git clone --depth 1 --branch "${git_ref}" "${repo_url}" "${target_dir}" \
            || { error "${label}: failed to clone ${git_ref}"; exit 1; }
    fi
    ok "${label}: ${target_dir}"
}

fetch_docs_tarball() {
    local url="$1"
    local target_dir="$2"
    local label="$3"

    info "Fetching docs tarball from ${url}..."
    rm -rf "${target_dir}"
    mkdir -p "${target_dir}"
    curl -fsSL --retry 3 "${url}" | tar -xz -C "${target_dir}" --strip-components=1 \
        || { error "${label}: failed to fetch or extract tarball"; exit 1; }
    ok "${label}: ${target_dir}"
}

if is_snapshot "$VERSION"; then
    clone_or_update_repo "${DOCS_CLONE_URL}" "${DOCS_DIR}" "${DOCS_REF}" "Docs"
else
    fetch_docs_tarball "${DOCS_DOWNLOAD_BASE}/${VERSION}/docs.tar.gz" "${DOCS_DIR}" "Docs"
fi

clone_or_update_repo "${SAMPLES_REPO}" "${SAMPLES_DIR}" "${SAMPLES_REF}" "Samples"

AGENT_CODE_DIR="${INSTALL_DIR}/app"
if [ -n "${AGENT_SOURCE_DIR}" ]; then
    # AMI-bake path: the build commit isn't tagged on origin yet, so seed
    # the agent directory from a local checkout. Rewrite the origin URL so
    # post-install upgrades fetch from the canonical remote.
    if [ ! -d "${AGENT_SOURCE_DIR}/.git" ]; then
        error "Agent: --agent-source path is not a git repo: ${AGENT_SOURCE_DIR}"
        exit 1
    fi
    info "Seeding agent source from local checkout: ${AGENT_SOURCE_DIR}"
    rm -rf "${AGENT_CODE_DIR}"
    mkdir -p "$(dirname "${AGENT_CODE_DIR}")"
    cp -a "${AGENT_SOURCE_DIR}" "${AGENT_CODE_DIR}"
    git -C "${AGENT_CODE_DIR}" remote set-url origin "${AGENT_REPO}"
    ok "Agent: ${AGENT_CODE_DIR} (from ${AGENT_SOURCE_DIR}, $(git -C "${AGENT_CODE_DIR}" rev-parse --short HEAD))"
else
    clone_or_update_repo "${AGENT_CLONE_URL}" "${AGENT_CODE_DIR}" "${AGENT_REF}" "Agent"
fi

# ── Step: Install Python dependencies ────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing Python dependencies..."

if [ ! -d "${VENV_DIR}" ]; then
    ${PYTHON_BIN} -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -r "${AGENT_CODE_DIR}/requirements.txt" -q
ok "Python dependencies installed"

# ── Step: Configure /etc/hosts for Datafye local environment ─────
next_step
info "[${STEP}/${TOTAL_STEPS}] Configuring /etc/hosts for Datafye local environment..."

HOSTS_MARKER_START="# BEGIN datafye-agent (managed)"
HOSTS_MARKER_END="# END datafye-agent"

# Strip any existing block (idempotent across re-installs)
sed -i "/${HOSTS_MARKER_START}/,/${HOSTS_MARKER_END}/d" /etc/hosts
# Remove any trailing blank lines the sed may have left behind
sed -i -e :a -e '/^\s*$/{$d;N;ba' -e '}' /etc/hosts

# Append the managed block
cat >> /etc/hosts <<EOF

${HOSTS_MARKER_START}
127.0.0.1   local-foundry-dev-api.datafye.local
127.0.0.1   local-foundry-dev-admin.datafye.local
127.0.0.1   local-foundry-dev-monitor.datafye.local
127.0.0.1   local-foundry-dev-mcp-api.datafye.local
${HOSTS_MARKER_END}
EOF
ok "/etc/hosts configured (datafye.local hostnames → 127.0.0.1)"

# ── Step: Provision / upgrade local Datafye foundry environment ──
# Skip in --ami-cleanup mode: foundry provisioning pulls docker images,
# starts containers, and writes instance-specific state under ~/.rumi
# (admin-docker-compose.yml, named volumes, etc.) — none of which is
# safe to snapshot into an AMI. Each per-user sandbox provisions its
# own foundry at first boot.
next_step
if [ "$AMI_CLEANUP" = true ]; then
    info "[${STEP}/${TOTAL_STEPS}] Foundry provisioning skipped (--ami-cleanup mode)"
    info "  Per-user sandboxes provision their own foundry at first boot."
    ok "Foundry: deferred to first boot"
elif [ "$IS_UPGRADE" = true ]; then
    info "[${STEP}/${TOTAL_STEPS}] Upgrading local Datafye foundry environment..."
    sudo -u datafye "${CLI_PATH}" foundry local upgrade \
        || { error "Foundry upgrade failed. The agent requires a working foundry environment and API MCP server to function. Resolve the issue and re-run the installer."; exit 1; }
    ok "Foundry environment upgraded"
else
    info "[${STEP}/${TOTAL_STEPS}] Provisioning local Datafye foundry environment..."
    info "  (First-time provision may take several minutes while Docker images are pulled.)"
    sudo -u datafye "${CLI_PATH}" foundry local provision \
        || { error "Foundry provision failed. The agent requires a working foundry environment and API MCP server to function. Resolve the issue and re-run the installer."; exit 1; }
    ok "Foundry environment provisioned"
fi

# ── Write configuration ──────────────────────────────────────────
cat > "${ENV_FILE}" << EOF
# Datafye Agent Configuration
# Version: ${VERSION}
# Updated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
DATAFYE_AGENT_VERSION=${VERSION}
DATAFYE_AGENT_MODE=${MODE}
DATAFYE_AGENT_PORT=${AGENT_PORT}
DATAFYE_AGENT_ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
DATAFYE_AGENT_WORKSPACE=${WORKSPACE_DIR}
DATAFYE_AGENT_DOCS_DIR=${DOCS_DIR}
DATAFYE_AGENT_SAMPLES_DIR=${SAMPLES_DIR}
DATAFYE_AGENT_CLI_PATH=${CLI_PATH}
DATAFYE_AGENT_DNS=${DNS_NAME}
DATAFYE_AGENT_PINNED=${VERSION_EXPLICIT}
DATAFYE_AGENT_API_MCP_URL=http://local-foundry-dev-mcp-api.datafye.local:3200/mcp
EOF
chmod 600 "${ENV_FILE}"
echo "${VERSION}" > "${INSTALL_DIR}/version"
ok "Config: ${ENV_FILE}"

# ── Write systemd service ────────────────────────────────────────
cat > /etc/systemd/system/datafye-agent.service << EOF
[Unit]
Description=Datafye Agent Service
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
User=datafye
# systemd does not source the user's .bashrc, so add the datafye user's
# ~/.local/bin (where claude is installed) and /usr/local/bin (datafye
# CLI symlink) explicitly. The default PATH otherwise omits both.
Environment=PATH=/home/datafye/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${AGENT_CODE_DIR}/main.py
Restart=on-failure
RestartSec=5
WorkingDirectory=${WORKSPACE_DIR}
# Identity, the credentials-store key, and the Anthropic key all arrive
# from the accounts service via the bootstrap push (POST /bootstrap) and
# the credentials channel — nothing is scraped from EC2 user data.

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable datafye-agent.service
ok "Systemd service: datafye-agent.service"

# ── Step: nginx + SSL (standalone mode only) ─────────────────────
if [ "$MODE" = "standalone" ]; then

    install_nginx() {
        if command -v nginx &> /dev/null; then
            ok "nginx already installed"
            return
        fi

        info "Installing nginx..."
        case $PLATFORM in
            amzn)
                yum install -y nginx || amazon-linux-extras install nginx1 -y 2>/dev/null || {
                    error "Failed to install nginx"; exit 1
                }
                ;;
            ubuntu|debian)
                apt-get install -y -qq nginx
                ;;
            rhel|centos|fedora|rocky|almalinux)
                yum install -y nginx
                ;;
        esac

        systemctl enable nginx
        systemctl start nginx
        ok "nginx installed"
    }

    next_step
    info "[${STEP}/${TOTAL_STEPS}] Configuring nginx..."
    install_nginx

    # Determine nginx config directory
    if [ -d /etc/nginx/sites-available ]; then
        NGINX_CONF_DIR="/etc/nginx/sites-available"
        NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"
    else
        mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
        NGINX_CONF_DIR="/etc/nginx/sites-available"
        NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"
        if ! grep -q "sites-enabled" /etc/nginx/nginx.conf 2>/dev/null; then
            sed -i '/http {/a \    include /etc/nginx/sites-enabled/*;' /etc/nginx/nginx.conf
        fi
    fi

    NGINX_SERVER_NAME="${DNS_NAME:-_}"

    cat > "${NGINX_CONF_DIR}/datafye-agent.conf" << NGINX
# Datafye Agent reverse proxy (standalone mode)
# Generated by installer v${VERSION}

server {
    listen 80 default_server;
    server_name ${NGINX_SERVER_NAME};

    access_log /var/log/nginx/datafye_agent_access.log;
    error_log /var/log/nginx/datafye_agent_error.log;

    # Agent API
    location /v1/ {
        proxy_pass http://127.0.0.1:${AGENT_PORT}/v1/;
        proxy_http_version 1.1;

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # SSE support
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;

        # Long timeouts for agent operations
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    location /health {
        proxy_pass http://127.0.0.1:${AGENT_PORT}/health;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
    }

    location / {
        root /var/www/datafye-agent;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX

    mkdir -p "${NGINX_ENABLED_DIR}"
    ln -sf "${NGINX_CONF_DIR}/datafye-agent.conf" "${NGINX_ENABLED_DIR}/datafye-agent.conf"
    rm -f "${NGINX_ENABLED_DIR}/default"

    # Placeholder page
    mkdir -p /var/www/datafye-agent
    cat > /var/www/datafye-agent/index.html << 'PLACEHOLDER'
<!DOCTYPE html>
<html><body style="background:#0a0a0a;color:#e6edf3;font-family:monospace;padding:40px;">
<h2>Datafye Agent</h2>
<p>Backend is running. Connect via the Datafye App at developer.datafye.io.</p>
<p><a href="/health" style="color:#f59e0b;">Check health</a></p>
</body></html>
PLACEHOLDER

    if nginx -t 2>/dev/null; then
        systemctl restart nginx
        ok "nginx configured and restarted"
    else
        error "nginx configuration test failed"
        exit 1
    fi

    # SSL
    next_step
    if [ -n "$DNS_NAME" ] && [ "$DNS_NAME" != "_" ]; then
        info "[${STEP}/${TOTAL_STEPS}] Setting up SSL with Let's Encrypt for ${DNS_NAME}..."

        case $PLATFORM in
            amzn|rhel|centos|fedora|rocky|almalinux)
                yum install -y certbot python3-certbot-nginx
                ;;
            ubuntu|debian)
                apt-get install -y -qq certbot python3-certbot-nginx
                ;;
        esac

        certbot --nginx -d "${DNS_NAME}" \
            --non-interactive \
            --agree-tos \
            --register-unsafely-without-email \
            --redirect

        ok "SSL configured for ${DNS_NAME}"
    else
        info "[${STEP}/${TOTAL_STEPS}] SSL skipped (no --dns name provided)"
    fi

fi  # end standalone mode

# ── Auto-upgrade ─────────────────────────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Configuring auto-upgrade..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -f "${SCRIPT_DIR}/upgrade-check.sh" ]; then
    cp "${SCRIPT_DIR}/upgrade-check.sh" "${INSTALL_DIR}/upgrade-check.sh"
elif [ ! -f "${INSTALL_DIR}/upgrade-check.sh" ]; then
    curl -fsSL "https://downloads.n5corp.com/datafye/agent/${VERSION}/upgrade-check.sh" \
        -o "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true
fi
chmod +x "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true

if [ -f "${SCRIPT_DIR}/install_template.sh" ]; then
    cp "${SCRIPT_DIR}/install_template.sh" "${INSTALL_DIR}/install.sh"
    chmod +x "${INSTALL_DIR}/install.sh"
fi

cat > /etc/cron.d/datafye-agent-upgrade << CRON
# Datafye Agent auto-upgrade check (every 5 minutes)
*/5 * * * * root ${INSTALL_DIR}/upgrade-check.sh >> /var/log/datafye-agent-upgrade.log 2>&1
CRON
ok "Auto-upgrade: every 5 minutes"

# ── AMI cleanup (if requested) ────────────────────────────────────
if [ "$AMI_CLEANUP" = true ]; then
    info "Cleaning up for AMI snapshot..."

    # Stop agent if running
    systemctl stop datafye-agent 2>/dev/null || true

    # Clear Anthropic key (will be injected at launch via user data)
    if [ -f "${ENV_FILE}" ]; then
        sed -i 's/^DATAFYE_AGENT_ANTHROPIC_API_KEY=.*/DATAFYE_AGENT_ANTHROPIC_API_KEY=/' "${ENV_FILE}"
    fi

    # Clear logs
    journalctl --rotate 2>/dev/null || true
    journalctl --vacuum-time=1s 2>/dev/null || true
    rm -rf /var/log/nginx/*.log 2>/dev/null || true
    rm -f /var/log/datafye-agent-upgrade.log

    # Clean Docker
    docker system prune -f 2>/dev/null || true

    # Clear shell history
    > /root/.bash_history
    history -c 2>/dev/null || true

    ok "AMI cleanup complete"

    echo ""
    info "================================================"
    ok "AMI ready for snapshot (v${VERSION}, ${MODE} mode)"
    info "================================================"
    echo ""
    exit 0
fi

# ── Start agent ──────────────────────────────────────────────────
if [ -z "$ANTHROPIC_API_KEY" ]; then
    warn "No Anthropic key provided."
    warn "Set it in ${ENV_FILE} then run: systemctl start datafye-agent"
else
    info "Starting agent..."
    systemctl start datafye-agent

    sleep 3
    if systemctl is-active --quiet datafye-agent; then
        ok "Agent service is running"

        HEALTH=$(curl -sf --connect-timeout 5 "http://127.0.0.1:${AGENT_PORT}/health" 2>/dev/null || true)
        if [ -n "$HEALTH" ]; then
            ok "Agent health check passed"
        else
            warn "Agent started but health check not responding yet (may still be initializing)"
        fi
    else
        error "Agent service failed to start"
        error "Check logs: journalctl -u datafye-agent"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────
echo ""
info "================================================"
if [ "$IS_UPGRADE" = true ]; then
    ok "Datafye Agent upgraded: ${CURRENT_VERSION} -> ${VERSION}"
else
    ok "Datafye Agent v${VERSION} installed (${MODE} mode)"
fi
info "================================================"
echo ""
info "  Config:    ${ENV_FILE}"
info "  Agent:     ${AGENT_CODE_DIR}"
info "  Workspace: ${WORKSPACE_DIR}"
info "  Docs:      ${DOCS_DIR}"
info "  Samples:   ${SAMPLES_DIR}"
info "  CLI:       ${CLI_PATH}"
info "  Venv:      ${VENV_DIR}"
echo ""
info "  Service:   systemctl {start|stop|restart|status} datafye-agent"
info "  Logs:      journalctl -u datafye-agent -f"
echo ""
if [ "$MODE" = "standalone" ]; then
    PROTOCOL="http"
    [ -n "$DNS_NAME" ] && PROTOCOL="https"
    DISPLAY_HOST="${DNS_NAME:-localhost}"
    info "  API:       ${PROTOCOL}://${DISPLAY_HOST}/v1/chat"
    info "  Health:    ${PROTOCOL}://${DISPLAY_HOST}/health"
else
    info "  API:       http://localhost:${AGENT_PORT}/v1/chat"
    info "  Health:    http://localhost:${AGENT_PORT}/health"
    info "  (Proxied via jump server in hosted mode)"
fi
echo ""
