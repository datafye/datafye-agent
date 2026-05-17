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

# Run the Datafye Agent locally for development.
#
# Prerequisites:
#   pip install -r requirements.txt
#
# Usage:
#   export DATAFYE_AGENT_ANTHROPIC_API_KEY="sk-ant-..."
#   ./run-local.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GITHUB_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Check for Anthropic key
if [ -z "${DATAFYE_AGENT_ANTHROPIC_API_KEY}" ]; then
    echo "Error: DATAFYE_AGENT_ANTHROPIC_API_KEY not set."
    echo "  export DATAFYE_AGENT_ANTHROPIC_API_KEY=\"sk-ant-...\""
    exit 1
fi

# Local paths (sibling repos)
export DATAFYE_AGENT_DOCS_DIR="${DATAFYE_AGENT_DOCS_DIR:-${GITHUB_ROOT}/datafye-docs}"
export DATAFYE_AGENT_SAMPLES_DIR="${DATAFYE_AGENT_SAMPLES_DIR:-${GITHUB_ROOT}/datafye-samples}"
export DATAFYE_AGENT_WORKSPACE="${DATAFYE_AGENT_WORKSPACE:-/tmp/datafye-workspace}"
export DATAFYE_AGENT_CLI_PATH="${DATAFYE_AGENT_CLI_PATH:-datafye}"
export DATAFYE_AGENT_PORT="${DATAFYE_AGENT_PORT:-18780}"
export DATAFYE_AGENT_MODEL="${DATAFYE_AGENT_MODEL:-opus}"

# Identity and the credentials-store key are no longer env-driven — they
# arrive from the accounts bootstrap push (POST /bootstrap). The agent
# starts "awaiting bootstrap"; for local dev, POST a bootstrap token to
# bring it live.

# Where the agent fetches JWKS from to verify inbound JWTs. Production
# defaults to https://accounts.datafye.io; for local dev point at a
# locally-running accounts service so chat endpoint auth can be tested.
export DATAFYE_AGENT_ACCOUNTS_URL="${DATAFYE_AGENT_ACCOUNTS_URL:-http://127.0.0.1:7779}"

# Create workspace
mkdir -p "${DATAFYE_AGENT_WORKSPACE}"

echo "Datafye Agent (local)"
echo "  Docs:      ${DATAFYE_AGENT_DOCS_DIR}"
echo "  Samples:   ${DATAFYE_AGENT_SAMPLES_DIR}"
echo "  Workspace: ${DATAFYE_AGENT_WORKSPACE}"
echo "  CLI:       ${DATAFYE_AGENT_CLI_PATH}"
echo "  Port:      ${DATAFYE_AGENT_PORT}"
echo "  Model:     ${DATAFYE_AGENT_MODEL}"
echo ""

cd "${SCRIPT_DIR}"
python main.py
