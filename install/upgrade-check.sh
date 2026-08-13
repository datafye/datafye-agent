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
# if the installed version is behind. Designed to run via cron every 1 minute
# UNDER flock (so a tick can't fire while an upgrade is in progress).
#
# NEVER upgrades mid-turn. Before it downloads/runs the installer it gates on the
# agent's own /health: it proceeds only when the agent is idle (running_jobs==0),
# no live preview is exposed (active_proxied_apps empty), and the user has been
# away for at least DATAFYE_UPGRADE_INACTIVITY_WINDOW seconds (last_chat_activity_at,
# bumped by chat turns AND the SPA's visible-tab presence ping). Otherwise it
# defers and retries on the next tick.
#
# Setup (installed by install_template.sh):
#   * * * * * root /usr/bin/flock -n /run/lock/datafye-agent-upgrade.lock \
#     /opt/datafye/agent/upgrade-check.sh >> /var/log/datafye-agent-upgrade.log 2>&1
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

STATE_FILE="${INSTALL_DIR}/.upgrade-check-state"
# Consecutive failed install attempts, and the earliest epoch second at which
# another may be tried. See the backoff note further down.
FAIL_FILE="${INSTALL_DIR}/.upgrade-check-failures"
RETRY_FILE="${INSTALL_DIR}/.upgrade-check-retry-after"

# Log a decision ONLY when it changes from the last one. This runs every
# minute, so logging every outcome would bury the interesting lines in
# thousands of no-ops -- but logging nothing is what let a DISABLED check look
# exactly like a working idle one (DAT-187): an empty log was consistent with
# "pinned and standing down" and with "checked, already current", and there was
# no way to tell which from outside. One line per state change gives a durable
# trace at effectively zero volume.
note_state() {
    local state="$1" msg="$2" prev=""
    [ -f "${STATE_FILE}" ] && prev=$(cat "${STATE_FILE}" 2>/dev/null || true)
    if [ "${state}" != "${prev}" ]; then
        echo "${LOG_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ) ${msg}"
        printf '%s' "${state}" > "${STATE_FILE}" 2>/dev/null || true
    fi
}

# Check we're installed
if [ ! -f "${INSTALL_DIR}/version" ]; then
    exit 0
fi

# Respect pinning
if [ -f "${ENV_FILE}" ]; then
    PINNED=$(grep -oP '^DATAFYE_AGENT_PINNED=\K.*' "${ENV_FILE}" 2>/dev/null || true)
    if [ "${PINNED}" = "true" ]; then
        note_state "pinned" "Auto-upgrade DISABLED (DATAFYE_AGENT_PINNED=true); holding $(cat "${INSTALL_DIR}/version" 2>/dev/null). Clear the pin in ${ENV_FILE} to re-enable."
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
    note_state "current:${CURRENT_VERSION}" "Auto-upgrade ACTIVE; already at ${CURRENT_VERSION} (latest)."
    exit 0
fi

echo "${LOG_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ) Upgrade available: ${CURRENT_VERSION} -> ${LATEST_VERSION}"

# ── Never-mid-turn gate ───────────────────────────────────────────
# Proceed only when the agent is genuinely idle: no turn running, no live
# preview exposed, and the user has been away (no chat/presence) for at least
# the inactivity window. A turn's running_jobs stays >0 for the whole
# background turn even if the SPA disconnected, so this can't race a parked
# turn. If /health is unreachable there is no turn to protect (and an upgrade
# may fix a down agent), so we proceed.
AGENT_PORT=18780
if [ -f "${ENV_FILE}" ]; then
    _p=$(grep -oP '^DATAFYE_AGENT_PORT=\K.*' "${ENV_FILE}" 2>/dev/null || true)
    [ -n "${_p}" ] && AGENT_PORT="${_p}"
fi
WINDOW_SECONDS="${DATAFYE_UPGRADE_INACTIVITY_WINDOW:-120}"

health_json=$(curl -fsS --connect-timeout 3 --max-time 5 "http://127.0.0.1:${AGENT_PORT}/health" 2>/dev/null || true)
if [ -n "${health_json}" ]; then
    verdict=$(printf '%s' "${health_json}" | python3 -c '
import sys, json, time
try:
    h = json.load(sys.stdin)
except Exception:
    print("PROCEED unparseable-health"); sys.exit(0)
window_ms = int(sys.argv[1]) * 1000
now_ms = int(time.time() * 1000)
rj = h.get("running_jobs", 0) or 0
apps = h.get("active_proxied_apps") or []
last = h.get("last_chat_activity_at") or 0
if rj > 0:
    print("DEFER running_jobs=%d" % rj); sys.exit(0)
if apps:
    print("DEFER live_preview=%d" % len(apps)); sys.exit(0)
idle_ms = (now_ms - last) if last else window_ms   # never active => treat as idle
if idle_ms < window_ms:
    print("DEFER active_recently idle_ms=%d" % idle_ms); sys.exit(0)
print("PROCEED idle_ms=%d" % idle_ms)
' "${WINDOW_SECONDS}" 2>/dev/null || echo "DEFER health-parse-failed")
    case "${verdict}" in
        PROCEED*) echo "${LOG_PREFIX} gate passed (${verdict})" ;;
        *)        echo "${LOG_PREFIX} upgrade deferred (${verdict}); will retry next tick"; exit 0 ;;
    esac
else
    echo "${LOG_PREFIX} agent /health unreachable; proceeding with upgrade"
fi

# ── Jitter ────────────────────────────────────────────────────────
# downloads.n5corp.com is a single origin (no CDN today); spread the download
# herd when many agents upgrade in the same idle window. Held under flock, so a
# 1-min tick can't overlap this. Set DATAFYE_UPGRADE_JITTER_SECONDS=0 to disable.
JITTER_MAX="${DATAFYE_UPGRADE_JITTER_SECONDS:-60}"
if [ "${JITTER_MAX}" -gt 0 ] 2>/dev/null; then
    j=$(( RANDOM % (JITTER_MAX + 1) ))
    echo "${LOG_PREFIX} jittering ${j}s before download"
    sleep "${j}"
fi

# ── Failure backoff ──────────────────────────────────────────────
# ⚠️ This cron runs EVERY MINUTE, and an install that fails partway leaves the
# agent stopped -- so /health goes unreachable, which this script reads as
# "nothing to protect, proceed". Without a brake those two rules compose into an
# infinite loop: fail, retry a minute later, fail identically, forever. Observed
# live on a box whose agent tree had non-root ownership; git refused it at the
# same step on every one of dozens of attempts, and the agent stayed down
# throughout.
#
# So back off on repeated failure: 1, 5, 15, 30, then 60 minutes. A genuinely
# transient fault (a download blip) still recovers on the next tick, while a
# deterministic one stops consuming the box and stops burying the journal in
# identical stack traces. It never gives up entirely -- a fix published upstream
# must still be able to reach a wedged box unattended.
_fail_count=0
[ -f "${FAIL_FILE}" ] && _fail_count=$(cat "${FAIL_FILE}" 2>/dev/null || echo 0)
case "${_fail_count}" in ''|*[!0-9]*) _fail_count=0 ;; esac

if [ -f "${RETRY_FILE}" ]; then
    _retry_after=$(cat "${RETRY_FILE}" 2>/dev/null || echo 0)
    case "${_retry_after}" in ''|*[!0-9]*) _retry_after=0 ;; esac
    _now=$(date +%s)
    if [ "${_now}" -lt "${_retry_after}" ]; then
        # Deliberately silent: this fires every minute and the reason was
        # already logged loudly when the failure happened.
        exit 0
    fi
fi

echo "${LOG_PREFIX} Fetching installer v${LATEST_VERSION} from downloads.n5corp.com..."

# Always fetch the latest installer (which has the target version baked in);
# do not reuse the local installer with --version, since that would change
# pinning semantics. Config (mode, credentials, DNS, port) is preserved
# automatically by the installer via agent.env. DATAFYE_AUTO_UPGRADE=1 arms the
# installer's own last-moment mid-turn re-check.
#
# The tail is a BRACE GROUP so bash parses the upgrade, the log line, and the
# exit as ONE compound command before running any of it — and then never reads
# from this file again. The installer we are invoking REPLACES this very script;
# bash reads a script lazily by byte offset, so a replacement on the same inode
# would leave the shell resuming mid-line in the new file and dying with a bogus
# "syntax error near unexpected token" after a perfectly successful upgrade.
# (The installer also swaps the file atomically via mv, which keeps our original
# inode alive. This is the belt to that pair of braces.)
{
    if curl -fsSL "https://downloads.n5corp.com/datafye/agent/${LATEST_VERSION}/install.sh" | DATAFYE_AUTO_UPGRADE=1 bash; then
        rm -f "${FAIL_FILE}" "${RETRY_FILE}" 2>/dev/null || true
        echo "${LOG_PREFIX} Upgrade complete: now running v${LATEST_VERSION}"
        exit 0
    fi
    # The installer restarts the previous agent on its own failure, so the box
    # should still be serving the OLD version here.
    _fail_count=$(( _fail_count + 1 ))
    case "${_fail_count}" in
        1) _wait=60 ;; 2) _wait=300 ;; 3) _wait=900 ;; 4) _wait=1800 ;; *) _wait=3600 ;;
    esac
    printf '%s' "${_fail_count}" > "${FAIL_FILE}" 2>/dev/null || true
    printf '%s' "$(( $(date +%s) + _wait ))" > "${RETRY_FILE}" 2>/dev/null || true
    echo "${LOG_PREFIX} Upgrade to v${LATEST_VERSION} FAILED (attempt ${_fail_count}); next attempt in $(( _wait / 60 ))m"
    exit 1
}
