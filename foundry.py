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

"""
The foundry as seen from the agent: what state it is in, and bringing it down
cleanly when the box is about to stop.

Two halves, and they are here together because they answer to the same
question -- whether this box can currently do work. The reader (DAT-198)
reports it; the graceful stop (DAT-125) is what keeps the answer true across a
power cycle.

Why the reader exists: "Running" used to mean this Python process answers /health,
which says nothing at all about whether the box can do work. On u1 that gap put
a user's request onto a box three minutes into its first provision -- the agent
even logged "Datafye API MCP: NOT REACHABLE" fifteen seconds beforehand, and
nothing acted on it.

⚠️ NOTHING WRITES THIS FILE TODAY, so `/health` reports `unknown` on every box.
That is expected, not a defect to chase. Both writers were removed on purpose:
the deploy engine's was shipped and then REVERTED (datafye-deploy PR #11,
because a lifecycle command cannot tell a debugging `stop` from a policy
decision), and the foundry boot service deliberately writes no state either
(DAT-199 -- it acts, it does not narrate). Under the agreed model readiness is
DERIVED by whoever asks, from intent (pushed by accounts) + observation + the
in-flight lock, rather than stored as one fact by whoever moved last.

This module is kept as the reader half so re-pointing it at the derived form is
additive. Wiring that up is DAT-198's remaining work; until then every caller
gets `unknown`, which is a truthful answer.

It deliberately does no inference of its own: the whole point was that layers
stop guessing from side effects.

Note the file lives under the CLI's run directory (~/.datafye/run), NOT the
agent's state root. It describes the environment on the box, which outlives and
is independent of this process -- and it is written by whoever ran the command,
which may be the boot service or an operator over SSH rather than the agent.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Where the CLI keeps everything describing an in-flight or just-finished
# environment operation: the DAT-196 lock and the DAT-183 markers. Not the
# agent's state root -- this belongs to the environment, not to this process.
RUN_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("DATAFYE_AGENT_RUN_DIR", "~/.datafye/run"))
)

# Upper bound on a graceful stop. Deliberately shorter than the caller's own
# read timeout, so THIS side is what expires: an expiry here returns a
# structured answer naming what happened, while an expiry at the caller returns
# nothing at all and cannot be told from an unreachable box.
STOP_TIMEOUT_SECONDS = int(os.environ.get("DATAFYE_AGENT_FOUNDRY_STOP_TIMEOUT", "240"))

# Same location the engine's FoundryState and the boot service use. The
# override exists so a local run can point at a scratch copy.
FOUNDRY_STATE_FILE = os.path.abspath(
    os.path.expanduser(
        os.environ.get("DATAFYE_AGENT_FOUNDRY_STATE_FILE", "~/.datafye/run/foundry-state.json")
    )
)

# The states the writers use. Reproduced here as documentation, not as
# validation: an unrecognised value is passed through rather than rejected, so a
# newer writer can add a state without this agent refusing to report it.
KNOWN_STATES = ("starting", "provisioning", "restoring", "ready", "failed", "absent")

# Ours, not the writers'. Means "no state file", which is a different claim from
# any of theirs -- notably from "absent", which asserts that no environment
# exists. Not knowing and knowing there is nothing are answers a caller must be
# able to tell apart.
STATE_UNKNOWN = "unknown"


def read_foundry_state() -> dict[str, Any]:
    """The current foundry readiness, as {state, since, operation, intended,
    error, reason}.

    Never raises. This is read on the /health path, which must answer even when
    everything else on the box is broken -- an agent that cannot report its
    health is indistinguishable from an instance that is simply dead, and that
    is the one distinction accounts needs most.
    """
    try:
        with open(FOUNDRY_STATE_FILE, "r") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        # The ordinary reading on a fresh box: the boot service is ordered after
        # the agent, so the agent answers /health before anything has written a
        # state. Absence is only meaningful after a grace period, and judging
        # that is the caller's job -- accounts knows when the box booted, this
        # process does not.
        return _unknown("no foundry state has been recorded yet")
    except Exception as exc:
        return _unknown(f"the foundry state file could not be read: {exc}")

    if not isinstance(raw, dict):
        return _unknown("the foundry state file is not an object")

    state = raw.get("state") or STATE_UNKNOWN
    return {
        "state": state,
        "since": raw.get("since"),
        "operation": raw.get("operation"),
        "intended": raw.get("intended"),
        "error": raw.get("error"),
        "reason": None,
    }


def is_ready(state: dict[str, Any]) -> bool:
    """Whether the foundry matches its intended state.

    Deliberately NOT "is the foundry running". A foundry the user deliberately
    stopped is in good order, and treating it as unready would leave that box
    permanently unhealthy, fixable only by starting an environment they
    explicitly did not want.
    """
    return state.get("state") == "ready"


def describe_for_model(state: dict[str, Any]) -> str:
    """A sentence for the system prompt telling the model what it can rely on.

    The model is told the state AND the reason, so it can explain the situation
    to the user rather than acting blindly on an environment that is mid-build.
    That is the whole failure this addresses: the information existed, nothing
    consumed it.
    """
    name = state.get("state")
    operation = state.get("operation")
    intended = state.get("intended")
    error = state.get("error")

    if name == "ready" and intended == "stopped":
        return (
            "The foundry on this box is provisioned but deliberately STOPPED. "
            "This is a healthy state, not a fault. Start it before any data work, "
            "and tell the user you are doing so."
        )
    if name == "ready":
        return "The foundry on this box is ready to use."
    if name in ("provisioning", "restoring", "starting"):
        return (
            f"The foundry on this box is NOT ready: a '{operation or name}' operation is in "
            "progress. Do NOT start, apply, provision or otherwise change the environment "
            "while that is running - a second operation on one foundry is what corrupts it. "
            "Tell the user the environment is still being prepared and roughly what it is doing, "
            "then wait or ask them to try again shortly."
        )
    if name == "failed":
        detail = f" The last error was: {error}" if error else ""
        return (
            f"The foundry on this box FAILED its last '{operation or 'operation'}'.{detail} "
            "Read the newest failure report under ~/.datafye/logs before deciding anything, and "
            "QUOTE the real error to the user. Do NOT rebuild automatically: the broken "
            "environment is the only evidence of why it failed. Offer a rebuild as a choice."
        )
    if name == "absent":
        return (
            "There is no foundry on this box, and none is intended. Any data work needs one "
            "provisioned first - tell the user before doing it."
        )

    reason = state.get("reason") or "no foundry state has been recorded"
    return (
        f"The readiness of the foundry on this box is UNKNOWN ({reason}). Check with "
        "'datafye foundry local status' before assuming an environment exists, and do not "
        "assume it is broken either."
    )


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "state": STATE_UNKNOWN,
        "since": None,
        "operation": None,
        "intended": None,
        "error": None,
        "reason": reason,
    }


# ── Graceful stop before the box is powered off (DAT-125) ────────────────


def in_flight_holder() -> str | None:
    """The environment command currently running on this box, or None.

    Reads the DAT-183 markers (``~/.datafye/run/cli-<pid>.json``) rather than
    the DAT-196 lock file, for the same reason the boot service does: the lock
    file is deliberately never deleted on release, so its contents describe the
    LAST holder rather than necessarily a current one. The marker's contract is
    the stronger one and is exactly this question -- present AND the process
    alive.

    Never raises. A caller uses this to decide whether it is safe to act, and
    an exception here would be indistinguishable from "nothing is running",
    which is the answer that does damage.
    """
    try:
        markers = glob.glob(os.path.join(RUN_DIR, "cli-*.json"))
    except Exception:
        return None

    for marker in markers:
        try:
            with open(marker, "r") as handle:
                record = json.load(handle)
            pid = int(record.get("pid"))
        except Exception:
            # A marker being written as we read it, or one from an older
            # format. Not evidence of anything either way.
            continue

        if not _process_alive(pid):
            continue

        command = record.get("command") or "a Datafye CLI command"
        return f"{command} (pid {pid})"

    return None


def _process_alive(pid: int) -> bool:
    """Whether a marker's PID is a live Datafye CLI.

    PIDs are recycled, so liveness alone is not enough -- a marker left by a
    CLI that was killed can name a PID the OS has since handed to something
    unrelated, and believing it would leave this box permanently "busy". The
    cmdline check is the cheap confirmation. Where /proc is unavailable (macOS,
    local development) liveness alone has to do; the deployed box is Linux.
    """
    try:
        os.kill(pid, 0)
    except Exception:
        return False

    cmdline_path = f"/proc/{pid}/cmdline"
    if not os.path.exists(cmdline_path):
        return True
    try:
        with open(cmdline_path, "rb") as handle:
            return b"datafye" in handle.read().lower()
    except Exception:
        return True


async def graceful_stop(cli_path: str, timeout: int = STOP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Bring the foundry down cleanly, and report which of four things happened.

    ``{"status": "stopped" | "absent" | "busy" | "failed", "detail": str}``

    The caller is the accounts idle monitor, about to stop the EC2 instance.
    What it needs from this is not success or failure but **whether stopping
    the box now is safe**, and those are different questions:

    - ``busy`` means another operation owns the environment, so powering the
      box off would cut it off mid-flight. That is the u1 incident exactly: a
      sandbox stopped sixteen minutes into a provision, which left containers
      whose applications were never deployed. The caller must ABORT.
    - ``failed`` means the stop did not complete, but nothing is in flight.
      There is nothing to protect by staying up, and a box that can never stop
      cleanly would otherwise bill forever, so the caller proceeds.
    - ``absent`` is not an error. A box with no foundry has nothing to bring
      down and is safe to stop.

    Why the stop matters at all: ``foundry local stop`` runs each system's
    shutdown so the applications flush their transaction logs, and then marks
    the containers EXPLICITLY stopped -- which ``--restart unless-stopped``
    deliberately does not undo on the next daemon start. A foundry still
    running when the box stops comes back as bare sshd machines with no
    applications inside them (DAT-171).
    """
    holder = in_flight_holder()
    if holder:
        return {"status": "busy", "detail": f"another operation owns this environment: {holder}"}

    status = await _run_cli(cli_path, ["foundry", "local", "status"], timeout=60)
    if status["code"] != 0:
        return {"status": "failed",
                "detail": f"could not read the environment status: {_tail(status['output'])}"}

    provisioned = _parse_provisioned(status["output"])
    if provisioned is False:
        return {"status": "absent", "detail": "no foundry is provisioned on this box"}
    if provisioned is None:
        # DAT-172: "could not look" is not "nothing is here". Refuse to claim
        # the box is clean when we could not establish it.
        return {"status": "failed",
                "detail": f"could not establish whether a foundry exists: {_tail(status['output'])}"}

    result = await _run_cli(cli_path, ["foundry", "local", "stop"], timeout=timeout)
    if result["code"] == 0:
        return {"status": "stopped", "detail": "the environment was stopped cleanly"}

    # A refusal from the DAT-196 lock is the one failure that is not a failure.
    # Re-check the marker rather than matching on the refusal text: the wording
    # is a message for humans in another repo, and keying on it would be a
    # cross-repo contract with no compiler behind it.
    holder = in_flight_holder()
    if holder:
        return {"status": "busy",
                "detail": f"an operation started while stopping: {holder}"}

    return {"status": "failed", "detail": _tail(result["output"])}


async def _run_cli(cli_path: str, args: list[str], timeout: int) -> dict[str, Any]:
    """Run the Datafye CLI off the event loop, bounded, never raising.

    Bounded because this runs while the accounts worker blocks on the reply; an
    unbounded call would convert "the environment is wedged" into "the sandbox
    plane is wedged". Off the loop because a multi-minute stop must not stop
    this process answering /health -- an agent that goes quiet during its own
    shutdown looks exactly like a dead instance.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            cli_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:
        return {"code": 127, "output": f"could not run {cli_path}: {exc}"}

    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return {"code": process.returncode, "output": (stdout or b"").decode("utf-8", "replace")}
    except asyncio.TimeoutError:
        # Kill it. Leaving it running would hold the environment lock with
        # nobody waiting on the result, and the next caller would be refused by
        # a command that has already been abandoned.
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        return {"code": 124, "output": f"'{' '.join(args)}' did not finish within {timeout}s"}


_PROVISIONED = re.compile(r"^\s*Provisioned:\s+(yes|no)\b", re.MULTILINE)


def _parse_provisioned(status_output: str) -> bool | None:
    """True, False, or None for "the CLI could not tell us"."""
    found = _PROVISIONED.search(status_output)
    if not found:
        return None
    return found.group(1) == "yes"


def _tail(text: str, lines: int = 12) -> str:
    """The last few lines of command output, for a caller that logs one line."""
    kept = [line for line in (text or "").strip().splitlines() if line.strip()]
    return " | ".join(kept[-lines:]) if kept else "no output"
