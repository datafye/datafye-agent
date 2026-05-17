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
# Datafye Agent - First Boot Script (Standalone/Marketplace)
#
# Runs on first boot of a standalone AMI. Reads configuration from EC2 user data
# and runs the full installer. This script is a one-shot systemd service that
# only runs if /opt/datafye/agent/version does not exist (i.e., first boot).
#
# Expected EC2 user data (key=value format, one per line):
#   DATAFYE_AGENT_VERSION=2.0.4          (required)
#   DATAFYE_AGENT_ANTHROPIC_API_KEY=sk-ant-...  (required)
#   DATAFYE_AGENT_DNS=agent.mycompany.com       (optional, enables SSL)
#

set -e

LOG_PREFIX="[datafye-first-boot]"
log() { echo "${LOG_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

log "Starting first boot setup..."

# ── Read EC2 user data (IMDSv2) ──────────────────────────────────
TOKEN=$(curl -s --connect-timeout 5 -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || true)

if [ -z "$TOKEN" ]; then
    log "ERROR: Could not obtain IMDS token. Not running on EC2?"
    exit 1
fi

USER_DATA=$(curl -s --connect-timeout 5 \
    -H "X-aws-ec2-metadata-token: $TOKEN" \
    "http://169.254.169.254/latest/user-data" 2>/dev/null || true)

if [ -z "$USER_DATA" ]; then
    log "ERROR: No user data found. Provide DATAFYE_AGENT_VERSION and DATAFYE_AGENT_ANTHROPIC_API_KEY."
    exit 1
fi

# Parse user data
VERSION=$(echo "$USER_DATA" | grep -oP '^DATAFYE_AGENT_VERSION=\K.*' || true)
ANTHROPIC_KEY=$(echo "$USER_DATA" | grep -oP '^DATAFYE_AGENT_ANTHROPIC_API_KEY=\K.*' || true)
DNS_NAME=$(echo "$USER_DATA" | grep -oP '^DATAFYE_AGENT_DNS=\K.*' || true)

if [ -z "$VERSION" ]; then
    log "ERROR: DATAFYE_AGENT_VERSION not found in user data"
    exit 1
fi

if [ -z "$ANTHROPIC_KEY" ]; then
    log "ERROR: DATAFYE_AGENT_ANTHROPIC_API_KEY not found in user data"
    exit 1
fi

log "Version: ${VERSION}"
log "DNS: ${DNS_NAME:-none}"

# ── Download and run installer ───────────────────────────────────
INSTALLER_URL="https://downloads.n5corp.com/datafye/agent/${VERSION}/install.sh"

log "Downloading installer from ${INSTALLER_URL}..."
curl -fsSL "${INSTALLER_URL}" -o /tmp/datafye-agent-install.sh
chmod +x /tmp/datafye-agent-install.sh

INSTALL_ARGS="--version ${VERSION} --mode standalone --anthropic-key ${ANTHROPIC_KEY}"
if [ -n "$DNS_NAME" ]; then
    INSTALL_ARGS="${INSTALL_ARGS} --dns ${DNS_NAME}"
fi

log "Running installer..."
bash /tmp/datafye-agent-install.sh ${INSTALL_ARGS}

rm -f /tmp/datafye-agent-install.sh

log "First boot setup complete."
