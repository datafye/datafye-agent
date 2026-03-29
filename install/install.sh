#!/bin/bash
#
# Datafye Agent - Backend Installer / Upgrader
#
# Installs or upgrades the Datafye Agent backend on a Linux machine.
# Handles: Docker, agent container image, nginx reverse proxy, SSL.
#
# Usage:
#   # Fresh install
#   sudo ./install.sh --version 2.0.4 --dns agent.datafye.io --anthropic-key sk-ant-...
#
#   # Upgrade to new version (preserves credentials, DNS, workspace)
#   sudo ./install.sh --version 2.0.5
#
#   # AMI prep (no key, skips startup and SSL)
#   sudo ./install.sh --version 2.0.4 --ami-prep
#
#   # Dev mode (no SSL)
#   sudo ./install.sh --version 2.0.4 --no-ssl --anthropic-key sk-ant-...
#
#   # Force reinstall same version (useful for SNAPSHOT builds)
#   sudo ./install.sh --version 2.0.4-SNAPSHOT --force
#

set -e

# ── Defaults ──────────────────────────────────────────────────────
VERSION=""
DNS_NAME=""
ANTHROPIC_API_KEY=""
AMI_PREP=false
FORCE=false
INSTALL_SSL=true
AGENT_PORT=18780
AGENT_IMAGE="datafye/datafye-agent"
INSTALL_DIR="/opt/datafye/agent"
WORKSPACE_DIR="/home/datafye/workspace"

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
        --dns)           DNS_NAME="$2"; shift 2 ;;
        --anthropic-key) ANTHROPIC_API_KEY="$2"; shift 2 ;;
        --ami-prep)      AMI_PREP=true; INSTALL_SSL=false; shift ;;
        --force)         FORCE=true; shift ;;
        --no-ssl)        INSTALL_SSL=false; shift ;;
        --port)          AGENT_PORT="$2"; shift 2 ;;
        -h|--help)
            cat <<EOF
Datafye Agent Installer

Usage:
  install.sh --version <version> [OPTIONS]

Options:
  --version <ver>     Datafye platform version (required)
  --dns <name>        DNS name for the instance (e.g., agent.datafye.io)
  --anthropic-key <k> Anthropic API key (can be set later or via EC2 user data)
  --ami-prep          Prepare for AMI snapshot (skip startup and SSL)
  --no-ssl            Skip SSL/Let's Encrypt setup
  --force             Reinstall even if same version (useful for SNAPSHOT)
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

if [ "$IS_UPGRADE" = true ]; then
    echo ""
    info "================================================"
    info "  Datafye Agent Upgrade: ${CURRENT_VERSION} -> ${VERSION}"
    info "================================================"
else
    echo ""
    info "================================================"
    info "  Datafye Agent Install: v${VERSION}"
    info "================================================"
fi
echo ""

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
    # Preserve DNS from existing nginx config
    if [ -z "$DNS_NAME" ]; then
        DNS_NAME=$(grep -oP 'server_name\s+\K[^;]+' /etc/nginx/sites-available/datafye-agent.conf 2>/dev/null \
                || grep -oP 'server_name\s+\K[^;]+' /etc/nginx/conf.d/datafye-agent.conf 2>/dev/null \
                || true)
        [ "$DNS_NAME" = "_" ] && DNS_NAME=""
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
info "[1/9] Platform: ${PLATFORM}"

# ── Step 2: Install Docker ───────────────────────────────────────
install_docker() {
    if command -v docker &> /dev/null; then
        if docker info &>/dev/null; then
            ok "Docker: $(docker --version)"
            return
        fi
        # Docker installed but daemon not running
        systemctl start docker 2>/dev/null || true
        if docker info &>/dev/null; then
            ok "Docker daemon started"
            return
        fi
    fi

    info "[2/9] Installing Docker..."
    case $PLATFORM in
        amzn)
            yum install -y docker
            ;;
        ubuntu|debian)
            apt-get update -qq
            apt-get install -y -qq docker.io
            ;;
        rhel|centos|fedora|rocky|almalinux)
            yum install -y yum-utils
            yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
            yum install -y docker-ce docker-ce-cli containerd.io
            ;;
        *)
            error "Unsupported platform: ${PLATFORM}. Install Docker manually."
            exit 1
            ;;
    esac

    systemctl enable docker
    systemctl start docker

    if docker info &>/dev/null; then
        ok "Docker installed: $(docker --version)"
    else
        error "Docker installation failed or daemon won't start"
        exit 1
    fi
}

install_docker

# ── Step 3: Create directories and user ──────────────────────────
info "[3/9] Directories and user..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${WORKSPACE_DIR}"

if ! id -u datafye &>/dev/null; then
    useradd -u 1000 -m -d /home/datafye -s /bin/bash datafye
fi
chown -R datafye:datafye "${WORKSPACE_DIR}"
ok "Workspace: ${WORKSPACE_DIR}"

# ── Step 4: Stop existing container on upgrade ───────────────────
if [ "$IS_UPGRADE" = true ]; then
    info "[4/9] Stopping existing agent..."
    docker stop datafye-agent 2>/dev/null || true
    docker rm datafye-agent 2>/dev/null || true
    ok "Old container removed"
else
    info "[4/9] Fresh install (no existing container)"
fi

# ── Step 5: Pull agent image ─────────────────────────────────────
info "[5/9] Pulling ${AGENT_IMAGE}:${VERSION}..."
docker pull "${AGENT_IMAGE}:${VERSION}"
ok "Image pulled"

# Clean up old image if upgrading to different version
if [ "$IS_UPGRADE" = true ] && [ "${CURRENT_VERSION}" != "${VERSION}" ]; then
    docker rmi "${AGENT_IMAGE}:${CURRENT_VERSION}" 2>/dev/null || true
fi

# ── Step 6: Write configuration ──────────────────────────────────
info "[6/9] Writing configuration..."
cat > "${ENV_FILE}" << EOF
# Datafye Agent Configuration
# Version: ${VERSION}
# Updated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
DATAFYE_AGENT_VERSION=${VERSION}
DATAFYE_AGENT_PORT=${AGENT_PORT}
DATAFYE_AGENT_ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
EOF
chmod 600 "${ENV_FILE}"
echo "${VERSION}" > "${INSTALL_DIR}/version"
ok "Config: ${ENV_FILE}"

# ── Write start/stop scripts ─────────────────────────────────────
cat > "${INSTALL_DIR}/start.sh" << 'STARTUP'
#!/bin/bash
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

# Resolve Anthropic key: env file -> EC2 user data (IMDSv2)
if [ -z "${DATAFYE_AGENT_ANTHROPIC_API_KEY}" ]; then
    TOKEN=$(curl -s --connect-timeout 2 -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || true)
    if [ -n "$TOKEN" ]; then
        USER_DATA=$(curl -s --connect-timeout 2 \
            -H "X-aws-ec2-metadata-token: $TOKEN" \
            "http://169.254.169.254/latest/user-data" 2>/dev/null || true)
        DATAFYE_AGENT_ANTHROPIC_API_KEY=$(echo "$USER_DATA" | grep -oP 'DATAFYE_AGENT_ANTHROPIC_API_KEY=\K.*' || true)
        [ -n "${DATAFYE_AGENT_ANTHROPIC_API_KEY}" ] && echo "Loaded Anthropic key from EC2 user data"
    fi
fi

if [ -z "${DATAFYE_AGENT_ANTHROPIC_API_KEY}" ]; then
    echo "Error: DATAFYE_AGENT_ANTHROPIC_API_KEY not set."
    echo "Set it in ${ENV_FILE} or pass via EC2 user data."
    exit 1
fi

VERSION=$(cat "${INSTALL_DIR}/version")
IMAGE="datafye/datafye-agent:${VERSION}"

docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true

echo "Starting ${CONTAINER_NAME} (${IMAGE})..."
docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    -p "${DATAFYE_AGENT_PORT}:${DATAFYE_AGENT_PORT}" \
    -e "DATAFYE_AGENT_ANTHROPIC_API_KEY=${DATAFYE_AGENT_ANTHROPIC_API_KEY}" \
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

# ── Step 7: Install and configure nginx ──────────────────────────
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

info "[7/9] Configuring nginx..."
install_nginx

# Determine nginx config directory and ensure sites-enabled is included
if [ -d /etc/nginx/sites-available ]; then
    NGINX_CONF_DIR="/etc/nginx/sites-available"
    NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"
else
    mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
    NGINX_CONF_DIR="/etc/nginx/sites-available"
    NGINX_ENABLED_DIR="/etc/nginx/sites-enabled"
    # Add include directive to nginx.conf if not present
    if ! grep -q "sites-enabled" /etc/nginx/nginx.conf 2>/dev/null; then
        sed -i '/http {/a \    include /etc/nginx/sites-enabled/*;' /etc/nginx/nginx.conf
    fi
fi

# Use DNS name or wildcard
NGINX_SERVER_NAME="${DNS_NAME:-_}"

cat > "${NGINX_CONF_DIR}/datafye-agent.conf" << NGINX
# Datafye Agent reverse proxy
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

    # Frontend (installed by AMI builder, or served locally during dev)
    location / {
        root /var/www/datafye-agent;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }
}
NGINX

# Enable site
mkdir -p "${NGINX_ENABLED_DIR}"
ln -sf "${NGINX_CONF_DIR}/datafye-agent.conf" "${NGINX_ENABLED_DIR}/datafye-agent.conf"
rm -f "${NGINX_ENABLED_DIR}/default"

# Placeholder frontend (AMI builder replaces this)
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

if nginx -t 2>/dev/null; then
    systemctl restart nginx
    ok "nginx configured and restarted"
else
    error "nginx configuration test failed"
    exit 1
fi

# ── Step 8: SSL with Let's Encrypt ────────────────────────────────
if [ "$INSTALL_SSL" = true ] && [ -n "$DNS_NAME" ] && [ "$DNS_NAME" != "_" ]; then
    info "[8/9] Setting up SSL with Let's Encrypt for ${DNS_NAME}..."

    # Install certbot
    case $PLATFORM in
        amzn|rhel|centos|fedora|rocky|almalinux)
            yum install -y certbot python3-certbot-nginx
            ;;
        ubuntu|debian)
            apt-get install -y -qq certbot python3-certbot-nginx
            ;;
    esac

    # Obtain certificate
    certbot --nginx -d "${DNS_NAME}" \
        --non-interactive \
        --agree-tos \
        --register-unsafely-without-email \
        --redirect

    ok "SSL configured for ${DNS_NAME}"
    ok "Auto-renewal via certbot systemd timer"
elif [ "$INSTALL_SSL" = true ] && [ -z "$DNS_NAME" ]; then
    warn "[8/9] SSL skipped (no --dns name provided)"
    warn "  Run later: certbot --nginx -d <your-domain>"
else
    info "[8/9] SSL skipped (--no-ssl or --ami-prep)"
fi

# ── Step 9a: Auto-upgrade ─────────────────────────────────────────
info "[9/9] Configuring auto-upgrade..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -f "${SCRIPT_DIR}/upgrade-check.sh" ]; then
    cp "${SCRIPT_DIR}/upgrade-check.sh" "${INSTALL_DIR}/upgrade-check.sh"
elif [ ! -f "${INSTALL_DIR}/upgrade-check.sh" ]; then
    curl -fsSL "https://downloads.n5corp.com/datafye/agent/${VERSION}/upgrade-check.sh" \
        -o "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true
fi
chmod +x "${INSTALL_DIR}/upgrade-check.sh" 2>/dev/null || true

if [ -f "${SCRIPT_DIR}/install.sh" ]; then
    cp "${SCRIPT_DIR}/install.sh" "${INSTALL_DIR}/install.sh"
    chmod +x "${INSTALL_DIR}/install.sh"
fi

cat > /etc/cron.d/datafye-agent-upgrade << CRON
# Datafye Agent auto-upgrade check (every 5 minutes)
*/5 * * * * root ${INSTALL_DIR}/upgrade-check.sh >> /var/log/datafye-agent-upgrade.log 2>&1
CRON
ok "Auto-upgrade: every 5 minutes"

# ── Step 9b: Start agent (unless AMI prep) ────────────────────────
if [ "$AMI_PREP" = true ]; then
    info "AMI prep mode - skipping agent startup"
else
    if [ -z "$ANTHROPIC_API_KEY" ]; then
        warn "No Anthropic key provided."
        warn "Set it in ${ENV_FILE} then run: ${INSTALL_DIR}/start.sh"
    else
        info "Starting agent..."
        "${INSTALL_DIR}/start.sh"

        # Verify agent is running
        sleep 3
        if docker ps --format '{{.Names}}' | grep -q "^datafye-agent$"; then
            ok "Agent container is running"

            # Health check
            HEALTH=$(curl -sf --connect-timeout 5 "http://127.0.0.1:${AGENT_PORT}/health" 2>/dev/null || true)
            if [ -n "$HEALTH" ]; then
                ok "Agent health check passed"
            else
                warn "Agent started but health check not responding yet (may still be initializing)"
            fi
        else
            error "Agent container failed to start"
            error "Check logs: docker logs datafye-agent"
        fi
    fi
fi

# ── Summary ───────────────────────────────────────────────────────
PROTOCOL="http"
[ "$INSTALL_SSL" = true ] && [ -n "$DNS_NAME" ] && PROTOCOL="https"
DISPLAY_HOST="${DNS_NAME:-localhost}"

echo ""
info "================================================"
if [ "$IS_UPGRADE" = true ]; then
    ok "Datafye Agent upgraded: ${CURRENT_VERSION} -> ${VERSION}"
else
    ok "Datafye Agent v${VERSION} installed"
fi
info "================================================"
echo ""
info "  Config:    ${ENV_FILE}"
info "  Workspace: ${WORKSPACE_DIR}"
info "  Start:     ${INSTALL_DIR}/start.sh"
info "  Stop:      ${INSTALL_DIR}/stop.sh"
echo ""
info "  API:       ${PROTOCOL}://${DISPLAY_HOST}/v1/chat"
info "  Health:    ${PROTOCOL}://${DISPLAY_HOST}/health"
echo ""
info "  Logs:      docker logs datafye-agent"
info "  Status:    docker ps --filter name=datafye-agent"
info "  Upgrade:   /var/log/datafye-agent-upgrade.log"
echo ""
