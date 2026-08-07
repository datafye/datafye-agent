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
# Datafye Agent - First-Boot Foundry Provisioner (hosted mode)
#
# The hosted AMI is baked with --ami-cleanup, which deliberately SKIPS foundry
# provisioning: provisioning pulls images, starts containers and writes
# instance-specific state under ~/.rumi, none of which is safe to snapshot into
# an AMI. The installer's comment promised that "each per-user sandbox
# provisions its own foundry at first boot" -- but nothing implemented it, so
# every hosted sandbox came up with no foundry while prompt.py told the agent
# one already existed (DAT-170). This script is that missing step.
#
# Run by datafye-foundry-firstboot.service, a systemd one-shot enabled in hosted
# mode. It is separate from first-boot.sh, which is standalone/marketplace only
# (it reads EC2 user data and hard-codes --mode standalone).
#
# Two design points worth keeping:
#
#   * NOT sentinel-guarded. It keys on whether a foundry is ACTUALLY provisioned
#     rather than on a marker file. A marker written before the work succeeded
#     would lock a box out of ever retrying after a failed provision, which is
#     precisely the state we are trying to stop shipping. Keying on real state
#     also makes the unit safe to leave enabled: on an ordinary reboot or a
#     dormancy wake it finds the foundry present and exits in a second.
#
#   * Runs the CLI as the `datafye` user, always. `rumi` is in `wheel` but NOT
#     in `docker`, so as any other user `foundry local status` cannot reach the
#     Docker socket and reports "not provisioned" -- a confident false negative
#     indistinguishable from an empty box, which would send us on to provision
#     on top of a live environment (DAT-172).
#

set -u

LOG_PREFIX="[datafye-foundry-firstboot]"
log() { echo "${LOG_PREFIX} $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

ENV_FILE="/opt/datafye/agent/agent.env"

# ── Read the installed configuration ─────────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
    log "No ${ENV_FILE}; agent not installed. Nothing to do."
    exit 0
fi

# shellcheck disable=SC1090
MODE=$(grep -oE '^DATAFYE_AGENT_MODE=.*' "${ENV_FILE}" | cut -d= -f2- || true)
CLI_PATH=$(grep -oE '^DATAFYE_AGENT_CLI_PATH=.*' "${ENV_FILE}" | cut -d= -f2- || true)

if [ "${MODE:-}" != "hosted" ]; then
    log "Mode is '${MODE:-unset}', not hosted. Nothing to do."
    exit 0
fi

if [ -z "${CLI_PATH:-}" ] || [ ! -x "${CLI_PATH}" ]; then
    log "ERROR: Datafye CLI not found or not executable at '${CLI_PATH:-unset}'."
    exit 1
fi

# ── Wait for Docker ──────────────────────────────────────────────
# The unit orders itself after docker.service, but "started" is not "accepting
# connections", and provisioning against a socket that is not ready yet fails in
# a way that reads like a foundry problem rather than a timing one.
docker_ready=false
for _ in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
        docker_ready=true
        break
    fi
    sleep 2
done

if [ "${docker_ready}" != true ]; then
    log "ERROR: Docker did not become ready within 60s; leaving the foundry unprovisioned."
    exit 1
fi

# ── Already provisioned? ─────────────────────────────────────────
# Run as `datafye` so the Docker-permission false negative above cannot fire.
if sudo -u datafye "${CLI_PATH}" foundry local status 2>/dev/null \
        | grep -qE '^[[:space:]]*Provisioned:[[:space:]]+yes'; then
    log "Foundry already provisioned. Nothing to do."
    exit 0
fi

# ── Provision an empty foundry ───────────────────────────────────
# No -x descriptor: this stands up the Platform and the API system with NO
# datasets, which is exactly the state prompt.py and CLAUDE.md describe. The
# agent then ADDS a dataset with `foundry local dataset add` / `apply` rather
# than running `provision` on top of a live environment (DAT-93).
log "No foundry present. Provisioning an empty foundry (this takes several minutes)..."

if sudo -u datafye "${CLI_PATH}" foundry local provision; then
    log "Foundry provisioned. Waiting for the API to answer before stopping..."

    # WAIT before stopping. `foundry local stop` starts by asking the deployment
    # API which systems are deployed, so calling it the instant `provision`
    # returns races the API's own startup: observed on a live sandbox, the stop
    # failed 12 seconds in and left the foundry RUNNING, which then decayed to
    # DEGRADED (containers up, apps gone) on the next box stop. Provision
    # returning is not the same as the API serving.
    api_ready=false
    for _ in $(seq 1 60); do
        if sudo -u datafye "${CLI_PATH}" foundry local status 2>/dev/null \
                | grep -qE '^[[:space:]]*API:[[:space:]]+up'; then
            api_ready=true
            break
        fi
        sleep 5
    done

    if [ "${api_ready}" != true ]; then
        log "WARNING: the API did not answer within 5 minutes; stopping anyway."
    fi

    log "Stopping the foundry so the box is left in the standard rest state..."

    # Leave the foundry PRESENT BUT STOPPED, not running. Two reasons, and the
    # second is the one that matters:
    #
    #   1. It gives every box one uniform postcondition. After this unit has run,
    #      a foundry always exists and only ever needs STARTING -- so the wake
    #      path (DAT-124) has a single case to handle rather than branching on
    #      "never existed" versus "existed but stopped".
    #
    #   2. It avoids the app-less wake state. `foundry local stop` runs each
    #      system's `shutdown` admin script and only then stops the environment,
    #      so the XVM apps come down cleanly. That also marks the containers as
    #      EXPLICITLY stopped, and `--restart unless-stopped` (what the local
    #      provisioner sets) deliberately does NOT restart those on the next
    #      daemon start. A foundry left RUNNING when the box stops is the
    #      opposite: its containers were never explicitly stopped, so they come
    #      back as bare sshd machines with no apps inside and the API never
    #      answers -- the wedge in DAT-171. Stopping cleanly here keeps a fresh
    #      box out of that state from the very first boot.
    # Capture the stop's output: the first cut logged only THAT it failed, which
    # left no way to tell a racing API from a genuinely broken shutdown.
    stop_out=$(sudo -u datafye "${CLI_PATH}" foundry local stop 2>&1)
    stop_rc=$?
    if [ ${stop_rc} -eq 0 ]; then
        log "Foundry present and stopped. Start it with 'foundry local start' before use."
        exit 0
    fi

    # The foundry exists, which is the postcondition that matters; it is simply
    # still running. Do not fail the unit over this -- report it and move on.
    log "WARNING: provisioned but could not stop cleanly (exit ${stop_rc}); left RUNNING."
    printf '%s\n' "${stop_out}" | tail -20 | while IFS= read -r line; do
        log "  stop: ${line}"
    done
    log "         It is usable, but if the box stops while it is up, its containers"
    log "         will come back without their apps (DAT-171)."
    exit 0
fi

# Non-fatal on purpose, and the reasoning matters: the agent service is already
# running and is useful without a foundry (chat, docs, code, memory all work).
# Failing this unit hard would not repair anything, and the next boot retries
# because nothing was marked done. What must NOT happen is silence -- the whole
# reason DAT-170 went unnoticed is that a missing foundry left no trace anywhere.
log "ERROR: Foundry provisioning failed. The agent stays up without an environment;"
log "       this unit retries on the next boot. Investigate as the datafye user with:"
log "         sudo -u datafye ${CLI_PATH} foundry local status"
exit 1
