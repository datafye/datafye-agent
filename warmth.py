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
Is real work happening on this box, of the kind that must not be interrupted?

Accounts' idle monitor stops a box that has not been *chatted with* for 30
minutes. That is the wrong question: on u1 it stopped a sandbox **sixteen
minutes into a foundry provision**, and on wake `--restart unless-stopped`
faithfully restored the containers with the applications never deployed,
leaving an environment that needed a full rebuild. Nobody had typed anything,
so by the only measure available the box was idle. It was not.

## The channel

`/health` already reports `active_proxied_apps`, accounts already treats a
non-empty list as busy in BOTH places that matter -- `agentBusy` (the
pre-stop re-check) and `idleSnapshot` (the admin countdown) -- and the agent
has always sent `[]`. So the plumbing has been live and inert the whole time,
on the agent's side of the wire. Filling the existing field rather than adding
one means no accounts change and no coordinated deploy.

Values are self-describing (`env:data-flowing`, `env:provision`) because they
surface raw in the admin panel and in logs, where a bare `true` would tell an
operator nothing about WHY a box refuses to sleep.

## What counts, and what deliberately does not

⚠️ **An idle or empty foundry must report nothing.** If merely having
containers up kept a box awake, dormancy would stop saving anything at all --
every provisioned sandbox in the fleet would be permanently warm. That falls
out of the definition rather than needing a carve-out: an empty foundry has no
datasets, so no service reports activity, so nothing is flowing.

⚠️ **Observation must never count as activity.** The platform guarantees this
on its side -- health pings, fetch-status polls and the activity reads
themselves do not bump the signal -- which is what makes it safe for this
module to poll once a minute forever.

⚠️ **Unreachable is not warm.** A service that cannot be reached is "I could
not look", and the safe reading of that is *not* to pin the box awake
indefinitely on a probe that may never recover. It matches how accounts treats
an agent it cannot reach: still stoppable. The case this might seem to lose --
a box mid-provision whose API is not up yet -- is covered by the in-flight
signal instead, which is local and does not depend on the deployment
answering.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

import foundry

logger = logging.getLogger(__name__)

# How recent a service's last activity must be for the environment to count as
# working. Chosen AGAINST the 30-minute idle threshold rather than picked: a
# third of it, so this can never dominate the dormancy decision, while being
# far longer than any gap inside genuinely continuous work. A live feed
# receives trades, a replay advances clock ticks, and a history fetch reports
# progress as it goes -- none of them go quiet for ten minutes while still
# running. Configurable because the idle threshold is.
DATA_WINDOW_SECONDS = int(os.environ.get("DATAFYE_AGENT_WARM_DATA_WINDOW", "600"))

# How often the deployment is asked. /health is polled by accounts every 60s,
# by the upgrade cron every minute, and by the SPA, so the probe must never sit
# on that path.
REFRESH_INTERVAL_SECONDS = int(os.environ.get("DATAFYE_AGENT_WARM_REFRESH_INTERVAL", "60"))

# One bounded call, not a fan-out: the platform's /deployment/activity asks
# every deployed service in Java and returns one verdict.
PROBE_TIMEOUT_SECONDS = float(os.environ.get("DATAFYE_AGENT_WARM_PROBE_TIMEOUT", "15"))

# The last data-flow reading, refreshed on a timer. Absent until the first
# probe completes, which reads as not-flowing -- the safe direction.
_data_flowing: bool = False
_data_detail: str = "the environment has not been asked yet"
_checked_at: int | None = None


async def probe_data_flowing(deployment_api_url: str) -> tuple[bool, str]:
    """Ask the deployment whether any service has done work recently.

    One call. `GET /deployment/activity` fans out across every deployed
    dataset's feed, agg, history and reference **inside the platform** and
    returns a single verdict against the window we supply, so the agent does
    not fan out over HTTP and does not bake in a threshold the platform would
    also have to know.
    """
    base = deployment_api_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{base}/datafye-api/v1/deployment/activity",
                params={"activeWithinSeconds": DATA_WINDOW_SECONDS})
        if response.status_code != 200:
            return False, f"the deployment activity endpoint returned HTTP {response.status_code}"
        body = response.json()
    except Exception as exc:
        # Not warm. See the module docstring: "I could not look" must not pin a
        # box awake forever, and a genuinely busy provision is covered by the
        # in-flight signal, which does not need the deployment to answer.
        return False, f"the deployment could not be asked ({exc})"

    if not body.get("dataFlowing"):
        return False, "no service has done work recently"

    # Name the busiest service rather than only that something is busy: this
    # string is what an operator sees when a box refuses to sleep.
    busiest = ""
    latest = 0
    for entry in body.get("services") or []:
        when = entry.get("latestActivityTime") or 0
        if entry.get("reachable") and when > latest:
            latest = when
            busiest = f"{entry.get('dataset') or '?'}/{entry.get('service') or '?'}"
            kind = entry.get("kind")
            if kind:
                busiest += f" ({kind})"
    return True, busiest or "a service reported recent work"


async def refresh_forever(deployment_api_url: str) -> None:
    """Keep the data-flow reading fresh, off the /health path.

    Failures are swallowed and retried: an exception escaping here would stop
    warmth updating for the life of the process, and a signal that silently
    freezes at "warm" would keep a box alive forever while one frozen at "cold"
    would let it sleep through real work.
    """
    global _data_flowing, _data_detail, _checked_at
    while True:
        try:
            _data_flowing, _data_detail = await probe_data_flowing(deployment_api_url)
            _checked_at = int(time.time() * 1000)
        except Exception as exc:
            logger.warning("Warm-signal probe failed: %s", exc)
            _data_flowing, _data_detail = False, f"the warm-signal probe failed ({exc})"
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


def active_work() -> list[str]:
    """The work currently keeping this box awake, as self-describing labels.

    Empty means nothing is happening and the box may be stopped. Never raises:
    this is on the /health path, and an exception here would make the agent
    look dead to the very monitor deciding whether to stop it.
    """
    active: list[str] = []
    try:
        # 1. Data flowing in the environment. Cached; see refresh_forever.
        if _data_flowing:
            active.append("env:data-flowing")

        # 2. A lifecycle command in flight. Read live rather than cached -- it
        #    is a couple of small local files, and this is the signal that
        #    covers a 17-minute provision, where being a minute stale at the
        #    wrong moment is exactly the u1 failure. A HUNG command counts as
        #    warm on purpose: a box with a wedged CLI is precisely the box you
        #    want left running so somebody can log in and find out why.
        holder = foundry.in_flight_holder()
        if holder:
            active.append(f"env:{_operation_of(holder)}")
    except Exception as exc:
        logger.warning("Could not assemble the warm signal: %s", exc)

    # 3. Compute the agent started outside a turn is NOT reported, because
    #    there is none to report: prompt.py forbids background execution
    #    outright (no `&`, `nohup`, `setsid`, `disown`) after a backgrounded
    #    provision was orphaned when its session ended (DAT-185). A turn in
    #    flight is already reported separately as running_jobs. This becomes
    #    real when the agent can run and expose a long-lived app (DAT-202), and
    #    the label space (`compute:<name>`) is reserved for it.
    return active


def describe() -> dict[str, Any]:
    """The warm signal with its evidence, for operators and for /health readers
    that want to know why rather than only whether."""
    return {
        "active": active_work(),
        "data_window_seconds": DATA_WINDOW_SECONDS,
        "data_detail": _data_detail,
        "checked_at": _checked_at,
    }


def _operation_of(holder: str) -> str:
    """`provision` out of `datafye foundry local provision (pid 2889)`.

    Falls back to a generic label rather than an empty one: the string ends up
    in an operator-facing list, where `env:` alone would read as a bug.
    """
    text = holder.split("(")[0].strip()
    words = [w for w in text.split() if w]
    return words[-1] if words else "operation"
