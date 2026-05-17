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
# If DATAFYE_AGENT_PINNED=true in agent.env (because the install used
# --version), this script exits silently — a pinned install must be
# upgraded manually.
#

set -e

INSTALL_DIR="/opt/datafye/agent"
ENV_FILE="${INSTALL_DIR}/agent.env"
VERSION_URL="https://downloads.n5corp.com/datafye/agent/latest/version.txt"
LOG_PREFIX="[datafye-agent-upgrade]"

# Check we're installed
if [ ! -f "${INSTALL_DIR}/version" ]; then
    exit 0
fi

# Respect pinning
if [ -f "${ENV_FILE}" ]; then
    PINNED=$(grep -oP '^DATAFYE_AGENT_PINNED=\K.*' "${ENV_FILE}" 2>/dev/null || true)
    if [ "${PINNED}" = "true" ]; then
        # Silent — this is expected for pinned installs
        exit 0
    fi
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
echo "${LOG_PREFIX} Fetching installer v${LATEST_VERSION} from downloads.n5corp.com..."

# Always fetch the latest installer (which has the target version baked in);
# do not reuse the local installer with --version, since that would change
# pinning semantics. Config (mode, credentials, DNS, port) is preserved
# automatically by the installer via agent.env.
curl -fsSL "https://downloads.n5corp.com/datafye/agent/${LATEST_VERSION}/install.sh" | bash
echo "${LOG_PREFIX} Upgrade complete: now running v${LATEST_VERSION}"
