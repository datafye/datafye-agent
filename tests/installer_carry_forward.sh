#!/usr/bin/env bash
#
# Every installer setting that is still meaningful after the install must survive
# an upgrade (DAT-449). An upgrade runs unattended from a root cron job with no
# arguments, so a setting that is not carried forward is not "defaulted" -- it is
# SILENTLY REVERTED on a box somebody deliberately configured.
#
# This checks the two halves that have to agree:
#   1. the carry-forward block reads back everything agent.env persists
#   2. the one-shot flags are deliberately NOT carried forward
#
# Pure text analysis of install_template.sh, so it runs anywhere with no box, no
# root and no Docker. Run: bash tests/installer_carry_forward.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${HERE}/../install/install_template.sh"
fails=0

ok()   { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

[ -f "${TEMPLATE}" ] || { echo "cannot find ${TEMPLATE}"; exit 2; }

# The env-file heredoc: what the installer persists.
persisted=$(awk '/^cat > "\$\{ENV_FILE\}"/,/^EOF$/' "${TEMPLATE}" \
            | grep -oE '^[A-Z][A-Z0-9_]*=' | tr -d '=' | sort -u)
# The carry-forward block: what it reads back.
carried=$(awk '/Preserve existing config on upgrade/,/^# ── Validate mode/' "${TEMPLATE}" \
          | grep -oE 'existing_env_var [A-Z][A-Z0-9_]*' | awk '{print $2}' | sort -u)

echo "== every persisted setting is read back on upgrade =="

# Values derived from the install itself rather than from operator intent. These
# describe the build being installed, not how this box was configured, so the NEW
# value must win -- carrying them forward would pin a box to its first version.
derived_regex='^DATAFYE_AGENT_(VERSION|CLI_PATH|WORKSPACE|DOCS_DIR|SAMPLES_DIR)$'

for key in ${persisted}; do
    if [[ "${key}" =~ ${derived_regex} ]]; then
        continue
    fi
    if grep -qx "${key}" <<< "${carried}"; then
        ok "${key} is carried forward"
    else
        fail "${key} is persisted but NEVER read back -- an upgrade silently reverts it"
    fi
done

echo "== the settings that bit us are specifically covered =="
for key in DATAFYE_AGENT_PINNED DATAFYE_ACCOUNTS_URL DATAFYE_AGENT_GITHUB_TOKEN DATAFYE_AGENT_API_MCP_SERVER; do
    grep -qx "${key}" <<< "${persisted}" || fail "${key} is not persisted at all"
    grep -qx "${key}" <<< "${carried}"   || fail "${key} is not carried forward"
done
grep -qx DATAFYE_AGENT_PINNED <<< "${persisted}" \
  && grep -qx DATAFYE_AGENT_PINNED <<< "${carried}" \
  && ok "--pin survives a re-run (it used to silently un-pin a pinned box)"
grep -qx DATAFYE_ACCOUNTS_URL <<< "${carried}" \
  && ok "the accounts URL survives (its absence killed the foundry on every upgrade)"
grep -qx DATAFYE_AGENT_API_MCP_SERVER <<< "${carried}" \
  && ok "the MCP server choice survives (it is the switch back to npm, DAT-439)"
grep -q 'DATAFYE_AGENT_API_MCP_URL=' <(awk '/^cat > "\$\{ENV_FILE\}"/,/^EOF$/' "${TEMPLATE}") \
  && fail "agent.env pins DATAFYE_AGENT_API_MCP_URL, which overrides the MCP server switch"

echo "== a flag that exists must be persisted, or be a deliberate one-shot =="
# Flags about THIS invocation only. Carrying them forward would re-apply an
# instruction the operator gave once: --agent-source in particular would re-seed
# the agent tree from a local checkout on every later upgrade, overriding the
# version being installed.
oneshot='--force|--ami-cleanup|--version|--agent-source|--mode|--dns|--port|--pin|--github-token|--accounts-url|--mcp-server'
for flag in $(grep -oE '^\s+--[a-z-]+\)' "${TEMPLATE}" | tr -d ' )' | sort -u); do
    if [[ "${flag}" =~ ^(${oneshot})$ ]]; then
        ok "${flag} is accounted for"
    else
        fail "${flag} is a new flag that this test does not know about -- persist it or declare it one-shot"
    fi
done

echo "== sudo drops the environment, so step 11 must hand the URL over =="
if grep -q 'sudo -u datafye ${ACCOUNTS_URL:+DATAFYE_ACCOUNTS_URL=' "${TEMPLATE}"; then
    ok "the foundry upgrade is given DATAFYE_ACCOUNTS_URL explicitly"
else
    fail "the foundry upgrade does not pass DATAFYE_ACCOUNTS_URL -- persisting it achieves nothing"
fi

echo
if [ "${fails}" -eq 0 ]; then
    echo "PASS"
else
    echo "FAIL: ${fails} problem(s)"
fi
exit $(( fails > 0 ))
