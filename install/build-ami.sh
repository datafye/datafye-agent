#!/bin/bash
#
# Datafye Agent - AMI Builder
#
# Runs the backend installer + installs the frontend, preparing the machine
# for an AMI snapshot. Run this on a fresh EC2 instance, then create the AMI.
#
# Usage:
#   sudo ./build-ami.sh --version 2.0.4
#
# Prerequisites:
#   - Fresh Amazon Linux 2023 or Ubuntu EC2 instance
#   - Internet access (to pull Docker images, clone repos)
#   - This script must be run as root
#

set -e

VERSION=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --version) VERSION="$2"; shift 2 ;;
        *)         echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$VERSION" ]; then
    echo "Error: --version is required"
    echo "Usage: build-ami.sh --version <version>"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "================================================"
echo "  Datafye Agent AMI Builder v${VERSION}"
echo "================================================"
echo ""

# ── Step 1: Run backend installer in AMI prep mode ────────────────
echo "=== Step 1: Installing backend ==="
bash "${SCRIPT_DIR}/install.sh" --version "${VERSION}" --ami-prep

# ── Step 2: Install frontend ─────────────────────────────────────
echo ""
echo "=== Step 2: Installing frontend ==="

FRONTEND_DIR="/var/www/datafye-agent"
FRONTEND_REPO="https://github.com/datafye/datafye-agent-app.git"

# Clone frontend at release tag (fall back to main)
rm -rf /tmp/datafye-agent-app
git clone --depth 1 --branch "v${VERSION}" "${FRONTEND_REPO}" /tmp/datafye-agent-app 2>/dev/null \
    || git clone --depth 1 "${FRONTEND_REPO}" /tmp/datafye-agent-app

# Copy static files to nginx web root
rm -rf "${FRONTEND_DIR}"
mkdir -p "${FRONTEND_DIR}"
cp -r /tmp/datafye-agent-app/index.html "${FRONTEND_DIR}/"
cp -r /tmp/datafye-agent-app/styles "${FRONTEND_DIR}/"
cp -r /tmp/datafye-agent-app/js "${FRONTEND_DIR}/"
cp -r /tmp/datafye-agent-app/assets "${FRONTEND_DIR}/"
# Copy lib directory if it exists (charting libraries etc.)
[ -d /tmp/datafye-agent-app/lib ] && cp -r /tmp/datafye-agent-app/lib "${FRONTEND_DIR}/"

rm -rf /tmp/datafye-agent-app

echo "  Frontend installed at ${FRONTEND_DIR}"

# ── Step 3: Configure auto-start on boot ──────────────────────────
echo ""
echo "=== Step 3: Configuring auto-start ==="

cat > /etc/systemd/system/datafye-agent.service << EOF
[Unit]
Description=Datafye Agent Service
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/datafye/agent/start.sh
ExecStop=/opt/datafye/agent/stop.sh

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable datafye-agent.service
echo "  Systemd service enabled (datafye-agent.service)"

# ── Step 4: Clean up for AMI ─────────────────────────────────────
echo ""
echo "=== Step 4: Cleaning up for AMI snapshot ==="

# Clear the Anthropic key (will be injected at launch)
sed -i 's/^ANTHROPIC_API_KEY=.*/ANTHROPIC_API_KEY=/' /opt/datafye/agent/agent.env

# Clear logs
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true
rm -rf /var/log/nginx/*.log
docker system prune -f 2>/dev/null || true

# Clear shell history
> /root/.bash_history
history -c 2>/dev/null || true

echo ""
echo "================================================"
echo "  AMI ready for snapshot"
echo "================================================"
echo ""
echo "  Version: ${VERSION}"
echo "  Backend: Docker image pre-pulled"
echo "  Frontend: ${FRONTEND_DIR}"
echo "  Auto-start: systemd (datafye-agent.service)"
echo ""
echo "  On launch, the agent reads ANTHROPIC_API_KEY from:"
echo "    1. /opt/datafye/agent/agent.env"
echo "    2. EC2 user data (ANTHROPIC_API_KEY=sk-ant-...)"
echo ""
echo "  Next: Create AMI from this instance."
echo ""
