#!/bin/bash
#
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

#
# Datafye Agent - Foundry Boot Reconciler (DAT-199)
#
# Run by datafye-foundry-boot.service on EVERY boot, in EVERY installer mode.
# It is the single thing responsible for making the box's foundry match what
# the box is for.
#
# It replaces first-boot-foundry.sh (hosted-only first-boot provisioning) and
# supersedes the separate wake-restore hook DAT-124 proposed. Merging them is
# the whole point: TWO boot-time actors mutating one foundry is the u1
# incident, where a first-boot provision, an agent `start` three minutes into
# it, and an `apply` on top of both destroyed the environment twice. Adding a
# second boot-time actor would have moved that from every fresh boot to every
# wake.
#
# The name changed with the job. "first-boot" described one of the two things
# it does, and a unit whose name disagrees with its behaviour is exactly the
# drift that produced DAT-170 (an installer comment promising a first-boot step
# that did not exist).
#
# ── What it reads, and what it does not write ────────────────────
#
# Readiness is DERIVED from three inputs and stored as no single fact:
#
#   intent     the local cache of the last intent accounts pushed. ABSENT means
#              no deviation has ever been recorded, which for a sandbox means
#              RUNNING -- the box exists to host a foundry.
#   observed   interrogated on demand: are the applications ANSWERING.
#   in flight  the DAT-196 operation lock, plus the DAT-183 markers that report
#              it.
#
# It ANNOUNCES its work by being an ordinary CLI caller rather than by opening a
# second channel: `provision` and `start` each write their own DAT-183 marker
# for their duration, so a boot-time provision is visible as in-flight work
# instead of the box looking idle for seventeen minutes. That is also what
# keeps it from being dormed out from under itself once DAT-184 consumes the
# marker. Nothing here needs to change when it does.
#
# It writes NO state file. An earlier cut of this work had every lifecycle
# command record the environment's desired state; that shipped and was reverted
# (datafye-deploy PR #11). The bug it produced: a human SSHes in to debug and
# runs `foundry local stop`, the engine records intended=stopped, and this unit
# then leaves the foundry down on every subsequent boot -- a debugging action
# promoted into standing policy by a component with no way to tell the two
# apart. Intent is formed in accounts, where the user's request actually
# arrives, and is pushed here; this box holds a replica, never the record.
#
# ── Two rules that keep it from doing harm ───────────────────────
#
#   * RECONCILE ADDITIVELY ONLY. Intent `stopped` with a fully serving foundry
#     means somebody started it by hand. Never tear down live work to satisfy a
#     record.
#   * NEVER ACT WHILE SOMETHING ELSE OWNS THE ENVIRONMENT. Exit cleanly rather
#     than wait or force -- a boot unit parked behind a 17-minute provision is
#     indistinguishable from a hung boot.
#

set -u

LOG_PREFIX="[datafye-foundry-boot]"
log() { echo "${LOG_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

ENV_FILE="/opt/datafye/agent/agent.env"

# How long to wait for the API to answer after a provision before saying so.
API_WAIT_ATTEMPTS=60
API_WAIT_INTERVAL=5

# ── Read the installed configuration ─────────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
    log "No ${ENV_FILE}; agent not installed. Nothing to do."
    exit 0
fi

CLI_PATH=$(grep -oE '^DATAFYE_AGENT_CLI_PATH=.*' "${ENV_FILE}" | cut -d= -f2- || true)

# No mode gate. The hosted-only version left a self-provisioned user -- who
# stops and starts their own box and hits the identical app-less wake -- with
# nothing at all, and DAT-124's alternative hook (~rumi/boot.sh) is the Rumi
# Worker AMI's extension point, which a DIY box may not have either.
if [ -z "${CLI_PATH:-}" ] || [ ! -x "${CLI_PATH}" ]; then
    log "ERROR: Datafye CLI not found or not executable at '${CLI_PATH:-unset}'."
    exit 1
fi

# ── Locate the datafye user's run directory ──────────────────────
# Everything that describes an in-flight operation -- the DAT-196 lock, the
# DAT-183 markers, and the intent cache -- lives under ~datafye/.datafye/run,
# because the CLI resolves it from `user.home` and every foundry operation on
# this box runs as `datafye`.
DATAFYE_HOME=$(getent passwd datafye 2>/dev/null | cut -d: -f6)
DATAFYE_HOME="${DATAFYE_HOME:-/home/datafye}"
RUN_DIR="${DATAFYE_HOME}/.datafye/run"
INTENT_FILE="${RUN_DIR}/foundry-intent.json"

# ── Run the CLI as `datafye`, always ─────────────────────────────
# `rumi` is in `wheel` but NOT in `docker`. As any other user the CLI cannot
# reach the Docker socket, and before DAT-172 `foundry local status` reported
# that as "not provisioned" -- a confident false negative indistinguishable
# from an empty box, which sends you on to provision on top of a live
# environment. DAT-172 now classes it as a permission failure, which is a
# better error but still not an answer.
cli() { sudo -u datafye -H "${CLI_PATH}" "$@"; }

# ── Wait for Docker ──────────────────────────────────────────────
# The unit orders itself after docker.service, but "started" is not "accepting
# connections", and provisioning against a socket that is not ready yet fails
# in a way that reads like a foundry problem rather than a timing one.
docker_ready=false
for _ in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
        docker_ready=true
        break
    fi
    sleep 2
done

if [ "${docker_ready}" != true ]; then
    log "ERROR: Docker did not become ready within 60s; leaving the foundry alone."
    exit 1
fi

#
# Report a live CLI command holding this environment, or nothing.
#
# ⚠️ This script cannot take the DAT-196 lock itself, and must not pretend to.
# That lock is a `FileChannel.tryLock()`, which maps to fcntl(2) POSIX record
# locks -- while `flock(1)` uses flock(2). On Linux those are INDEPENDENT lock
# namespaces: a shell `flock` on the same file would succeed against a held
# Java lock and give mutual exclusion that silently is not.
#
# So exclusion is enforced where it always was -- every operation this script
# performs goes through the CLI, which takes the real lock for its duration.
# This check is the courtesy half: it stops us starting a command that would
# only be refused, and it names the holder in the journal. The window between
# checking and acting is closed by the lock itself, not by this function.
#
# The mechanism is deliberately the DAT-183 marker rather than the lock file.
# The lock file is never deleted on release (unlinking it would hand two
# processes different inodes), so its contents describe the LAST holder, not
# necessarily a current one. The marker's contract is stronger and is exactly
# this question: present AND the process alive.
#
in_flight_holder() {
    local marker pid command
    for marker in "${RUN_DIR}"/cli-*.json; do
        [ -f "${marker}" ] || continue

        pid=$(grep -oE '"pid"[[:space:]]*:[[:space:]]*[0-9]+' "${marker}" \
              | grep -oE '[0-9]+' | head -1)
        [ -n "${pid}" ] || continue

        kill -0 "${pid}" 2>/dev/null || continue

        # PIDs are recycled, and a boot is when that bites: a marker left by a
        # CLI killed before the last shutdown can name a PID this boot has
        # since handed to something unrelated, and `kill -0` alone would then
        # report a command that has not existed since the previous boot. Cheap
        # confirmation that the process is actually ours.
        if [ -r "/proc/${pid}/cmdline" ] \
           && ! tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -q 'datafye'; then
            continue
        fi

        command=$(grep -oE '"command"[[:space:]]*:[[:space:]]*"[^"]*"' "${marker}" \
                  | cut -d'"' -f4)
        echo "${command:-a Datafye CLI command} (pid ${pid})"
        return 0
    done
    return 1
}

holder=$(in_flight_holder)
if [ -n "${holder}" ]; then
    # Exit 0, not 1. Another owner is a correct and expected state, not a
    # failure of this unit -- and a red unit in `systemctl status` is a signal
    # somebody should act on, so spending it here would devalue it for the
    # boots where the foundry really is broken.
    log "Another operation already owns this environment: ${holder}."
    log "Leaving it alone; this unit re-runs on the next boot."
    exit 0
fi

# ── Read intent ──────────────────────────────────────────────────
# The cache is written by whoever receives the push from accounts (DAT-198).
# It does not exist yet, and this unit is correct without it: absence is the
# ordinary reading, not an error.
#
# ⚠️ Intent is NOT per-mutation. The installer's upgrade, the agent's
# `dataset add`, a human's debugging `stop` are all mutations that are not
# policy changes. The decisive case: dormancy must never record
# intended=stopped, or the box wakes and correctly declines to restore an
# environment the user never asked to lose.
INTENT="running"
if [ -f "${INTENT_FILE}" ]; then
    recorded=$(grep -oE '"intended"[[:space:]]*:[[:space:]]*"[a-zA-Z]+"' "${INTENT_FILE}" \
               | cut -d'"' -f4)
    case "${recorded}" in
        stopped|running)
            INTENT="${recorded}"
            log "Recorded intent: ${INTENT} (from ${INTENT_FILE})."
            ;;
        "")
            log "WARNING: ${INTENT_FILE} records no intent; assuming running."
            ;;
        *)
            # Treat an unrecognised value as running rather than refusing to
            # act. A newer writer adding a state must not brick the boot path
            # of an older box, and `running` is the additive answer -- the
            # worst case is starting a foundry that was going to be started.
            log "WARNING: ${INTENT_FILE} records unrecognised intent '${recorded}'; assuming running."
            ;;
    esac
fi

# ── Interrogate ──────────────────────────────────────────────────
status_out=$(cli foundry local status 2>&1)

provisioned=unknown
if printf '%s\n' "${status_out}" | grep -qE '^[[:space:]]*Provisioned:[[:space:]]+yes'; then
    provisioned=yes
elif printf '%s\n' "${status_out}" | grep -qE '^[[:space:]]*Provisioned:[[:space:]]+no'; then
    provisioned=no
fi

if [ "${provisioned}" = unknown ]; then
    # DAT-172: "could not look" is not "nothing is here". Acting on the second
    # reading when the first is true is what provisions on top of a live
    # environment, so stop and say why.
    log "ERROR: could not establish whether a foundry exists. Status reported:"
    printf '%s\n' "${status_out}" | tail -20 | while IFS= read -r line; do
        log "  status: ${line}"
    done
    exit 1
fi

# ── Reconcile ────────────────────────────────────────────────────
if [ "${INTENT}" = stopped ]; then
    # Whatever the observation. If the foundry is serving, a human started it
    # by hand and this unit does not get to overrule them; if it is down, that
    # already matches. Stopping here is the ADDITIVE-ONLY rule: reconcile
    # toward intent by bringing things up, never by taking them down.
    log "Intent is 'stopped'; leaving the environment as it is (currently provisioned: ${provisioned})."
    exit 0
fi

if [ "${provisioned}" = no ]; then
    # No -x descriptor: an empty foundry, Platform plus the API system with no
    # datasets, which is the state prompt.py and CLAUDE.md describe. The agent
    # then ADDS a dataset with `foundry local dataset add` / `apply` rather
    # than running `provision` on top of a live environment (DAT-93).
    log "No foundry present. Provisioning an empty foundry (this takes several minutes)..."

    if ! cli foundry local provision; then
        # Non-fatal on purpose, and the reasoning matters: the agent service is
        # already up and is useful without a foundry (chat, docs, code and
        # memory all work). Failing hard repairs nothing, and the next boot
        # retries because nothing was marked done. What must NOT happen is
        # silence -- DAT-170 went unnoticed precisely because a missing foundry
        # left no trace anywhere.
        log "ERROR: Foundry provisioning failed. The agent stays up without an environment;"
        log "       this unit retries on the next boot. The failure report is under"
        log "       ${DATAFYE_HOME}/.datafye/logs -- read it before rebuilding. Investigate with:"
        log "         sudo -u datafye -H ${CLI_PATH} foundry local status"
        exit 1
    fi

    # ⚠️ Deliberately left RUNNING. The old first-boot unit provisioned and
    # then STOPPED the foundry, for two reasons that both now live elsewhere:
    # the uniform postcondition is this unit's job (it converges on every
    # boot), and staying out of the app-less wake state is DAT-125's (stop the
    # apps cleanly before the box stops). Under the readiness model a fresh box
    # must end up matching intent, and intent is running -- otherwise the box
    # is permanently unready and prompt.py's "your sandbox already has one"
    # stays false on exactly the boxes that just built one.
    log "Foundry provisioned. Waiting for the API to answer..."

    api_ready=false
    for _ in $(seq 1 ${API_WAIT_ATTEMPTS}); do
        if cli foundry local status 2>/dev/null \
                | grep -qE '^[[:space:]]*API:[[:space:]]+up'; then
            api_ready=true
            break
        fi
        sleep ${API_WAIT_INTERVAL}
    done

    if [ "${api_ready}" = true ]; then
        log "Foundry provisioned and serving."
        exit 0
    fi

    # Report it rather than acting on it. `provision` waits for every service
    # to reach appstartdone, so the applications have started; the API lagging
    # past five minutes is worth a line in the journal, but relaunching on top
    # of services that are simply still coming up is how a healthy environment
    # gets broken.
    log "WARNING: provisioned, but the API did not answer within"
    log "         $((API_WAIT_ATTEMPTS * API_WAIT_INTERVAL))s. The environment is up; check it with"
    log "         'sudo -u datafye -H ${CLI_PATH} foundry local status'."
    exit 0
fi

# ── Converge ─────────────────────────────────────────────────────
# A foundry exists and intent is running, so bring it to running FROM WHATEVER
# STATE IT IS IN. This covers a cleanly stopped box, an app-less wake
# (containers restored by `--restart unless-stopped` with no applications
# inside them), and a partially serving one, because DAT-197 made `start`
# converge per service rather than launch everything: it probes each service
# for an ANSWER and launches only the dead ones, so a healthy environment is a
# fast no-op that exits 0.
#
# ⚠️ Note what this deliberately does NOT do: it does not read `Overall:
# HEALTHY` from the status above and skip the converge. That verdict keys on
# the deployment API answering, and an environment with one dead service still
# reports HEALTHY (DAT-200, reproduced live). Skipping on it would make this
# unit blind to exactly the partial state it exists to repair. The serving
# decision belongs to the prober, in one place, not duplicated in shell.
log "Foundry present; converging it to running..."

if cli foundry local start; then
    log "Foundry running."
    exit 0
fi

log "ERROR: could not bring the foundry to running. The agent stays up without a usable"
log "       environment; this unit retries on the next boot. Read the newest failure report"
log "       under ${DATAFYE_HOME}/.datafye/logs before rebuilding -- the broken environment is"
log "       the only evidence of why it failed. Investigate with:"
log "         sudo -u datafye -H ${CLI_PATH} foundry local status"
exit 1
