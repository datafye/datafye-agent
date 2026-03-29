#!/bin/bash
#
# Datafye Agent - Backend Installer / Upgrader
#
# Installs or upgrades the Datafye Agent backend on a Linux machine.
# Handles: Docker, agent container image, nginx reverse proxy.
#
# Usage:
#   # Fresh install
#   sudo ./install.sh --version 2.0.4 --anthropic-key sk-ant-...
#
#   # Upgrade to new version (preserves credentials and workspace)
#   sudo ./install.sh --version 2.0.5
#
#   # AMI prep (no key, skips startup)
#   sudo ./install.sh --version 2.0.4 --ami-prep
#
#   # Force reinstall same version
#   sudo ./install.sh --version 2.0.4 --force
#

set -e

# ── Defaults ──────────────────────────────────────────────────────
VERSION=""
ANTHROPIC_API_KEY=""
AMI_PREP=false
FORCE=false
AGENT_PORT=18780
AGENT_IMAGE="datafye/datafye-agent"
INSTALL_DIR="/opt/datafye/agent"
WORKSPACE_DIR="/home/datafye/workspace"

# ── Parse arguments ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --version)       VERSION="$2"; shift 2 ;;
        --anthropic-key) ANTHROPIC_API_KEY="$2"; shift 2 ;;
        --ami-prep)      AMI_PREP=true; shift ;;
        --force)         FORCE=true; shift ;;
        --port)          AGENT_PORT="$2"; shift 2 ;;
        *)               echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$VERSION" ]; then
    echo "Error: --version is required"
    echo ""
    echo "Usage:"
    echo "  install.sh --version <version> [--anthropic-key <key>] [--ami-prep] [--force]"
    echo ""
    echo "Options:"
    echo "  --version        Datafye platform version (required)"
    echo "  --anthropic-key  Anthropic API key (optional, can be set later)"
    echo "  --ami-prep       Prepare for AMI snapshot (skip startup)"
    echo "  --force          Reinstall even if same version is already installed"
    echo "  --port           Agent port (default: 18780)"
    exit 1
fi

# ── Check root ────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "Error: This installer must be run as root (sudo)"
    exit 1
fi

# ── Detect existing installation ──────────────────────────────────
CURRENT_VERSION=""
IS_UPGRADE=false
ENV_FILE="${INSTALL_DIR}/agent.env"

if [ -f "${INSTALL_DIR}/version" ]; then
    CURRENT_VERSION=$(cat "${INSTALL_DIR}/version")

    if [ "${CURRENT_VERSION}" = "${VERSION}" ] && [ "$FORCE" = false ]; then
        echo "Datafye Agent v${VERSION} is already installed."
        echo "Use --force to reinstall, or specify a different --version."
        exit 0
    fi

    IS_UPGRADE=true
fi

if [ "$IS_UPGRADE" = true ]; then
    echo "================================================"
    echo "  Datafye Agent Upgrade: ${CURRENT_VERSION} -> ${VERSION}"
    echo "================================================"
else
    echo "================================================"
    echo "  Datafye Agent Install: v${VERSION}"
    echo "================================================"
fi
echo ""

# ── Preserve existing credentials on upgrade ──────────────────────
if [ "$IS_UPGRADE" = true ] && [ -f "${ENV_FILE}" ]; then
    echo "  Preserving existing configuration..."
    # Read existing key if not provided on command line
    if [ -z "$ANTHROPIC_API_KEY" ]; then
        ANTHROPIC_API_KEY=$(grep -oP '^ANTHROPIC_API_KEY=\K.*' "${ENV_FILE}" || true)
    fi
    # Preserve port from existing config
    EXISTING_PORT=$(grep -oP '^DATAFYE_AGENT_PORT=\K.*' "${ENV_FILE}" || true)
    if [ -n "$EXISTING_PORT" ]; then
        AGENT_PORT="${EXISTING_PORT}"
    fi
fi

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
echo "[1/7] Platform: ${PLATFORM}"

# ── Install Docker ────────────────────────────────────────────────
install_docker() {
    if command -v docker &> /dev/null; then
        echo "[2/7] Docker: $(docker --version)"
        return
    fi

    echo "[2/7] Installing Docker..."
    case $PLATFORM in
        amzn)
            yum install -y docker
            ;;
        ubuntu|debian)
            apt-get update
            apt-get install -y docker.io
            ;;
        *)
            echo "Unsupported platform: ${PLATFORM}. Install Docker manually."
            exit 1
            ;;
    esac

    systemctl enable docker
    systemctl start docker
    echo "  Docker installed: $(docker --version)"
}

install_docker

# ── Create directories and user ──────────────────────────────────
echo "[3/7] Directories and user..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${WORKSPACE_DIR}"

if ! id -u datafye &>/dev/null; then
    useradd -u 1000 -m -d /home/datafye -s /bin/bash datafye
fi
chown -R datafye:datafye "${WORKSPACE_DIR}"

# ── Stop existing container on upgrade ────────────────────────────
if [ "$IS_UPGRADE" = true ]; then
    echo "  Stopping existing agent..."
    docker stop datafye-agent 2>/dev/null || true
    docker rm datafye-agent 2>/dev/null || true
fi

# ── Pull agent image ─────────────────────────────────────────────
echo "[4/7] Pulling ${AGENT_IMAGE}:${VERSION}..."
docker pull "${AGENT_IMAGE}:${VERSION}"

# Clean up old image if upgrading
if [ "$IS_UPGRADE" = true ] && [ "${CURRENT_VERSION}" != "${VERSION}" ]; then
    echo "  Removing old image: ${AGENT_IMAGE}:${CURRENT_VERSION}"
    docker rmi "${AGENT_IMAGE}:${CURRENT_VERSION}" 2>/dev/null || true
fi

# ── Write configuration ──────────────────────────────────────────
echo "[5/7] Writing configuration..."
cat > "${ENV_FILE}" << EOF
# Datafye Agent Configuration
# Version: ${VERSION}
# Updated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
DATAFYE_AGENT_VERSION=${VERSION}
DATAFYE_AGENT_PORT=${AGENT_PORT}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
EOF
chmod 600 "${ENV_FILE}"

echo "${VERSION}" > "${INSTALL_DIR}/version"

# ── Write start/stop scripts ─────────────────────────────────────
cat > "${INSTALL_DIR}/start.sh" << 'STARTUP'
#!/bin/bash
#
# Start the Datafye Agent container.
# Reads config from /opt/datafye/agent/agent.env
#

set -e

INSTALL_DIR="/opt/datafye/agent"
ENV_FILE="${INSTALL_DIR}/agent.env"
WORKSPACE_DIR="/home/datafye/workspace"
CONTAINER_NAME="datafye-agent"

if [ ! -f "${ENV_FILE}" ]; then
    echo "Error: ${ENV_FILE} not found. Run the installer first."
    exit 1
fi
source "${ENV_FILE}"

# Resolve Anthropic key: env file -> EC2 user data -> EC2 instance tags
if [ -z "${ANTHROPIC_API_KEY}" ]; then
    # Try EC2 IMDSv2 user data
    TOKEN=$(curl -s --connect-timeout 2 -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || true)
    if [ -n "$TOKEN" ]; then
        USER_DATA=$(curl -s --connect-timeout 2 -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/user-data" 2>/dev/null || true)
        ANTHROPIC_API_KEY=$(echo "$USER_DATA" | grep -oP 'ANTHROPIC_API_KEY=\K.*' || true)
        if [ -n "${ANTHROPIC_API_KEY}" ]; then
            echo "Loaded Anthropic key from EC2 user data"
        fi
    fi
fi

if [ -z "${ANTHROPIC_API_KEY}" ]; then
    echo "Error: ANTHROPIC_API_KEY not set."
    echo "Set it in ${ENV_FILE} or pass via EC2 user data."
    exit 1
fi

VERSION=$(cat "${INSTALL_DIR}/version")
IMAGE="datafye/datafye-agent:${VERSION}"

# Stop existing container if running
docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

echo "Starting ${CONTAINER_NAME} (${IMAGE})..."
docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    -p "${DATAFYE_AGENT_PORT}:${DATAFYE_AGENT_PORT}" \
    -e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
    -e "DATAFYE_AGENT_PORT=${DATAFYE_AGENT_PORT}" \
    -v "${WORKSPACE_DIR}:/home/datafye/workspace" \
    "${IMAGE}"

echo "Agent running on port ${DATAFYE_AGENT_PORT}"
STARTUP
chmod +x "${INSTALL_DIR}/start.sh"

cat > "${INSTALL_DIR}/stop.sh" << 'STOPSCRIPT'
#!/bin/bash
docker stop datafye-agent 2>/dev/null && docker rm datafye-agent 2>/dev/null
echo "Agent stopped."
STOPSCRIPT
chmod +x "${INSTALL_DIR}/stop.sh"

# ── Install and configure nginx ──────────────────────────────────
install_nginx() {
    if command -v nginx &> /dev/null; then
        echo "[6/7] nginx: already installed"
        return
    fi

    echo "[6/7] Installing nginx..."
    case $PLATFORM in
        amzn)
            yum install -y nginx
            ;;
        ubuntu|debian)
            apt-get install -y nginx
            ;;
    esac

    systemctl enable nginx
}

install_nginx

echo "[6/7] Configuring nginx..."

if [ -d /etc/nginx/sites-available ]; then
    NGINX_CONF_DIR="/etc/nginx/sites-available"
    NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"
elif [ -d /etc/nginx/conf.d ]; then
    NGINX_CONF_DIR="/etc/nginx/conf.d"
    NGINX_ENABLED_DIR=""
else
    mkdir -p /etc/nginx/conf.d
    NGINX_CONF_DIR="/etc/nginx/conf.d"
    NGINX_ENABLED_DIR=""
fi

cat > "${NGINX_CONF_DIR}/datafye-agent.conf" << NGINX
server {
    listen 80 default_server;
    server_name _;

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
    }

    # Frontend (installed by AMI builder, or served locally during dev)
    location / {
        root /var/www/datafye-agent;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX

if [ -n "${NGINX_ENABLED_DIR}" ]; then
    mkdir -p "${NGINX_ENABLED_DIR}"
    ln -sf "${NGINX_CONF_DIR}/datafye-agent.conf" "${NGINX_ENABLED_DIR}/datafye-agent.conf"
    rm -f "${NGINX_ENABLED_DIR}/default"
fi

# Placeholder frontend (AMI builder or manual install replaces this)
mkdir -p /var/www/datafye-agent
if [ ! -f /var/www/datafye-agent/js/app.js ]; then
    cat > /var/www/datafye-agent/index.html << 'PLACEHOLDER'
<!DOCTYPE html>
<html><body style="background:#0a0a0a;color:#e6edf3;font-family:monospace;padding:40px;">
<h2>Datafye Agent</h2>
<p>Backend is running. Frontend not installed on this instance.</p>
<p>For development, run the frontend locally and point it at this server's API.</p>
<p><a href="/health" style="color:#f59e0b;">Check health</a></p>
</body></html>
PLACEHOLDER
fi

nginx -t && systemctl restart nginx

# ── Install auto-upgrade check ────────────────────────────────────
echo "[7/8] Configuring auto-upgrade..."

# Copy the upgrade-check script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -f "${SCRIPT_DIR}/upgrade-check.sh" ]; then
    cp "${SCRIPT_DIR}/upgrade-check.sh" "${INSTALL_DIR}/upgrade-check.sh"
elif [ -f "${INSTALL_DIR}/upgrade-check.sh" ]; then
    : # Already installed from previous run
else
    # Fetch from downloads
    curl -fsSL "https://downloads.n5corp.com/datafye/agent/${VERSION}/upgrade-check.sh" \
        -o "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true
fi
chmod +x "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true

# Also keep a copy of the installer itself for self-upgrades
if [ -f "${SCRIPT_DIR}/install.sh" ]; then
    cp "${SCRIPT_DIR}/install.sh" "${INSTALL_DIR}/install.sh"
    chmod +x "${INSTALL_DIR}/install.sh"
fi

# Set up cron (every 5 minutes)
cat > /etc/cron.d/datafye-agent-upgrade << CRON
# Datafye Agent auto-upgrade check
*/5 * * * * root ${INSTALL_DIR}/upgrade-check.sh >> /var/log/datafye-agent-upgrade.log 2>&1
CRON
echo "  Auto-upgrade check: every 5 minutes"

# ── Start agent (unless AMI prep) ─────────────────────────────────
if [ "$AMI_PREP" = true ]; then
    echo "[8/8] AMI prep mode - skipping agent startup"
    echo ""
    echo "  The startup script reads ANTHROPIC_API_KEY from:"
    echo "    1. ${ENV_FILE}"
    echo "    2. EC2 user data (IMDSv2)"
else
    echo "[8/8] Starting agent..."
    if [ -z "$ANTHROPIC_API_KEY" ]; then
        echo ""
        echo "  WARNING: No Anthropic key provided."
        echo "  Set it in ${ENV_FILE} then run: ${INSTALL_DIR}/start.sh"
    else
        "${INSTALL_DIR}/start.sh"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo "================================================"
if [ "$IS_UPGRADE" = true ]; then
    echo "  Datafye Agent upgraded: ${CURRENT_VERSION} -> ${VERSION}"
else
    echo "  Datafye Agent v${VERSION} installed"
fi
echo "================================================"
echo ""
echo "  Config:    ${ENV_FILE}"
echo "  Workspace: ${WORKSPACE_DIR}"
echo "  Start:     ${INSTALL_DIR}/start.sh"
echo "  Stop:      ${INSTALL_DIR}/stop.sh"
echo "  API:       http://localhost/v1/chat"
echo "  Health:    http://localhost/health"
echo ""
