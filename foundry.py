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
Reads the foundry readiness state the CLI and the boot service write.

Why this exists: "Running" used to mean this Python process answers /health,
which says nothing at all about whether the box can do work. On u1 that gap put
a user's request onto a box three minutes into its first provision -- the agent
even logged "Datafye API MCP: NOT REACHABLE" fifteen seconds beforehand, and
nothing acted on it.

The state file is a cross-language contract. The deploy engine writes it in Java
from every lifecycle command; the foundry boot service writes it in shell. This
module only reads it, and deliberately does no inference of its own: the whole
point of the file is that layers stop guessing from side effects.

Note the file lives under the CLI's run directory (~/.datafye/run), NOT the
agent's state root. It describes the environment on the box, which outlives and
is independent of this process -- and it is written by whoever ran the command,
which may be the boot service or an operator over SSH rather than the agent.
"""

from __future__ import annotations

import json
import os
from typing import Any

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
