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
#
#   # Standalone mode (marketplace/DIY)
#   sudo ./install.sh --mode standalone --dns agent.mycompany.com
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
PINNED=false
MODE=""
DNS_NAME=""
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
        --version)        VERSION="$2"; VERSION_EXPLICIT=true; shift 2 ;;
        --pin)            PINNED=true; shift ;;
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
  --version <v>         Override the baked-in version. Accepts X.Y.Z for
                        released builds or X.Y-SNAPSHOT for internal testing.
                        This selects WHAT to install; it does NOT disable
                        auto-upgrade. Use --pin for that.
  --pin                 Never auto-upgrade this install. The upgrade cron
                        stays installed but stands down, so the box holds the
                        version it has until someone upgrades it by hand.
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

# ── Auto-upgrade mid-turn guard (last-moment re-check) ────────────
# When invoked by the idle-gated upgrade-check (which exports DATAFYE_AUTO_UPGRADE=1),
# re-check the running agent's /health right before we touch anything and abort if
# a turn started in the gate-to-now window. A restart drops the in-flight
# resumable-turn buffer, so we NEVER restart the agent mid-turn — better to defer
# and let the next cron tick retry. Not armed for fresh/manual installs (the var
# is only set on the auto-upgrade path), so operators are never blocked.
if [ "${DATAFYE_AUTO_UPGRADE:-}" = "1" ] && [ -f "${INSTALL_DIR}/version" ]; then
    _guard_port=$(grep -oP '^DATAFYE_AGENT_PORT=\K.*' "${INSTALL_DIR}/agent.env" 2>/dev/null || true)
    _guard_port="${_guard_port:-18780}"
    _guard_health=$(curl -fsS --connect-timeout 3 --max-time 5 "http://127.0.0.1:${_guard_port}/health" 2>/dev/null || true)
    if [ -n "${_guard_health}" ]; then
        _guard_rj=$(printf '%s' "${_guard_health}" | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("running_jobs",0) or 0)
except Exception: print(0)' 2>/dev/null || echo 0)
        if [ "${_guard_rj}" -gt 0 ] 2>/dev/null; then
            info "Auto-upgrade aborted: agent is mid-turn (running_jobs=${_guard_rj}); next cron tick will retry."
            exit 0
        fi
    fi
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

# ── Node.js for project code (DAT-201) ───────────────────────────
# The lifecycle tracks already promise this: a `dashboard`/`app`/`tool` intent
# routes to Explore -> Design -> Build -> Ship, and there was nothing to build
# with. The concrete failure was smaller and more telling -- the model reached
# for a bundled skill's palette validator, reported "No Node in this sandbox to
# re-run the validator", and proceeded on documented values instead of checking
# its own work.
#
# PINNED, like Maven above and unlike the Claude CLI (DAT-215): a runtime that
# silently differs per box is a class of bug we have already paid for. Moving
# the pin is a reviewable edit here.
#
# ⚠️ ~209 MB extracted. That is comparable to the whole quant stack, on a box
# that still runs everything off one root volume (DAT-178) -- and per-project
# `node_modules` lands on top of it. See PROJECT.md for the disk arithmetic.
NODE_VERSION="24.19.0"          # LTS "Krypton"
case "$(uname -m)" in
    x86_64|amd64)  NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    *)             NODE_ARCH="" ;;
esac
NODE_HOME="/opt/node-v${NODE_VERSION}-linux-${NODE_ARCH}"
if [ -z "${NODE_ARCH}" ]; then
    warn "Unsupported architecture $(uname -m) for Node; skipping."
    warn "The model will not be able to build or run JavaScript."
elif [ -x "${NODE_HOME}/bin/node" ]; then
    ok "Node.js already at the pinned version: v${NODE_VERSION}"
else
    info "Installing Node.js v${NODE_VERSION} (${NODE_ARCH})..."
    # Fetch to a temp file first: piping straight into tar leaves a half-extracted
    # tree in /opt when the download dies, and the guard above would then treat a
    # broken install as complete on the next run.
    NODE_TGZ="$(mktemp -t node.XXXXXX.tar.xz)"
    if curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" -o "${NODE_TGZ}" \
       && tar -xJf "${NODE_TGZ}" -C /opt; then
        ln -sf "${NODE_HOME}/bin/node" /usr/local/bin/node
        ln -sf "${NODE_HOME}/bin/npm"  /usr/local/bin/npm
        ln -sf "${NODE_HOME}/bin/npx"  /usr/local/bin/npx
        ok "Node.js: $(node --version 2>/dev/null) / npm $(npm --version 2>/dev/null)"
    else
        warn "Could not install Node.js; the model will not be able to run JavaScript."
        warn "Everything else is unaffected -- re-run the installer to retry."
    fi
    rm -f "${NODE_TGZ}"
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

# ── npm writes under the datafye user, never into /opt (DAT-201) ──
# Project dependencies are already project-local: `npm install` in a project
# folder writes ./node_modules, which is exactly the per-project isolation the
# `.venv` gives Python. The trap is `npm install -g`, whose default prefix is
# the Node install in /opt -- root-owned, like the agent's own venv, and for the
# same deliberate reason. Without this the model would meet an EACCES it cannot
# fix and could not tell from "Node is broken".
#
# Written as ~datafye/.npmrc rather than exported in a profile script because
# npm reads it whatever the shell is. A model's Bash command is not a login
# shell, so /etc/profile.d would not reach it.
NPM_GLOBAL_DIR="/home/datafye/.npm-global"
if [ ! -f /home/datafye/.npmrc ] || ! grep -q "^prefix=" /home/datafye/.npmrc 2>/dev/null; then
    echo "prefix=${NPM_GLOBAL_DIR}" >> /home/datafye/.npmrc
fi
mkdir -p "${NPM_GLOBAL_DIR}/bin"
# The npm CACHE too: if anything ever runs npm as root it creates a root-owned
# ~/.npm, and every later install as datafye fails on it.
mkdir -p /home/datafye/.npm
chown -R datafye:datafye /home/datafye/.npmrc "${NPM_GLOBAL_DIR}" /home/datafye/.npm 2>/dev/null || true
# Globally-installed CLIs land in that bin dir; put it on PATH for login shells.
cat > /etc/profile.d/datafye-node.sh << EOF
# npm's global prefix for the datafye user (see ~datafye/.npmrc)
export PATH="\${PATH}:${NPM_GLOBAL_DIR}/bin"
EOF
chmod 0644 /etc/profile.d/datafye-node.sh

ok "Workspace: ${WORKSPACE_DIR}"

# ── Step: Stop existing service on upgrade ───────────────────────
next_step
if [ "$IS_UPGRADE" = true ]; then
    info "[${STEP}/${TOTAL_STEPS}] Stopping existing agent..."
    systemctl stop datafye-agent 2>/dev/null || true
    ok "Agent service stopped"
    # ⚠️ FROM HERE UNTIL THE START AT THE END, A FAILURE LEAVES THE AGENT DOWN.
    # This script runs under `set -e`, so any unguarded non-zero command exits
    # immediately -- and everything between here and the start is the whole
    # install. Observed live: a git failure at step 7 exited the script with the
    # agent stopped, the once-a-minute cron saw /health unreachable (which it
    # reads as "nothing to protect, proceed"), ran the installer again, stopped
    # the already-stopped agent, failed at the same step, and repeated. A
    # transient fault became permanent downtime plus an infinite retry loop.
    #
    # The old code is still on disk when an install fails partway, so starting
    # it back up restores a working agent. Cleared just before the deliberate
    # start at the end, so the normal path is unaffected.
    _restore_agent_on_failure() {
        local rc=$?
        [ "${rc}" -eq 0 ] && return 0
        warn "Install failed (exit ${rc}) -- restarting the previous agent so the box is not left down."
        systemctl start datafye-agent 2>/dev/null || true
        return "${rc}"
    }
    trap _restore_agent_on_failure EXIT
else
    info "[${STEP}/${TOTAL_STEPS}] Fresh install (no existing service)"
fi

# ── Step: Install Claude Code CLI ────────────────────────────────
# claude.ai/install.sh always lays files under the invoking user's
# ~/.local (binary + supporting state), and the binary resolves its
# install root at runtime — so a root install + /usr/local/bin symlink
# would not work. Install as the datafye user (the runtime user) so
# claude lands at /home/datafye/.local/{bin,share}/claude.
#
# ⚠️ THIS IS NOT THE HARNESS. A turn runs the CLI bundled inside the SDK
# (claude_agent_sdk/_bundled/claude), because SubprocessCLITransport._find_cli
# checks there FIRST and only then falls back to `which claude`. Nothing in the
# agent invokes this binary; it exists solely as that fallback, for a future SDK
# that ships without a bundled one. Kept deliberately (DAT-215) rather than
# removed: it is cheap insurance against the agent having no harness at all.
#
# ⚠️ It is also ~300 MB of duplicate on a single root volume, and on the box
# that was measured it was byte-for-byte the same VERSION as the bundled one
# (both 2.1.228) — a coincidence that made it easy to believe this was the
# harness. If DAT-178 (disk) gets tight, this is the first thing to reconsider,
# but do it as a decision about the fallback, not as a cleanup.
#
# `/v1/bom` reports both, and which one is in use, so nobody has to SSH in and
# guess again.
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
    # SNAPSHOT mode does NOT install or upgrade the Datafye CLI -- it uses
    # whatever CLI is already on the box. Datafye normally versions every
    # component (platform/CLI/docs/samples/agent) on one number, but an
    # agent-only SNAPSHOT (validating agent code) works fine against a released
    # CLI, so the version is REPORTED, not enforced. If a SNAPSHOT agent change
    # genuinely needs a newer CLI, upgrade the CLI manually first.
    info "[${STEP}/${TOTAL_STEPS}] Locating Datafye CLI (SNAPSHOT mode: version not enforced)..."
    if command -v datafye &>/dev/null; then
        CLI_PATH=$(command -v datafye)
    elif [ -x "${CLI_BASE}/current/bin/datafye" ]; then
        CLI_PATH="${CLI_BASE}/current/bin/datafye"
    else
        error "No Datafye CLI found (on PATH or at ${CLI_BASE}/current). SNAPSHOT mode does"
        error "not install the CLI -- install it manually, then re-run."
        exit 1
    fi
    INSTALLED_CLI_VERSION=$("${CLI_PATH}" version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?(-[A-Za-z0-9.-]+)?' | head -1 || true)
    if [ "$INSTALLED_CLI_VERSION" != "$VERSION" ]; then
        warn "Local Datafye CLI is v${INSTALLED_CLI_VERSION:-unknown}, not ${VERSION}. SNAPSHOT"
        warn "installs do not upgrade the CLI -- proceeding with the installed one. Upgrade the"
        warn "CLI manually if your agent changes require a newer CLI."
    fi
    ok "Using local Datafye CLI: ${CLI_PATH} (v${INSTALLED_CLI_VERSION:-unknown})"
else
    info "[${STEP}/${TOTAL_STEPS}] Installing Datafye CLI v${VERSION}..."
    # Retry the fetch with a bounded backoff (DAT-123): the CLI installer can lag
    # briefly on downloads -- e.g. a release cut where the agent AMI bake outraces
    # the CLI publish, or a transient downloads blip -- so don't fail on the first
    # 404. Up to ~5 minutes.
    CLI_INSTALLER_URL="https://downloads.n5corp.com/datafye/cli/${VERSION}/install.sh"
    CLI_INSTALLER_TMP="$(mktemp)"
    cli_fetched=false
    for cli_attempt in $(seq 1 20); do
        if curl -fsSL "${CLI_INSTALLER_URL}" -o "${CLI_INSTALLER_TMP}" 2>/dev/null; then
            cli_fetched=true; break
        fi
        [ "${cli_attempt}" -eq 1 ] && info "  Waiting for the CLI ${VERSION} installer to appear on downloads..."
        sleep 15
    done
    if [ "${cli_fetched}" != true ]; then
        rm -f "${CLI_INSTALLER_TMP}"
        error "Could not fetch the Datafye CLI ${VERSION} installer from ${CLI_INSTALLER_URL} after retries."
        exit 1
    fi
    bash "${CLI_INSTALLER_TMP}"
    rm -f "${CLI_INSTALLER_TMP}"
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

# ── Pin Java 17 for the Datafye CLI, globally (DAT-116) ───────────
# The box's default PATH `java` may be Java 8 (`/usr/lib/jvm/java-1.8.0`), but the
# Datafye CLI requires Java 17. Set DF_CLI_JAVA_HOME to the corretto-17 (or
# openjdk-17) home in /etc/profile.d so the CLI works for ANY user/shell -- an
# operator running `datafye ...` by hand, not just the install-time invocation.
# Safe to bake into an AMI (global, not instance-specific).
DF_CLI_JAVA_HOME=""
for _j in /usr/lib/jvm/java-17-amazon-corretto* /usr/lib/jvm/java-17-openjdk*; do
    if [ -x "${_j}/bin/java" ]; then DF_CLI_JAVA_HOME="${_j}"; break; fi
done
if [ -n "${DF_CLI_JAVA_HOME}" ]; then
    cat > /etc/profile.d/datafye-cli.sh << EOF
# The Datafye CLI requires Java 17; the box's default PATH java may be older.
export DF_CLI_JAVA_HOME="${DF_CLI_JAVA_HOME}"
EOF
    chmod 0644 /etc/profile.d/datafye-cli.sh
    ok "Datafye CLI Java 17: DF_CLI_JAVA_HOME=${DF_CLI_JAVA_HOME} (/etc/profile.d/datafye-cli.sh)"
else
    warn "Could not find a Java 17 home under /usr/lib/jvm -- DF_CLI_JAVA_HOME not set."
    warn "An interactive 'datafye ...' may fail if PATH java is older than 17."
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
        # ⚠️ -c safe.directory on EVERY call, not a global config write. These
        # trees are managed by root from cron but can carry another user's
        # ownership (an AMI bake seeded with `cp -a`, a hand-fixed box), and git
        # then refuses with "detected dubious ownership" -- which under `set -e`
        # aborted the upgrade mid-flight with the agent stopped. Scoped to the
        # directory we are about to touch rather than `--global --add`, so the
        # exemption cannot silently outlive this command or widen to a repo we
        # did not put here.
        local safe=(-c "safe.directory=${target_dir}")
        cd "${target_dir}"
        git "${safe[@]}" remote set-url origin "${repo_url}"
        git "${safe[@]}" fetch --depth 1 origin "${git_ref}" \
            || { error "${label}: failed to fetch ${git_ref}"; exit 1; }
        git "${safe[@]}" checkout -qf FETCH_HEAD
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
    # ⚠️ `cp -a` preserves ownership, so the app tree inherits whoever owned the
    # BUILD checkout -- typically not root. Every later upgrade runs git here as
    # root from cron, and git refuses a repo owned by another user ("detected
    # dubious ownership"), which under `set -e` killed the whole upgrade. The
    # tree is meant to be root-owned anyway: that is what makes it read-only to
    # the agent (see the fleet-memory note in CLAUDE.md).
    chown -R root:root "${AGENT_CODE_DIR}"
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

# ── Quant stack for PROJECT code (DAT-186) ───────────────────────
# Installed into the SYSTEM interpreter, not the agent's venv above. Each
# project gets its own venv built with --system-site-packages (see
# conversations._ensure_venv), so it inherits these instantly and keeps its
# own site-packages on top for anything project-specific.
#
# Pre-baked rather than installed on demand for two reasons: it puts a
# multi-minute download in front of the user's first message otherwise, and a
# sandbox may have slow or no egress to PyPI. The agent previously concluded
# "No pip available" and hand-wrote its numerics in pure Python, which is a
# poor and silently wrong-prone way to do statistics.
#
# Deliberately NOT installed into ${VENV_DIR}: that venv runs the agent
# service itself, and project code must never be able to break it by
# upgrading a shared dependency.
# The package list lives in install/quant-stack.txt, which prompt.py also reads
# to tell the model what it already has. One list, so the two cannot drift and
# the model neither reinstalls what is present nor assumes what is absent.
QUANT_STACK_FILE="${AGENT_CODE_DIR}/install/quant-stack.txt"
if [ -f "${QUANT_STACK_FILE}" ]; then
    QUANT_PKGS=$(grep -vE '^\s*(#|$)' "${QUANT_STACK_FILE}" | tr '\n' ' ')
else
    # A tree without the file predates it; keep the old behaviour rather than
    # installing nothing, since project code depends on this stack existing.
    warn "quant-stack.txt not found; falling back to the built-in list"
    QUANT_PKGS="pandas numpy scipy matplotlib requests tabulate"
fi
info "Installing the quant stack for project code (${QUANT_PKGS})..."
if ${PYTHON_BIN} -m pip install --quiet ${QUANT_PKGS}; then
    ok "Quant stack installed for project venvs"
else
    warn "Could not install the quant stack; project venvs will still build, but"
    warn "pandas/numpy will be missing until this is re-run. Agent is unaffected."
fi
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

# Append the managed block. The rumi.local hostnames are the Rumi foundry
# services the launched environment binds and the agent talks to for Java-based
# streaming, REST, and the per-dataset feeds/aggs; the datafye.local hostnames
# are the local foundry deployment's API/admin/monitor/MCP endpoints.
cat >> /etc/hosts <<EOF

${HOSTS_MARKER_START}
127.0.0.1   solace.rumi.local
127.0.0.1   api.rest.rumi.local
127.0.0.1   api.stream.rumi.local
127.0.0.1   synthetic.feed.rumi.local
127.0.0.1   synthetic.agg.rumi.local
127.0.0.1   synthetic.history.rumi.local
127.0.0.1   synthetic.reference.rumi.local
127.0.0.1   sip.feed.rumi.local
127.0.0.1   sip.agg.rumi.local
127.0.0.1   crypto.feed.rumi.local
127.0.0.1   crypto.agg.rumi.local
127.0.0.1   local-foundry-dev-api.datafye.local
127.0.0.1   local-foundry-dev-admin.datafye.local
127.0.0.1   local-foundry-dev-monitor.datafye.local
127.0.0.1   local-foundry-dev-mcp-api.datafye.local
${HOSTS_MARKER_END}
EOF
ok "/etc/hosts configured (rumi.local + datafye.local hostnames → 127.0.0.1)"

# ── Step: Provision / upgrade local Datafye foundry environment ──
# Skip in --ami-cleanup mode: foundry provisioning pulls docker images,
# starts containers, and writes instance-specific state under ~/.rumi
# (admin-docker-compose.yml, named volumes, etc.) — none of which is
# safe to snapshot into an AMI. Each per-user sandbox provisions its
# own foundry on its first boot, via the datafye-foundry-boot.service
# one-shot installed below in every mode (foundry-boot.sh).
#
# That last sentence used to be a promise nothing kept: --ami-cleanup
# skipped provisioning and deferred to a "first boot" step that did not
# exist for hosted mode (first-boot.sh is standalone-only and hard-codes
# --mode standalone), so every hosted sandbox came up with no foundry
# while prompt.py told the agent one already existed (DAT-170).
next_step
if [ "$AMI_CLEANUP" = true ]; then
    info "[${STEP}/${TOTAL_STEPS}] Foundry provisioning skipped (--ami-cleanup mode)"
    info "  datafye-foundry-boot.service provisions it on the instance's first boot."
    ok "Foundry: deferred to first boot"
elif [ "$IS_UPGRADE" = true ]; then
    info "[${STEP}/${TOTAL_STEPS}] Upgrading local Datafye foundry environment..."
    if sudo -u datafye "${CLI_PATH}" foundry local upgrade; then
        ok "Foundry environment upgraded"
    else
        # Non-fatal (DAT-115): a stale/broken foundry left by prior testing can fail
        # the FAST-restart upgrade. The agent is still useful WITHOUT a foundry
        # (chat, docs, code and memory all work), so we must NOT abort here and
        # leave the datafye-agent service stopped -- it is started below regardless.
        warn "Foundry upgrade failed -- continuing so the agent is not left stopped."
        warn "The agent does NOT provision on demand: a box gets its foundry from"
        warn "datafye-foundry-boot.service at boot. Deprovision + reprovision as the"
        warn "datafye user, or reboot to let that unit retry, and investigate."
    fi
else
    info "[${STEP}/${TOTAL_STEPS}] Provisioning local Datafye foundry environment..."
    info "  (First-time provision may take several minutes while Docker images are pulled.)"
    if sudo -u datafye "${CLI_PATH}" foundry local provision; then
        ok "Foundry environment provisioned"
    else
        # Non-fatal for the same reason: never leave the agent service down over a
        # foundry problem. datafye-foundry-boot.service retries on the next boot,
        # because it keys on real state rather than a marker.
        warn "Foundry provision failed -- continuing so the agent is not left stopped."
        warn "datafye-foundry-boot.service retries on the next boot; investigate separately."
    fi
fi

# ── Write configuration ──────────────────────────────────────────
cat > "${ENV_FILE}" << EOF
# Datafye Agent Configuration
# Version: ${VERSION}
# Updated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
DATAFYE_AGENT_VERSION=${VERSION}
DATAFYE_AGENT_MODE=${MODE}
DATAFYE_AGENT_PORT=${AGENT_PORT}
DATAFYE_AGENT_WORKSPACE=${WORKSPACE_DIR}
DATAFYE_AGENT_DOCS_DIR=${DOCS_DIR}
DATAFYE_AGENT_SAMPLES_DIR=${SAMPLES_DIR}
DATAFYE_AGENT_CLI_PATH=${CLI_PATH}
DATAFYE_AGENT_DNS=${DNS_NAME}
DATAFYE_AGENT_PINNED=${PINNED}
DATAFYE_AGENT_API_MCP_URL=http://local-foundry-dev-mcp-api.datafye.local:3200/mcp
EOF
chmod 600 "${ENV_FILE}"
echo "${VERSION}" > "${INSTALL_DIR}/version"
ok "Config: ${ENV_FILE}"

# Dependency BOM — served by the agent at GET /v1/bom and shown on the Yukti
# agent surface. Datafye versions all components together, so a single version
# covers platform, samples, CLI, and docs.
cat > "${INSTALL_DIR}/bom.json" << EOF
{
  "agent_version": "${VERSION}",
  "built_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "dependencies": {
    "datafye": {"version": "${VERSION}", "covers": "platform, samples, CLI, docs (Datafye versions all components on one number)"}
  }
}
EOF
ok "BOM: ${INSTALL_DIR}/bom.json"

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

# ── Write the foundry boot reconciler one-shot (DAT-199) ─────────
# Installed in EVERY mode. Its predecessor (datafye-foundry-firstboot.service)
# was hosted-only, which left a self-provisioned user -- who stops and starts
# their own box and hits the identical app-less wake -- with nothing at all.
#
# RemainAfterExit is deliberately NOT set. The unit is meant to be re-evaluated
# on every boot: foundry-boot.sh keys on real state rather than a marker file,
# so a normal reboot with a healthy foundry costs a fast no-op, while a boot
# after a FAILED provision retries instead of being locked out by a marker
# written before the work succeeded.
#
# Ordering AFTER datafye-agent.service is load-bearing and must stay. The agent
# comes up first and stays reachable throughout; gating it on the foundry would
# take the box off the network for the length of a cold provision.

# Retire the old unit BEFORE writing the new one. An upgraded box already has
# datafye-foundry-firstboot.service enabled and pointing at a copy of the old
# script that is still on disk, so leaving it would put TWO boot-time actors on
# one foundry -- the u1 incident, reintroduced by the change meant to prevent
# it. `disable` alone is not enough: the unit file has to go, or a later
# `systemctl enable` by hand quietly resurrects it.
if [ -f /etc/systemd/system/datafye-foundry-firstboot.service ]; then
    systemctl disable --now datafye-foundry-firstboot.service 2>/dev/null || true
    rm -f /etc/systemd/system/datafye-foundry-firstboot.service
    rm -f "${INSTALL_DIR}/first-boot-foundry.sh"
    ok "Retired datafye-foundry-firstboot.service (replaced by datafye-foundry-boot.service)"
fi

cat > /etc/systemd/system/datafye-foundry-boot.service << EOF
[Unit]
Description=Datafye foundry boot reconciler
Documentation=https://linear.app/datafye/issue/DAT-199
After=network-online.target docker.service datafye-agent.service
Wants=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=${INSTALL_DIR}/foundry-boot.sh
# A cold first provision pulls images and starts the whole platform; it ran
# ~17 minutes on a 4-core box during DAT-174 validation, so give it real room
# rather than killing a provision that was going to succeed.
TimeoutStartSec=1800
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable datafye-foundry-boot.service
ok "Systemd service: datafye-foundry-boot.service (reconciles the foundry on every boot)"

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
<p>Backend is running. Connect via Yukti at yukti.datafye.ai.</p>
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

# SCRIPT_DIR is only meaningful when this installer runs as a FILE (a manual
# install, first-boot, or the Packer bake). On the auto-upgrade path it arrives
# as `curl … | bash`, where BASH_SOURCE[0] is unset and `dirname` would silently
# resolve to cron's working directory — a directory with nothing to do with the
# installer. Leave it EMPTY there so the companion-script lookup below falls
# through to the version-matched copies in the cloned agent tree, rather than
# being hijacked by a stray upgrade-check.sh that happens to sit in the cwd.
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
else
    SCRIPT_DIR=""
fi

# Refresh the installer's companion scripts in the install dir on EVERY install,
# including auto-upgrades. Two things this has to get right:
#
# 1. ALWAYS REPLACE. The old shape only copied from SCRIPT_DIR, and fell back to
#    a download that was skipped whenever the file already existed. On the
#    auto-upgrade path SCRIPT_DIR is empty (see above) and the file always
#    exists, so upgrade-check.sh was NEVER refreshed — a box stayed pinned to
#    whatever version was baked into its AMI, and a fix to upgrade-check.sh
#    could only ship via a re-bake. The cloned agent tree carries the
#    version-matched copy, so use it as the fallback source instead.
#
# 2. ATOMIC REPLACE (write-temp + mv), never a plain `cp` onto the destination.
#    On the auto-upgrade path this installer is invoked BY the very
#    upgrade-check.sh it is about to replace, and that script is still
#    executing. bash reads a script lazily, by byte offset: a `cp` truncates and
#    rewrites the SAME inode, so the running shell's fd suddenly points at
#    different bytes, and the next line it reads is whatever happens to sit at
#    its saved offset in the new file. That is a mid-line fragment, so bash dies
#    with a bogus "syntax error near unexpected token" AFTER the upgrade already
#    succeeded. `mv` swaps the directory entry instead: the running shell keeps
#    the original (now unlinked) inode and reads its own remaining lines intact.
# foundry-boot.sh rides this loop for the same reason upgrade-check.sh does: it
# is referenced by a systemd unit from INSTALL_DIR, so without a refresh here a
# box would stay pinned to whatever version its AMI baked and a fix could only
# reach the fleet via a re-bake, never via auto-upgrade.
for f in upgrade-check.sh install.sh foundry-boot.sh; do
    src_candidates=()
    [ -n "${SCRIPT_DIR}" ] && src_candidates+=("${SCRIPT_DIR}/${f}")
    src_candidates+=("${AGENT_CODE_DIR}/install/${f}")
    # install_template.sh gets stored as install.sh in the install dir for
    # upgrades — source it under either name.
    if [ "$f" = "install.sh" ]; then
        [ -n "${SCRIPT_DIR}" ] && src_candidates+=("${SCRIPT_DIR}/install_template.sh")
        src_candidates+=("${AGENT_CODE_DIR}/install/install_template.sh")
    fi
    for src in "${src_candidates[@]}"; do
        if [ -f "$src" ]; then
            cp "$src" "${INSTALL_DIR}/.${f}.new"
            chmod +x "${INSTALL_DIR}/.${f}.new"
            mv -f "${INSTALL_DIR}/.${f}.new" "${INSTALL_DIR}/${f}"
            break
        fi
    done
done

# Last resort: a first install where the agent tree somehow has no install/ dir.
if [ ! -f "${INSTALL_DIR}/upgrade-check.sh" ]; then
    curl -fsSL "https://downloads.n5corp.com/datafye/agent/${VERSION}/upgrade-check.sh" \
        -o "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true
    chmod +x "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true
fi

cat > /etc/cron.d/datafye-agent-upgrade << CRON
# Datafye Agent auto-upgrade check (every minute, under flock).
# The check itself is cheap and idle-gated (it defers unless the agent is idle
# and the user is away); flock -n makes a tick a no-op while a prior check OR an
# in-flight install still holds the lock, so upgrades never overlap or re-fire
# mid-install.
* * * * * root /usr/bin/flock -n /run/lock/datafye-agent-upgrade.lock ${INSTALL_DIR}/upgrade-check.sh >> /var/log/datafye-agent-upgrade.log 2>&1
CRON
ok "Auto-upgrade: idle-gated check every minute (flock-guarded)"

# ── AMI cleanup (if requested) ────────────────────────────────────
if [ "$AMI_CLEANUP" = true ]; then
    info "Cleaning up for AMI snapshot..."

    # Stop agent if running
    systemctl stop datafye-agent 2>/dev/null || true

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
# The agent boots into the awaiting-bootstrap state — it starts with no
# identity or credentials and waits for the accounts service to push them
# (POST /bootstrap, then the Anthropic key over the credentials channel).
# So always start it; no key is needed at install time.
# Past every step that could fail; the start below is the deliberate one.
trap - EXIT
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
