#!/bin/bash
#
# Datafye Agent - Auto-Upgrade Check
#
# Checks downloads.n5corp.com for the latest agent version and upgrades
# if the installed version is behind. Designed to run via cron or systemd timer.
#
# Setup (cron, every 5 minutes):
#   echo "*/5 * * * * root /opt/datafye/agent/upgrade-check.sh >> /var/log/datafye-agent-upgrade.log 2>&1" \
#     > /etc/cron.d/datafye-agent-upgrade
#
# The version file is published by the agent build pipeline to:
#   https://downloads.n5corp.com/datafye/agent/latest/version.txt
#

set -e

INSTALL_DIR="/opt/datafye/agent"
VERSION_URL="https://downloads.n5corp.com/datafye/agent/latest/version.txt"
LOG_PREFIX="[datafye-agent-upgrade]"

# Check we're installed
if [ ! -f "${INSTALL_DIR}/version" ]; then
    exit 0
fi

CURRENT_VERSION=$(cat "${INSTALL_DIR}/version")

# Fetch latest version (quiet, short timeout)
LATEST_VERSION=$(curl -fsSL --connect-timeout 5 --max-time 10 "${VERSION_URL}" 2>/dev/null | tr -d '[:space:]')

if [ -z "${LATEST_VERSION}" ]; then
    echo "${LOG_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ) Could not fetch latest version"
    exit 0
fi

# Compare
if [ "${CURRENT_VERSION}" = "${LATEST_VERSION}" ]; then
    # Up to date, silent
    exit 0
fi

echo "${LOG_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ) Upgrade available: ${CURRENT_VERSION} -> ${LATEST_VERSION}"

# Run the installer in upgrade mode
# The installer preserves credentials and workspace automatically
INSTALLER="${INSTALL_DIR}/install.sh"

if [ -f "${INSTALLER}" ]; then
    echo "${LOG_PREFIX} Running installer for v${LATEST_VERSION}..."
    bash "${INSTALLER}" --version "${LATEST_VERSION}"
    echo "${LOG_PREFIX} Upgrade complete: now running v${LATEST_VERSION}"
else
    # Installer not on disk — fetch it from downloads
    echo "${LOG_PREFIX} Fetching installer from downloads.n5corp.com..."
    curl -fsSL "https://downloads.n5corp.com/datafye/agent/${LATEST_VERSION}/install.sh" | bash -s -- --version "${LATEST_VERSION}"
    echo "${LOG_PREFIX} Upgrade complete: now running v${LATEST_VERSION}"
fi
