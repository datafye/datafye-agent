#!/bin/bash
#
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

# Identity: production reads these from the EC2 instance's Name tag + instance ID
# via IMDS. Locally there's no IMDS, so we provide explicit env-var defaults.
# The instance-id value seeds the credentials store's encryption key — keep it
# stable across runs so the persisted credentials.bin stays decryptable.
export DATAFYE_AGENT_USERNAME="${DATAFYE_AGENT_USERNAME:-local-dev}"
export DATAFYE_AGENT_INSTANCE_ID="${DATAFYE_AGENT_INSTANCE_ID:-local-dev-instance}"

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
