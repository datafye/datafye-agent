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
