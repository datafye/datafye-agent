#!/bin/bash
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
# Usage:
#   # Hosted mode (sandbox in Rumi cloud)
#   sudo ./install.sh --version 2.0.4 --mode hosted
#   sudo ./install.sh --version 2.0.4 --mode hosted --anthropic-key sk-ant-...
#
#   # Standalone mode (marketplace/DIY)
#   sudo ./install.sh --version 2.0.4 --mode standalone --dns agent.mycompany.com --anthropic-key sk-ant-...
#
#   # Upgrade (preserves credentials, mode, workspace)
#   sudo ./install.sh --version 2.0.5
#
#   # Force reinstall same version (useful for SNAPSHOT builds)
#   sudo ./install.sh --version 2.0.4-SNAPSHOT --force
#
#   # Build hosted AMI (install + cleanup for snapshot)
#   sudo ./install.sh --version 2.0.4 --mode hosted --ami-cleanup
#

set -e

# ── Defaults ──────────────────────────────────────────────────────
VERSION=""
MODE=""
DNS_NAME=""
ANTHROPIC_API_KEY=""
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

# ── Colors ────────────────────────────────────────────────────────
RED="\033[0;31m"; GREEN="\033[0;32m"; YELLOW="\033[1;33m"; CYAN="\033[0;36m"; RESET="\033[0m"
info()  { echo -e "${CYAN}$*${RESET}"; }
warn()  { echo -e "${YELLOW}$*${RESET}"; }
ok()    { echo -e "${GREEN}  ok: $*${RESET}"; }
error() { echo -e "${RED}ERROR: $*${RESET}" >&2; }

# ── Parse arguments ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --version)       VERSION="$2"; shift 2 ;;
        --mode)          MODE="$2"; shift 2 ;;
        --dns)           DNS_NAME="$2"; shift 2 ;;
        --anthropic-key) ANTHROPIC_API_KEY="$2"; shift 2 ;;
        --force)         FORCE=true; shift ;;
        --ami-cleanup)   AMI_CLEANUP=true; shift ;;
        --port)          AGENT_PORT="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Datafye Agent Installer

Usage:
  install.sh --version <version> --mode <hosted|standalone> [OPTIONS]

Options:
  --version <ver>     Datafye platform version (required)
  --mode <mode>       Installation mode (required for fresh install):
                        hosted     - Rumi cloud sandbox (no nginx, no SSL)
                        standalone - Marketplace/DIY (nginx + SSL)
  --dns <name>        DNS name (standalone mode, e.g., agent.mycompany.com)
  --anthropic-key <k> Anthropic API key (can be set later or via EC2 user data)
  --force             Reinstall even if same version (useful for SNAPSHOT)
  --ami-cleanup       Clean up for AMI snapshot (clear keys, logs, history)
  --port <port>       Agent port (default: 18780)
  -h, --help          Show this help
EOF
            exit 0
            ;;
        *)  error "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$VERSION" ]; then
    error "--version is required. Run with --help for usage."
    exit 1
fi

# ── Check root ────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    error "This installer must be run as root (sudo)"
    exit 1
fi

# ── SNAPSHOT handling ─────────────────────────────────────────────
is_snapshot() { [[ "$1" == *"-SNAPSHOT"* ]]; }

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
TOTAL_STEPS=9
[ "$MODE" = "standalone" ] && TOTAL_STEPS=11
STEP=0

next_step() { STEP=$((STEP + 1)); }

# ── Step: Install system dependencies ────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing system dependencies..."

case $PLATFORM in
    amzn)
        yum install -y python3 python3-pip git curl java-17-amazon-corretto-headless
        ;;
    ubuntu|debian)
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv git curl openjdk-17-jre-headless
        ;;
    rhel|centos|fedora|rocky|almalinux)
        yum install -y python3 python3-pip git curl java-17-openjdk-headless
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

ok "Python: $(python3 --version)"
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

next_step
info "[${STEP}/${TOTAL_STEPS}] Docker (for Datafye environment containers)..."
install_docker

# ── Step: Create directories and user ────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Directories and user..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${WORKSPACE_DIR}"
mkdir -p "${DOCS_DIR}"
mkdir -p "${SAMPLES_DIR}"

if ! id -u datafye &>/dev/null; then
    useradd -u 1000 -m -d /home/datafye -s /bin/bash datafye
fi
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
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing Claude Code CLI..."
if command -v claude &> /dev/null; then
    ok "Claude Code CLI already installed"
else
    curl -fsSL https://claude.ai/install.sh | bash
    ok "Claude Code CLI installed"
fi

# ── Step: Install Datafye CLI ────────────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing Datafye CLI v${VERSION}..."
curl -fsSL "https://downloads.n5corp.com/datafye/cli/${VERSION}/install.sh" | bash
CLI_PATH="${CLI_BASE}/${VERSION}/bin/datafye"
ok "Datafye CLI: ${CLI_PATH}"

# ── Step: Install/update docs, samples, and agent source ─────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing docs, samples, and agent source..."

clone_or_update_repo() {
    local repo_url="$1"
    local target_dir="$2"
    local version="$3"
    local label="$4"

    if [ -d "${target_dir}/.git" ]; then
        cd "${target_dir}"
        git fetch --depth 1 origin "v${version}" 2>/dev/null && git checkout "v${version}" 2>/dev/null \
            || { git fetch --depth 1 origin main && git checkout main; }
        cd - > /dev/null
    else
        rm -rf "${target_dir}"
        git clone --depth 1 --branch "v${version}" \
            "${repo_url}" "${target_dir}" 2>/dev/null \
            || git clone --depth 1 "${repo_url}" "${target_dir}"
    fi
    ok "${label}: ${target_dir}"
}

clone_or_update_repo "https://github.com/datafye/datafye-docs.git" "${DOCS_DIR}" "${VERSION}" "Docs"
clone_or_update_repo "https://github.com/datafye/datafye-samples.git" "${SAMPLES_DIR}" "${VERSION}" "Samples"

# Agent source code (public repo)
AGENT_CODE_DIR="${INSTALL_DIR}/app"
clone_or_update_repo "${AGENT_REPO}" "${AGENT_CODE_DIR}" "${VERSION}" "Agent"

# ── Step: Install Python dependencies ────────────────────────────
next_step
info "[${STEP}/${TOTAL_STEPS}] Installing Python dependencies..."

if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install --upgrade pip -q
"${VENV_DIR}/bin/pip" install -r "${AGENT_CODE_DIR}/requirements.txt" -q
ok "Python dependencies installed"

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
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${AGENT_CODE_DIR}/main.py
Restart=on-failure
RestartSec=5
WorkingDirectory=${WORKSPACE_DIR}

# Resolve Anthropic key from EC2 user data if not set
ExecStartPre=/bin/bash -c 'if [ -z "\$DATAFYE_AGENT_ANTHROPIC_API_KEY" ]; then \
    TOKEN=\$(curl -s --connect-timeout 2 -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || true); \
    if [ -n "\$TOKEN" ]; then \
        USER_DATA=\$(curl -s --connect-timeout 2 \
            -H "X-aws-ec2-metadata-token: \$TOKEN" \
            "http://169.254.169.254/latest/user-data" 2>/dev/null || true); \
        KEY=\$(echo "\$USER_DATA" | grep -oP "DATAFYE_AGENT_ANTHROPIC_API_KEY=\\K.*" || true); \
        if [ -n "\$KEY" ]; then \
            sed -i "s/^DATAFYE_AGENT_ANTHROPIC_API_KEY=.*/DATAFYE_AGENT_ANTHROPIC_API_KEY=\$KEY/" ${ENV_FILE}; \
        fi; \
    fi; \
fi'

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
