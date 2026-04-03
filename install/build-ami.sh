#!/bin/bash
#
# Datafye Agent - AMI Builder
#
# Builds AMIs for hosted (sandbox) or standalone (marketplace) deployment.
#
# Usage:
#   # Hosted AMI (Rumi cloud sandbox - fully baked, ready on boot)
#   sudo ./build-ami.sh --version 2.0.4 --mode hosted
#
#   # Standalone AMI (marketplace - minimal, installs on first boot via user data)
#   sudo ./build-ami.sh --version 2.0.4 --mode standalone
#
# Prerequisites:
#   - Fresh Amazon Linux 2023 EC2 instance
#   - Internet access
#   - This script must be run as root
#

set -e

VERSION=""
MODE=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --version) VERSION="$2"; shift 2 ;;
        --mode)    MODE="$2"; shift 2 ;;
        *)         echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$VERSION" ] || [ -z "$MODE" ]; then
    echo "Error: --version and --mode are required"
    echo "Usage: build-ami.sh --version <version> --mode <hosted|standalone>"
    exit 1
fi

if [ "$MODE" != "hosted" ] && [ "$MODE" != "standalone" ]; then
    echo "Error: --mode must be 'hosted' or 'standalone'"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "================================================"
echo "  Datafye Agent AMI Builder v${VERSION}"
echo "  Mode: ${MODE}"
echo "================================================"
echo ""

if [ "$MODE" = "hosted" ]; then
    # ── Hosted AMI: fully baked ──────────────────────────────────
    # Run the full installer. Agent, CLI, docs, samples all installed.
    # On boot, systemd starts the agent which reads key from user data.

    echo "=== Building hosted AMI (fully baked) ==="
    bash "${SCRIPT_DIR}/install_template.sh" --version "${VERSION}" --mode hosted

    # Stop the agent (will start on boot via systemd)
    systemctl stop datafye-agent 2>/dev/null || true

else
    # ── Standalone AMI: minimal ──────────────────────────────────
    # Only install the first-boot script. Everything else is installed
    # on first boot from user data.

    echo "=== Building standalone AMI (first-boot install) ==="

    cp "${SCRIPT_DIR}/first-boot.sh" /opt/datafye-first-boot.sh
    chmod +x /opt/datafye-first-boot.sh

    # Install first-boot as a one-shot systemd service
    cat > /etc/systemd/system/datafye-first-boot.service << 'EOF'
[Unit]
Description=Datafye Agent First Boot Installer
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/opt/datafye/agent/version

[Service]
Type=oneshot
ExecStart=/opt/datafye-first-boot.sh
RemainAfterExit=no
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable datafye-first-boot.service

    echo "  First-boot script installed at /opt/datafye-first-boot.sh"
    echo "  Systemd service: datafye-first-boot.service"

fi

# ── Clean up for AMI snapshot ────────────────────────────────────
echo ""
echo "=== Cleaning up for AMI snapshot ==="

# Clear the Anthropic key if env file exists
if [ -f /opt/datafye/agent/agent.env ]; then
    sed -i 's/^DATAFYE_AGENT_ANTHROPIC_API_KEY=.*/DATAFYE_AGENT_ANTHROPIC_API_KEY=/' /opt/datafye/agent/agent.env
fi

# Clear logs
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true
rm -rf /var/log/nginx/*.log 2>/dev/null || true
rm -f /var/log/datafye-agent-upgrade.log
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
echo "  Mode:    ${MODE}"
echo ""

if [ "$MODE" = "hosted" ]; then
    echo "  Contents: Agent, CLI, docs, samples, Python venv (all pre-installed)"
    echo "  On boot:  systemd starts agent, reads Anthropic key from user data"
else
    echo "  Contents: First-boot script only (minimal)"
    echo "  On boot:  Reads user data, downloads and installs everything"
    echo ""
    echo "  Required user data:"
    echo "    DATAFYE_AGENT_VERSION=${VERSION}"
    echo "    DATAFYE_AGENT_ANTHROPIC_API_KEY=sk-ant-..."
    echo "    DATAFYE_AGENT_DNS=agent.mycompany.com  (optional, for SSL)"
fi
echo ""
echo "  Next: Create AMI from this instance."
echo ""
