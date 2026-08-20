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
The foundry as seen from the agent: whether this box can currently do work,
and bringing the environment down cleanly when the box is about to stop.

Why it exists: "Running" used to mean this Python process answers /health,
which says nothing at all about whether the box can do work. On u1 that gap put
a user's request onto a box three minutes into its first provision -- the agent
even logged "Datafye API MCP: NOT REACHABLE" fifteen seconds beforehand, and
nothing acted on it.

## Readiness is DERIVED from three inputs, never stored as one fact (DAT-198)

    intent      what the box is SUPPOSED to be doing. Owned by accounts, where
                the user's decision actually arrives, and pushed here. This box
                holds a REPLICA in ~/.datafye/run/foundry-intent.json, never the
                record. Absent means running: no deviation has ever been
                recorded, and a sandbox exists to host a foundry.
    in flight   whether an operation owns the environment right now, read from
                the DAT-183 marker with a liveness check.
    observed    whether the applications are ANSWERING, interrogated on demand.

Readiness is "observed matches intent", evaluated only when nothing is in
flight.

⚠️ The first version of this stored readiness as a single fact that every
lifecycle command wrote. It shipped and was reverted (datafye-deploy PR #11).
The bug: an engineer SSHes in to debug and runs `foundry local stop`, the engine
records intended=stopped, and the box then stays down on every subsequent boot.
A debugging action promoted into standing policy by a component with no way to
tell the two apart. **"An operation is in flight" is a fact about a process;
"this box should have a running foundry" is a policy decision** -- and the
component performing an action is very often not the one that decided it.

⚠️ Interrogating without the in-flight input is dangerous. Mid-provision the
deployment reports "not serving", which is indistinguishable from a mismatch
against intent=running -- so anything reconciling on that would try to fix an
environment that is being built. That is the u1 collision, produced by something
trying to help.

## Why the snapshot is refreshed in the background

Interrogation costs real time when something is wrong (a dead service burns its
probe timeout), and /health must stay fast: accounts polls it to decide
dormancy, the upgrade cron gates on it every minute, and an agent that goes
quiet is indistinguishable from a dead instance. So the observation is refreshed
on a timer and /health serves the most recent snapshot with its age attached.
The in-flight read is cheap -- a few small files -- so it is done inline and is
always current.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import time
from typing import Any

import httpx
import yaml

logger = logging.getLogger(__name__)

# Where the CLI keeps everything describing an in-flight or just-finished
# environment operation: the DAT-196 lock, the DAT-183 markers, and the intent
# replica. Not the agent's state root -- this belongs to the environment, which
# outlives this process and is written to by whoever ran the last command.
RUN_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("DATAFYE_AGENT_RUN_DIR", "~/.datafye/run"))
)

# The intent replica. Same path datafye-foundry-boot.service reads before any
# user exists (DAT-199) -- that is the whole reason it is a file on disk rather
# than in this process's memory.
INTENT_FILE = os.path.join(RUN_DIR, "foundry-intent.json")

# Upper bound on a graceful stop. Deliberately shorter than the caller's own
# read timeout, so THIS side is what expires: an expiry here returns a
# structured answer naming what happened, while an expiry at the caller returns
# nothing at all and cannot be told from an unreachable box.
STOP_TIMEOUT_SECONDS = int(os.environ.get("DATAFYE_AGENT_FOUNDRY_STOP_TIMEOUT", "240"))

# How often the observation is refreshed, and how long a probe may take. The
# per-service probe bound is well under the deployment API's own 30s
# request/reply timeout, so a dead service costs us seconds rather than the
# API's full wait.
#
# 20s, down from 60. This snapshot is what /health serves, so it -- not the
# reader's poll -- is the binding constraint on how fresh a deployment reading
# can be: a UI polling every 15s against a 60s snapshot re-reads the same value
# four times. Safe to lower because `observe_forever` sleeps AFTER each pass
# completes, so passes can never stack; a slow one (a dead service costs up to
# PING_TIMEOUT_SECONDS) simply pushes the next out, which self-limits exactly
# when something is wrong. The box is single-tenant and every call is to a
# container on the same host.
OBSERVE_INTERVAL_SECONDS = int(os.environ.get("DATAFYE_AGENT_FOUNDRY_OBSERVE_INTERVAL", "20"))
# The datasets call is fast or the API is down, so it gets a short bound.
OBSERVE_TIMEOUT_SECONDS = float(os.environ.get("DATAFYE_AGENT_FOUNDRY_OBSERVE_TIMEOUT", "5"))
# ⚠️ The health ping is a different animal: it asks the API about four services
# at once, and a DEAD service makes the API wait out its own 30s Rumi
# request/reply timeout before answering for it. The engine avoids that by
# probing each service separately in parallel, because it runs on a command's
# critical path. Here the observation is a BACKGROUND refresh, so it can simply
# afford to wait -- which buys per-service fidelity from a single call. Bounding
# this below 30s would turn every partial environment into "not answering at
# all", losing exactly the distinction the ping exists for.
PING_TIMEOUT_SECONDS = float(os.environ.get("DATAFYE_AGENT_FOUNDRY_PING_TIMEOUT", "40"))

# Intent values. Deliberately only two: this is a policy statement about whether
# the box should be hosting a foundry, not a lifecycle state machine.
INTENT_RUNNING = "running"
INTENT_STOPPED = "stopped"

# Derived readiness states.
STATE_READY = "ready"              # observed matches intent
STATE_NOT_READY = "not_ready"      # it does not
STATE_IN_PROGRESS = "in_progress"  # an operation owns the environment; do not judge
STATE_ABSENT = "absent"            # there is no environment, and none is intended
STATE_UNKNOWN = "unknown"          # not enough evidence to say

# What the interrogation found.
OBSERVED_SERVING = "serving"
OBSERVED_PARTIAL = "partial"
OBSERVED_DOWN = "down"          # provisioned, but not answering -- wedged
OBSERVED_ABSENT = "absent"      # nothing is provisioned at all
OBSERVED_UNKNOWN = "unknown"

# ⚠️ DOWN and ABSENT used to be the same answer, and collapsing them made
# readiness lie. A silent deployment API is all the HTTP probe can see, so a
# cleanly stopped foundry and one whose containers are up with a dead API were
# indistinguishable -- and since intent=stopped short-circuits to READY, a box
# the CLI called DEGRADED was reported as ready. Telling them apart costs one
# CLI call, and only on the path where something is already wrong.

# The last environment type we managed to read. The type is a property of what
# is PROVISIONED, not of whether it answers, and it cannot change without the
# environment being rebuilt -- so going blank the moment the API stops
# answering hides it exactly when someone is looking at a broken box and asking
# what kind it is. Remembered for the life of the process; a rebuild into a
# different type brings the API back up to correct it.
_last_env_type: str | None = None

# The last observation, refreshed on a timer by observe_forever().
_snapshot: dict[str, Any] = {
    "observed": OBSERVED_UNKNOWN,
    "datasets": [],
    "not_answering": [],
    "checked_at": None,
    "detail": "the environment has not been interrogated yet",
}


# ── Intent: a replica of what accounts decided ───────────────────────────


def read_intent() -> dict[str, Any]:
    """The last intent accounts pushed, as {intended, source, at}.

    ⚠️ ABSENT MEANS RUNNING, and that default is load-bearing rather than
    convenient. A sandbox exists to host a foundry, so "no deviation has ever
    been recorded" and "it should be running" are the same statement. Treating
    absence as unknown would leave a fresh box permanently unready, and treating
    it as stopped would leave it permanently empty.

    Never raises: this feeds /health.
    """
    try:
        with open(INTENT_FILE, "r") as handle:
            record = json.load(handle)
        intended = record.get("intended")
        if intended in (INTENT_RUNNING, INTENT_STOPPED):
            return {
                "intended": intended,
                "source": record.get("source") or "accounts",
                "at": record.get("at"),
            }
        # A value from a newer writer. Fall back to running rather than refusing
        # to act: an older box must not be bricked by a vocabulary it predates,
        # and running is the additive answer.
        logger.warning("Unrecognised recorded intent %r; assuming running", intended)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Could not read the intent replica: %s", exc)

    return {"intended": INTENT_RUNNING, "source": "default", "at": None}


def write_intent(intended: str, source: str = "accounts") -> dict[str, Any]:
    """Record what accounts last decided. Raises so the push can report failure.

    Written to disk rather than held in memory because the reader that matters
    most runs before this process does: datafye-foundry-boot.service reconciles
    the foundry at boot, when there is no user, no JWT, and nothing to ask.
    """
    if intended not in (INTENT_RUNNING, INTENT_STOPPED):
        raise ValueError(f"intent must be {INTENT_RUNNING!r} or {INTENT_STOPPED!r}, got {intended!r}")

    os.makedirs(RUN_DIR, exist_ok=True)
    record = {"intended": intended, "source": source, "at": int(time.time() * 1000)}
    # Write-then-rename: the boot service may read this file at any moment, and
    # a half-written replica would read as "unrecognised" and silently fall back.
    temp = INTENT_FILE + ".new"
    with open(temp, "w") as handle:
        json.dump(record, handle)
    os.replace(temp, INTENT_FILE)
    logger.info("Foundry intent recorded: %s (from %s)", intended, source)
    return record


# ── Observation: are the applications answering ──────────────────────────


async def observe(deployment_api_url: str, cli_path: str = "datafye") -> dict[str, Any]:
    """Interrogate the deployment: which datasets are up, and are they serving.

    Keys on applications ANSWERING, never on containers being up. Rumi's local
    containers are machines running sshd with the applications deployed into
    them afterwards, so under `--restart unless-stopped` a box presents a
    complete, healthy-looking set of containers with nothing inside them. That
    is the state this most needs to detect, and a container check reports it as
    fine.
    """
    base = deployment_api_url.rstrip("/")
    try:
        async with httpx.AsyncClient() as client:
            datasets = await _deployed_datasets(client, base)
            if datasets is None:
                # The API is silent. That alone does not say WHICH kind of
                # silent, and the two are opposite findings: an environment
                # nobody has provisioned is in good order, while one whose
                # containers are up and whose API is dead is wedged and needs
                # `start`. Ask the CLI, which can see the containers -- one
                # subprocess, only on the path where something is already wrong.
                return await _observe_without_api(cli_path)
            env_type = await _environment_type(client, base)
            if not datasets:
                # The API serves, but nothing is deployed behind it. That is the
                # ordinary state of a fresh empty foundry, not a fault. No
                # descriptor read: there is nothing deployed to describe.
                return _observation(OBSERVED_SERVING, [], [],
                                    "the API is answering; no datasets are deployed",
                                    env_type)

            facts = await _descriptor_facts(client, base)

            dead: list[str] = []
            for dataset in datasets:
                dead.extend(await _dead_services(client, base, dataset))

            if dead:
                return _observation(OBSERVED_PARTIAL, datasets, dead,
                                    "not answering: " + ", ".join(dead), env_type, facts)
            return _observation(OBSERVED_SERVING, datasets, [],
                                "every deployed service is answering", env_type, facts)
    except Exception as exc:
        return _observation(OBSERVED_UNKNOWN, [], [],
                            f"the environment could not be interrogated: {exc}")


async def _observe_without_api(cli_path: str) -> dict[str, Any]:
    """What the CLI can still see when the deployment API has gone quiet."""
    status = await _run_cli(cli_path, ["foundry", "local", "status"], timeout=60)
    provisioned = _parse_provisioned(status["output"]) if status["code"] == 0 else None
    if provisioned is False:
        return _observation(OBSERVED_ABSENT, [], [],
                            "no environment is provisioned")
    if provisioned is True:
        return _observation(OBSERVED_DOWN, [], [],
                            "the containers are up but the deployment API is not "
                            "answering; the services need relaunching")
    # DAT-172's rule: "could not look" is not "nothing is here". Refuse to
    # report either way rather than guessing at the one that reads as healthy.
    return _observation(OBSERVED_UNKNOWN, [], [],
                        "the deployment API is not answering and the environment "
                        "state could not be established")


async def _descriptor_facts(client: httpx.AsyncClient, base: str) -> dict[str, Any]:
    """What was ASKED for: the symbols, the broker and the mode.

    These live only in the deployment descriptor, not in any of the endpoints
    the readiness probe already calls, so this is one more GET and a YAML parse
    on a path that runs once a minute in the background.

    ⚠️ Deliberately does NOT touch `env_type`. The descriptor's `mode` would
    give a second, independently-derived answer for the same field, and DAT-217
    chose to infer the type from the SYSTEMS actually deployed instead -- what
    is running beats what was requested, and two derivations of one field is a
    disagreement waiting to be reported to an operator as fact.

    Never raises: this is enrichment on a readiness probe, and a descriptor that
    cannot be read must not turn a serving environment into an unknown one.
    Empty values mean "not read", which is why the caller only publishes them
    when the API answered at all.
    """
    facts: dict[str, Any] = {"symbols": [], "broker": None, "mode": None}
    try:
        response = await client.get(f"{base}/datafye-api/v1/deployment/descriptor",
                                    timeout=OBSERVE_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return facts
        text = (response.json() or {}).get("descriptor", "")
        if not text or not text.strip():
            return facts
        descriptor = yaml.safe_load(text) or {}
    except Exception as exc:
        logger.debug("could not read the deployment descriptor: %s", exc)
        return facts

    symbols: list[str] = []
    for entry in descriptor.get("datasets") or []:
        for ticker in ((entry.get("symbols") or {}).get("tickers")) or []:
            if ticker not in symbols:
                symbols.append(ticker)
    facts["symbols"] = symbols
    facts["broker"] = (descriptor.get("broker") or {}).get("provider")
    facts["mode"] = descriptor.get("mode")
    return facts


def _observation(observed: str, datasets: list[str], dead: list[str], detail: str,
                 env_type: str | None = None,
                 facts: dict[str, Any] | None = None) -> dict[str, Any]:
    facts = facts or {}
    return {
        "observed": observed,
        "datasets": datasets,
        "not_answering": dead,
        "checked_at": int(time.time() * 1000),
        "detail": detail,
        # What was asked for, alongside what is running. Carried here so a
        # reader does not have to wait for a chat turn to learn what is
        # deployed: the SPA's sidebar was fed only by a POST-TURN event, so a
        # freshly opened workspace showed an empty panel and a partial one
        # showed as complete.
        "symbols": facts.get("symbols") or [],
        "broker": facts.get("broker"),
        "mode": facts.get("mode"),
        # None means "could not tell", NEVER "there is no environment" (DAT-217).
        # An admin column that renders those the same way turns "I could not
        # look" into "it is not working", which is the distinction the whole
        # readiness block exists to preserve.
        "env_type": _remember_env_type(env_type),
    }


def _remember_env_type(env_type: str | None) -> str | None:
    """Report the type we last managed to read when we cannot read it now."""
    global _last_env_type
    if env_type:
        _last_env_type = env_type
    return env_type or _last_env_type


async def _environment_type(client: httpx.AsyncClient, base: str) -> str | None:
    """'foundry' or 'trading', or None when it cannot be determined.

    Inferred from the deployed SYSTEMS rather than read from the deployment
    descriptor. Both would work, and the descriptor is the more authoritative
    source -- but the systems list is one call on an endpoint this function is
    already talking to, while the descriptor means a second read whose only
    purpose is a label. The tell is unambiguous: a trading environment stands up
    `datafye-broker-stocks-system` alongside the data systems, and a foundry
    never does.

    NB the names carry the version (`datafye-api-system-2.0.37`), so this
    matches on a substring rather than equality -- comparing whole names would
    work today and break on the next release, which is exactly the kind of
    silent version coupling this codebase keeps paying for.
    """
    try:
        response = await client.get(f"{base}/datafye-api/v1/deployment/systems",
                                    timeout=OBSERVE_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return None
        systems = response.json().get("systems")
        if not systems:
            # An empty list is a real answer -- an environment with nothing in
            # it -- but it does not say which KIND it would be, so the honest
            # report is still "could not tell".
            return None
        return "trading" if any("broker" in str(s) for s in systems) else "foundry"
    except Exception:
        return None


async def _deployed_datasets(client: httpx.AsyncClient, base: str) -> list[str] | None:
    """The deployed dataset names, or None when the API does not answer."""
    try:
        response = await client.get(f"{base}/datafye-api/v1/deployment/datasets",
                                    timeout=OBSERVE_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return None
        return list(response.json().get("datasets") or [])
    except Exception:
        return None


async def _dead_services(client: httpx.AsyncClient, base: str, dataset: str) -> list[str]:
    """The services of one dataset that are not answering.

    ⚠️ Reading this response right matters three times, and each one fails
    silently in the WRONG direction:

    - A healthy service reports an **empty** status, so a truthiness check reads
      exactly backwards -- it would call every healthy service dead.
    - A healthy service actually sends ``"status": null`` on the wire, because
      the ADM string is simply unset. ``entry.get("status") or ""`` folds that
      into the empty case on purpose; the equivalent Java trap is that a plain
      ``asText()`` on a JSON null returns the *string* ``"null"``, which would
      condemn every healthy service.
    - A response listing **no services at all** is not a pass. It means the API
      does not know about what we asked after, and treating silence as health is
      how a down service gets missed by the thing meant to notice it.

    The shape is ``{"datasets": [{"services": {"<name>": {"status": ...}}}],
    "trading": [...]}`` -- services is an OBJECT keyed by name, nested under
    per-system groups, not a flat list.
    """
    try:
        response = await client.get(
            f"{base}/datafye-api/v1/health/ping",
            params={"dataset": dataset},
            timeout=PING_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return [f"{dataset}: the API returned HTTP {response.status_code}"]
        body = response.json()
    except Exception as exc:
        return [f"{dataset}: {exc}"]

    dead: list[str] = []
    reported = 0
    for group in ("datasets", "trading"):
        for system in body.get(group) or []:
            for name, service in (system.get("services") or {}).items():
                reported += 1
                if ((service or {}).get("status") or "") != "":
                    dead.append(f"{dataset}/{name}")

    if reported == 0:
        return [f"{dataset}: the API reports no services"]
    return dead


async def observe_forever(deployment_api_url: str, cli_path: str = "datafye") -> None:
    """Keep the observation snapshot fresh, so /health never has to wait for it.

    Failures are swallowed and retried: this is a background refresher, and an
    exception escaping it would silently stop readiness updating for the life of
    the process -- a stale snapshot that looks live is worse than a stale one
    that says how old it is.
    """
    global _snapshot
    while True:
        try:
            _snapshot = await observe(deployment_api_url, cli_path)
        except Exception as exc:
            logger.warning("Foundry observation failed: %s", exc)
        await asyncio.sleep(OBSERVE_INTERVAL_SECONDS)


# ── Derivation: readiness from the three inputs ──────────────────────────


def read_foundry_state() -> dict[str, Any]:
    """Derived readiness, for /health.

    Never raises. This is on the /health path, which must answer even when
    everything else on the box is broken -- an agent that cannot report its
    health is indistinguishable from an instance that is simply dead, and that
    is the one distinction accounts needs most.
    """
    try:
        return derive(read_intent(), dict(_snapshot), in_flight_holder())
    except Exception as exc:
        return {
            "state": STATE_UNKNOWN,
            "intended": None,
            "intent_source": None,
            "observed": OBSERVED_UNKNOWN,
            "in_flight": None,
            "datasets": [],
            "not_answering": [],
            "checked_at": None,
            "reason": f"readiness could not be derived: {exc}",
            "error": None,
        }


def derive(intent: dict[str, Any], snapshot: dict[str, Any], in_flight: str | None) -> dict[str, Any]:
    """Combine the three inputs into one answer.

    Split out and pure so the truth table -- the part that is easy to get subtly
    backwards -- is testable without a deployment, a clock, or a filesystem.
    """
    intended = intent.get("intended")
    observed = snapshot.get("observed")

    if in_flight:
        # An operation owns the environment, so there is no settled state to
        # report and inventing one is the u1 failure. Judge nothing.
        state, reason = STATE_IN_PROGRESS, f"an operation is running: {in_flight}"
    elif intended == INTENT_STOPPED and observed in (OBSERVED_ABSENT, OBSERVED_SERVING,
                                                     OBSERVED_UNKNOWN):
        # ⚠️ Deliberately ready. A foundry the user asked to stop is in good
        # order; calling it unready would leave the box permanently unhealthy,
        # fixable only by starting an environment they explicitly did not want.
        # It is ready even if observation finds it SERVING -- somebody started it
        # by hand, which is more than intended, not less.
        #
        # ⚠️ BUT NOT WHEN THE ENVIRONMENT IS WEDGED. This clause used to
        # short-circuit before looking at the observation at all, so ANY
        # environment on a box with intent=stopped reported ready -- including
        # one the CLI called DEGRADED, containers up and the API dead. Seen
        # live, and doubly misleading because the intent itself was wrong
        # (DAT-222 read "kill the app" as a decision to stop the environment):
        # a misclassified intent silenced the health signal that would have
        # exposed it. "More than intended" is fine; "broken" is not a thing to
        # call ready under any intent, because nobody asked for that either.
        state, reason = STATE_READY, "the foundry is stopped, which is what was asked for"
    elif observed == OBSERVED_SERVING:
        state, reason = STATE_READY, snapshot.get("detail") or "the environment is serving"
    elif observed == OBSERVED_ABSENT:
        # Reached only when intent is RUNNING (the stopped case is handled
        # above), so this is a real mismatch: there should be an environment
        # and there is none. Before ABSENT existed this arrived as DOWN and
        # answered not_ready; without this branch it would fall through to
        # UNKNOWN, which would be a quieter and less true answer than the one
        # it replaced.
        state, reason = STATE_NOT_READY, "no environment is provisioned, but one should be running"
    elif observed == OBSERVED_PARTIAL:
        state, reason = STATE_NOT_READY, snapshot.get("detail") or "some services are not answering"
    elif observed == OBSERVED_DOWN:
        state, reason = STATE_NOT_READY, "the environment should be running but is not answering"
    else:
        state, reason = STATE_UNKNOWN, snapshot.get("detail") or "the environment has not been interrogated"

    return {
        "state": state,
        "intended": intended,
        "intent_source": intent.get("source"),
        "observed": observed,
        "in_flight": in_flight,
        "datasets": snapshot.get("datasets") or [],
        # The descriptor's view of the same environment: what was asked for.
        # Empty/None means "not read", not "none configured" -- they are only
        # populated on a snapshot where the deployment API answered.
        "symbols": snapshot.get("symbols") or [],
        "broker": snapshot.get("broker"),
        "mode": snapshot.get("mode"),
        "not_answering": snapshot.get("not_answering") or [],
        "checked_at": snapshot.get("checked_at"),
        # Passed straight through, null included: accounts renders this as its
        # own column (DAT-217), and null must reach it as null so it can show
        # "unknown" rather than defaulting to a type it was never told.
        "env_type": snapshot.get("env_type"),
        "reason": reason,
        # Kept so a reader written against the earlier shape does not KeyError.
        # Failure detail lives in the report under ~/.datafye/logs, written by
        # whoever ran the command -- it was never this block's to carry.
        "error": None,
    }


def is_ready(state: dict[str, Any]) -> bool:
    """Whether the foundry matches its intended state.

    Deliberately NOT "is the foundry running": see the stopped branch above.
    """
    return state.get("state") == STATE_READY


def describe_for_model(state: dict[str, Any]) -> str:
    """A sentence for the system prompt telling the model what it can rely on.

    The model is told the state AND the reason, so it can explain the situation
    rather than discovering it by colliding with it. That is the whole failure
    this addresses: the information existed, nothing consumed it.
    """
    name = state.get("state")
    intended = state.get("intended")
    reason = state.get("reason")

    if name == STATE_IN_PROGRESS:
        return (
            f"The foundry on this box is NOT ready: {reason}. Do NOT start, apply, provision "
            "or otherwise change the environment while that is running - a second operation on "
            "one foundry is what corrupts it. Tell the user the environment is busy and roughly "
            "what it is doing, then wait or ask them to try again shortly."
        )
    if name == STATE_READY and intended == INTENT_STOPPED:
        return (
            "The foundry on this box is provisioned but deliberately STOPPED. "
            "This is a healthy state, not a fault. Start it before any data work, "
            "and tell the user you are doing so."
        )
    if name == STATE_READY:
        return "The foundry on this box is ready to use."
    if name == STATE_NOT_READY:
        return (
            f"The foundry on this box is NOT ready: {reason}. It is meant to be running. "
            "Check 'datafye foundry local status' first, then bring it back with "
            "'datafye foundry local start', which relaunches only what is down and does NOT "
            "destroy the deployed datasets. Read the newest report under ~/.datafye/logs before "
            "rebuilding, and tell the user what you found rather than that 'there is a problem'."
        )
    if name == STATE_ABSENT:
        return (
            "There is no foundry on this box, and none is intended. Any data work needs one "
            "provisioned first - tell the user before doing it."
        )

    return (
        f"The readiness of the foundry on this box is UNKNOWN ({reason}). Check with "
        "'datafye foundry local status' before assuming an environment exists, and do not "
        "assume it is broken either."
    )


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
