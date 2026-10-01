"""What kind of work each model call did, and telling the metering gateway (DAT-317 / DAT-319).

Usage used to be attributed to a "stage": where the project was in a lifecycle, classified once per
turn, ratcheted forward, and used as the key the turn's whole cost was filed under. That conflated
two different things. (Here it was worse still: the classifier inferred an INTENT, a per-intent TRACK
and a STAGE within it, all three existing only to drive the workspace stepper.)

A project's position is a claim we are mostly wrong about. People work in a spiral, moving between
exploring, building and validating continuously, so "this project is in Backtest" is wrong more often
than it is right, and stating it is worse than saying nothing. **A call, by contrast, is not ambiguous.**
The thinking it did, the text it wrote and the tools it invoked say what that call was doing, and
nothing that happens later changes it. So the label belongs to the call, and it is immutable by
construction rather than by a ratchet.

That has a pleasant consequence for correctness: a batch classified one minute into a twelve-hour
turn is exactly as good as one classified at the end, because there is no later evidence that could
legitimately revise it. Which is what makes pushing labels mid-turn sound.

How it fits together::

    gateway meters each call, holds the record   ─┐
    agent buffers evidence per call               │  joined on message_id
    agent classifies a batch (one cheap call)     │
    agent POSTs /gateway/label                   ─┘  → gateway releases the record, labelled

⚠️ The agent classifies and does NOT attribute usage per call. `ResultMessage.model_usage` is per
model per TURN, so any per-activity split computed here would be an apportionment - an estimate, for
no gain, since the gateway meters every call exactly and now knows each one's activity. The measured
breakdown is the gateway's. The agent's own usage reporting is unchanged by all of this: per model,
per turn, as it always was.

The cadence matters more than it looks. The gateway sizes its hold from the cadence we report, NOT
from how long a turn takes - turns have run for twelve hours and will run for days, so nothing can be
sized against them. Pushing every ``age_seconds`` bounds the wait regardless of turn length, and the
gateway's reply tells us the hold it settled on so we can tighten our own deadline to stay inside it.

⚠️ Two things are deliberately NOT done here. We do not classify against "where the turn ended up":
each call is judged on the evidence around it. And we do not send ``unclassified`` - that word belongs
to the gateway's release path, so it keeps meaning "nobody told us", which is the signal that this
whole mechanism has stopped working.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger("datafye.activity")

# The vocabulary offered to the classifier: ONE flat set. There are no per-intent tracks any more - a
# dashboard and an algo draw from the same words, and the words describe work, not a position.
#
# ⚠️ "Ship" is ABSENT, deliberately: `Deploy` absorbs what it meant in the old non-trading track
# (standing up a dashboard or a tool for real use), so one word covers going live for every artifact.
# And the old track's "Idea" is long gone: `Explore` is the opener.
VOCABULARY = ("Explore", "Design", "Build", "Backtest", "Validate", "Deploy")

# The agent's own cheap model calls - titling, classifying, reading satisfaction. They are platform
# overhead rather than work on the user's project, and they deserve their own bucket: without one
# they would be the largest contributor to `unclassified`, and that word has to keep meaning
# "labelling is broken".
SIDECAR_ACTIVITY = "Platform"

# The gateway's own words for the absence of a label, never SENT from here - but `unlabelled` is used
# locally for a turn that nothing was ever going to classify, which is exactly what it means there.
UNCLASSIFIED = "unclassified"
UNLABELLED = "unlabelled"

# Classify when this many calls have piled up. Cost scales with the number of BATCHES, not calls, so
# a busy turn should produce a handful of classification calls rather than one a minute.
BATCH_CALLS = 25
# ...or when the oldest unlabelled call is this old, whichever comes first. This is the trigger that
# bounds latency on a slow turn, and it is what we report to the gateway as our cadence.
DEFAULT_AGE_SECONDS = 60.0
# Headroom under the gateway's hold: if it tells us it holds for H, we must push comfortably inside
# H or our labels arrive after the record has already gone.
#
# ⚠️ This is NOT the gateway's `usage_hold_multiplier`, even though both are 3 today. The deadline is
# always recomputed from the CONFIGURED age rather than from the current one, because tightening the
# current value on every push is a feedback loop: if the gateway's multiplier were 2, each reply would
# shrink the deadline by a further third (60 -> 40 -> 26.7 -> ...) until there was one classification
# call per model call. And a floor, because arithmetic should not be able to make this absurd.
HOLD_SAFETY_DIVISOR = 3.0
MIN_AGE_SECONDS = 10.0
# How often the background ticker asks "is a batch due?". Without it the question is only asked when
# the next model message arrives, and during a long tool run (a backtest, a history fetch, a 17-minute
# provision) none arrives - so labels sat past the gateway's hold and their records were released as
# `unclassified`, exactly on the turns this exists for.
TICK_SECONDS = 5.0
# An `unmatched` label is retried ONCE, after this pause. The gateway writes a call's record when that
# call's upstream stream ENDS, while the SDK hands us its blocks as they finish, so a label can arrive a
# moment before its record exists. Once is enough for that race; a label that is still unmatched is a
# real join failure and retrying it forever would hide it.
UNMATCHED_RETRY_SECONDS = 2.0
# How many passes the end-of-turn settle-up may make. Each pass can create work for the next (a
# classification is itself a metered call to label; a push can hand back a label owed its retry), so
# one or two fixed passes dropped whatever the last one produced. Bounded, because the retry rules
# already guarantee the work runs out; anything still owed after this is logged, never dropped silently.
MAX_FINAL_PASSES = 4

# Evidence per call sent to the classifier. Enough to tell design talk from a build step; short
# enough that 25 of them stay cheap.
#
# ⚠️ Budgeted PER FIELD, not as one total over the joined string. Thinking is written first and can
# easily fill 700 characters on its own, which dropped the call's own text entirely - and the
# Backtest-vs-Validate traps turn on exactly what the call SAID ("Sharpe 1.4 out of sample"), so a
# single shared budget degraded the cases this exists to get right. Tools are unbudgeted: they are a
# handful of short names and the most decisive signal there is.
# Per field. The per-call ceiling is their SUM (about 1.4k), not any one of them -- an earlier name
# for the first of these read as a whole-call bound and was not one.
MAX_TOOLS_CHARS = 700
MAX_SAID_CHARS = 380
MAX_THINKING_CHARS = 320
MAX_EVIDENCE_CHARS = MAX_TOOLS_CHARS + MAX_SAID_CHARS + MAX_THINKING_CHARS

_PROMPT = (
    "You label ONE model call at a time with the kind of work it did, for cost reporting in an AI "
    "quant workspace where people build trading algorithms, signals, dashboards and research.\n\n"
    "The activities:\n\n"
    "- Explore: understanding the question, or work that produces no build artifact. Clarifying "
    "what the user wants; looking at data to see what is there; one-off research or analysis that "
    "ends in a report, a chart or an answer; explaining a concept; any question about Datafye "
    "itself, its datasets, its CLI or its pricing.\n"
    "- Design: deciding HOW to make it. Choosing an approach, a strategy's rules, signals, data, "
    "parameters or architecture; weighing options; writing or revising a plan; reading existing "
    "code in order to decide how to change it.\n"
    "- Build: making it exist. Writing or editing code, config, descriptors or schemas; setting up "
    "the environment, datasets or history the work needs; installing dependencies; fixing an error "
    "so the thing will run at all; running tests on what was just written.\n"
    "- Backtest: running a strategy or signal over historical data and reading what came back. "
    "Running a backtest or a replay; computing and reading its returns, drawdowns or trades; "
    "tuning parameters by re-running it.\n"
    "- Validate: checking that results hold up BEFORE anything real depends on them. Paper "
    "trading; out-of-sample, walk-forward or robustness tests; stress and sanity checks on a "
    "result; comparing against a benchmark to decide whether to trust it.\n"
    "- Deploy: putting it into real use. Going live with real money for an algo; publishing a "
    "signal; standing up a dashboard or tool for the user to use; connecting a broker account "
    "in order to trade.\n\n"
    "The traps, because the plain reading of the words pulls the wrong way:\n\n"
    "- Running a backtest is Backtest, not Validate, however carefully it is done. Validate is "
    "about whether a result already in hand can be trusted.\n"
    "- Advising on going live, or explaining what it would take, is NOT Deploy. Deploy is doing "
    "it. Advice about it is Validate if it is weighing whether the strategy is ready, Explore "
    "otherwise.\n"
    "- A bare acknowledgement (\"done\", \"ok, stopping it\", \"thanks\") is not an activity of "
    "its own: give it the activity of the work it is wrapping up.\n"
    "- Reading code is Design when the point is to decide an approach, and Build when the point is "
    "the edit in hand. Judge it by what the call was reaching for.\n"
    "- Fetching history or adding a dataset so a backtest can run is Build. Pulling data to look at "
    "it or answer a question is Explore.\n"
    "- Explore is the catch-all opener: a conversation that never becomes a project, a one-off "
    "analysis, a question about Datafye. Use it rather than straining to fit another word.\n\n"
    "Judge EACH call on its own evidence. Do NOT reason about where the project has got to overall, "
    "and do NOT let a later call change an earlier one: a call that did design work is Design even "
    "if the project is mostly built. When a call shows two activities, pick the one its WORK served, "
    "not the one mentioned first - a call that read three files and then wrote one is Build.\n\n"
    "Reply with ONLY a JSON array of strings, one per call, in the same order, no prose and no "
    "markdown fences. Each string must be exactly one of: "
    + ", ".join(VOCABULARY) + ".\n\n"
    "The calls:\n\n"
)


def _evidence(call: dict) -> str:
    """One call's evidence, in the order that is most discriminative first.

    Tool names lead because they settle most cases outright and cost almost nothing: a call that
    ran the backtest command ran a backtest, with nothing to infer. Names only - never a
    tool's INPUT, which would put file contents and user data through a classifier.
    """
    bits = []
    if call["tools"]:
        bits.append("tools: " + ", ".join(sorted(set(call["tools"])))[:MAX_TOOLS_CHARS])
    if call["thinking"]:
        bits.append("thinking: " + " ".join(call["thinking"])[:MAX_THINKING_CHARS])
    if call["texts"]:
        bits.append("said: " + " ".join(call["texts"])[:MAX_SAID_CHARS])
    if not bits:
        bits.append("(no text, no thinking, no tools)")
    return " | ".join(bits)


class Collector:
    """Buffers per-call evidence for one turn, classifies it in batches, and labels the gateway.

    One instance per turn. Every method is safe to call when there is no gateway configured, in which
    case this does nothing at all and the gateway (if one appears later) releases records as
    ``unclassified`` - the degradation is visible rather than silent.
    """

    def __init__(self, gateway_url: Optional[str], token: Optional[str], model: str,
                 base_url: Optional[str] = None, age_seconds: float = DEFAULT_AGE_SECONDS,
                 usage_sink: Optional[list] = None):
        self._gateway = (gateway_url or "").rstrip("/")
        self._token = token
        self._model = model
        # Classification is a direct API call like the title sidecar, so it follows the same base
        # URL - which means it is metered too, and gets labelled SIDECAR_ACTIVITY in its turn.
        self._base = (base_url or self._gateway or "").rstrip("/")
        self._age = age_seconds
        self._calls = {}            # message_id -> evidence, deduped
        self._order = []            # message_ids in arrival order, so labels line up positionally
        self._fixed = {}            # message_id -> activity, for calls needing no classification
        self._oldest = None
        self._lock = asyncio.Lock()
        self._task = None
        self._seen = {}     # activity -> True, insertion-ordered, deduped
        self._configured_age = age_seconds
        # Which activities this turn's calls were classified as.
        # Where the classifier's OWN spend is reported, alongside the title and satisfaction
        # sidecars. Without it this call would be the one metered call in the turn that never
        # reaches the project's usage or the accounts roll-up.
        self._usage_sink = usage_sink
        # ⚠️ Calls that may still be STREAMING. The SDK emits one AssistantMessage per content block,
        # so a call's thinking can arrive well before its tool_use - and the gateway only writes the
        # call's record once its stream ends. Flushing an open call classified it on half its evidence
        # (usually without the tool name, the strongest signal), pushed a label the gateway could not
        # match yet, and then classified its remaining blocks AGAIN as a fresh entry.
        #
        # ⚠️ Tracked PER THREAD (`parent_tool_use_id`, None for the main agent), not as one set. Calls
        # within a thread are sequential, so a new call in a thread proves that thread's previous one
        # ended; but subagents run IN PARALLEL on the same stream, and with a single set one
        # subagent's call "closed" another's while it was still streaming - the bug back again.
        # A call leaves this map when its own thread's stream ends (`settle`), when its thread starts
        # another call, or at the end of turn.
        self._open = {}
        # Calls already taken by a flush. A late block for one of them is ignored rather than becoming
        # a second entry that is classified and paid for twice. Recorded when the flush TAKES the call,
        # not when its label lands, because classifying takes seconds and a block can arrive meanwhile.
        self._done = set()
        # Labels that have had their one retry, after `unmatched` or `in_flight`, and the earliest
        # moment that retry is worth making (the gateway needs a moment to write the record).
        self._retried = set()
        self._retry_not_before = 0.0
        self._ticker = None
        self.pushed = 0
        self.classified = 0
        self.discarded_batches = 0

    @property
    def classifying(self) -> bool:
        """Whether calls get an activity at all.

        ⚠️ A self-hosted box on its OWN key is not classified by default, because classifying costs a
        model call per batch and that would be spending the user's money on something they did not
        ask for. It has no gateway, so there are no records to label either. Such a turn is reported
        under `unlabelled`, which the app explains as "detailed cost reporting is not switched on" -
        honest, and free. (The workspace's copy for that state is DAT-320's.)

        An earlier version classified whenever a key was present. That was a fix for a REAL bug (with
        nothing classified, the turn's whole cost was filed under `unclassified`, telling a self-host
        user their reporting was broken) but it fixed it by spending their money. The bug is fixed by
        the WORD instead: `unlabelled` is the normal state, `unclassified` is the fault.

        `DATAFYE_AGENT_CLASSIFY_ACTIVITY=1` opts a self-hosted box in, for a user who does want the
        breakdown and is content to pay the classifier calls for it.
        """
        if not (self._token and self._base):
            return False
        return bool(self._gateway) or _opted_in()

    @property
    def enabled(self) -> bool:
        """Whether labels can be pushed. Needs a gateway to push them to."""
        return bool(self._gateway and self._token)

    @property
    def activities(self) -> list:
        """WHICH kinds of work this turn's calls did.

        ⚠️ A list, not a cost split. An earlier version tracked a per-call token weight here so a
        turn's usage could be apportioned across activities - which made the agent's own figures
        an ESTIMATE for no gain, because the gateway meters every call exactly and now knows each
        one's activity. The measured breakdown belongs there. This exists only so a client can
        see that the classification happened.
        """
        return list(self._seen)

    def observe(self, message_id: Optional[str], thinking=(), texts=(), tools=(),
                thread: Optional[str] = None) -> None:
        """Record what one model call did. Safe to call repeatedly for the same call.

        ⚠️ The SDK can emit several AssistantMessages carrying the SAME message_id (one per content
        block), so evidence is MERGED per message id rather than producing several calls.
        """
        if not message_id or not self.classifying:
            # ⚠️ The second half matters for memory, not just for cost: nothing drains `_calls` when
            # there is nothing to classify, so a multi-hour turn would accumulate every call's
            # thinking and text for its whole length.
            return
        if message_id in self._done:
            return
        # A NEW call in a thread means that thread's previous call has finished streaming: within one
        # thread the model makes its calls one after another. Other threads are untouched.
        self._open[thread] = message_id
        self._start_ticker()
        call = self._calls.get(message_id)
        if call is None:
            call = {"thinking": [], "texts": [], "tools": []}
            self._calls[message_id] = call
            self._order.append(message_id)
            if self._oldest is None:
                self._oldest = time.monotonic()
        # Capped HERE rather than only where the evidence is rendered: a single call can carry more
        # prose than the classifier will ever see, and holding all of it until then is pure cost.
        # Per field, matching the render budget, so one long field cannot crowd out another.
        budget = {"thinking": MAX_THINKING_CHARS, "texts": MAX_SAID_CHARS,
                  "tools": MAX_TOOLS_CHARS}
        for field, values in (("thinking", thinking), ("texts", texts), ("tools", tools)):
            room = budget[field] - sum(len(x) for x in call[field])
            for t in values:
                if t and room > 0:
                    call[field].append(t[:room])
                    room -= len(t)

    def settle(self, thread: Optional[str] = None) -> None:
        """The current call in ``thread`` has finished streaming (the caller saw its `message_stop`).

        From here that call is eligible for a mid-turn flush. Its evidence is complete and the gateway
        has, or is about to have, its record. Other threads' calls are unaffected."""
        self._open.pop(thread, None)

    def _is_open(self, message_id: str) -> bool:
        return message_id in self._open.values()

    def _start_ticker(self) -> None:
        if self._ticker is not None or not self.classifying:
            return
        try:
            self._ticker = asyncio.ensure_future(self._tick())
        except RuntimeError:
            self._ticker = None         # no running loop (a synchronous caller); flush_soon still works

    async def _tick(self) -> None:
        """Ask "is a batch due?" on a clock, not only when a model message happens to arrive."""
        try:
            while True:
                await asyncio.sleep(min(TICK_SECONDS, max(1.0, self._age / 6)))
                self.flush_soon()
        except asyncio.CancelledError:
            pass

    def observe_sidecar(self, message_id: Optional[str]) -> None:
        """One of the agent's own cheap calls. Labelled without asking a model to classify it: we
        know exactly what it was, and paying a classifier call to label a classifier call would be
        an amusing way to spend money."""
        if not message_id:
            return
        if message_id in self._done:
            return
        self._fixed[message_id] = SIDECAR_ACTIVITY
        self._seen[SIDECAR_ACTIVITY] = True
        self._start_ticker()
        if self._oldest is None:
            # ⚠️ Or a turn that goes quiet after a mid-turn flush has nothing to trigger on, and the
            # classifier's own record expires to `unclassified`.
            self._oldest = time.monotonic()
        # the main model's cost under Platform AND count the sidecars a second time.

    def due(self) -> bool:
        # Only calls whose stream has ENDED count: an open call cannot be flushed, so it cannot make a
        # batch due either.
        pending = sum(1 for mid in self._calls if not self._is_open(mid)) + len(self._fixed)
        if not pending:
            return False
        if pending >= BATCH_CALLS:
            return True
        return self._oldest is not None and (time.monotonic() - self._oldest) >= self._age

    def flush_soon(self) -> None:
        """Start a push in the background if one is due.

        ⚠️ Deliberately not awaited: this is called from inside the loop that is streaming the
        agent's reply to the browser, and blocking there on an HTTP round trip would stall the
        stream. A reference is kept so the task cannot be garbage-collected mid-flight.
        """
        if not self.classifying or not self.due():
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.ensure_future(self._flush())

    async def finish(self) -> None:
        """End of turn: let any in-flight push complete, then classify and push whatever is left."""
        if not self.classifying:
            return
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        # The turn is over, so every stream has ended.
        self._open = {}
        if self._task is not None:
            try:
                await self._task
            except Exception as e:      # noqa: BLE001 - never let labelling break a turn
                logger.warning("Activity label push failed: %s", e)
            self._task = None
        # ⚠️ Pass until nothing is owed. A flush classifies, and classifying makes one more metered
        # call that registers itself as a sidecar to be labelled; a push can hand back a label owed its
        # one retry. Two fixed passes dropped whatever the second produced, silently. A retry waits
        # until it is worth making, however soon after a mid-turn push the turn happened to end.
        for _ in range(MAX_FINAL_PASSES):
            if not (self._fixed or self._calls):
                break
            wait = self._retry_not_before - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            await self._flush()
        owed = len(self._fixed) + len(self._calls)
        if owed:
            logger.warning("%d activity label(s) still owed at the end of the turn; the gateway will "
                           "release those records as `unclassified`", owed)

    async def _flush(self) -> None:
        async with self._lock:
            # A call still streaming stays behind for the next flush (see `_open`).
            to_classify = [(mid, self._calls[mid]) for mid in self._order
                           if mid in self._calls and not self._is_open(mid)]
            fixed = dict(self._fixed)
            if not to_classify and not fixed:
                return
            for mid, _call in to_classify:
                del self._calls[mid]
            self._order = [mid for mid in self._order if mid in self._calls]
            self._fixed = {}
            self._done.update(mid for mid, _call in to_classify)
            self._done.update(fixed)
            self._oldest = time.monotonic() if self._calls else None

        labels = list(fixed.items())
        if to_classify:
            decided, retry = await self._classify(to_classify)
            if retry:
                # ⚠️ A TRANSPORT failure is not the same as a bad answer. A 429 or a timeout on the
                # classify call forfeited that batch for good - the buffer was already drained here -
                # even though the gateway holds those records for about three times the cadence and
                # would still accept the labels a minute later. Put them back and let the next flush
                # try again. A COUNT MISMATCH is deliberately NOT retried: that answer was wrong
                # rather than absent, and asking the same question again invites the same wrongness.
                await self._requeue(to_classify)
                if not labels:
                    return
                to_classify, decided = [], []
        if to_classify:
            for (mid, _call), act in zip(to_classify, decided):
                if not act:
                    # Nothing was decided about this call, so nothing is recorded here: the gateway
                    # releases its record as `unclassified`, which is the honest answer.
                    continue
                labels.append((mid, act))
                self._seen[act] = True
        # Classified either way; pushed only when there is a gateway holding records to label.
        if labels and self.enabled:
            if not await self._push(labels):
                # ⚠️ Same reasoning as a failed classification, one step later. A push that never
                # landed means the gateway releases those records as `unclassified` even though they
                # WERE classified, so the breakdown loses work that was correctly identified. The
                # records are still held, so the next flush gets another go.
                await self._requeue_labels(labels)

    async def _requeue(self, batch: list) -> None:
        """Put unclassified calls back, oldest-first, for the next flush to try again."""
        async with self._lock:
            for mid, call in batch:
                if mid not in self._calls:          # a later observation of it wins, not this one
                    self._calls[mid] = call
                    self._order.insert(0, mid)
            if self._oldest is None:
                self._oldest = time.monotonic()

    async def _requeue_labels(self, labels: list) -> None:
        """Put already-decided labels back, so a failed push costs a retry rather than the label."""
        async with self._lock:
            for mid, act in labels:
                self._fixed.setdefault(mid, act)
            if self._oldest is None:
                self._oldest = time.monotonic()

    async def _classify(self, batch: list) -> list:
        """One cheap model call for the whole batch. Returns one label per call, or None per call.

        Returns ``(labels, retry)``. ``retry`` is True only when the answer is ABSENT (a transport
        error, a timeout) rather than WRONG: the caller puts those calls back for the next flush,
        because the gateway still holds their records.

        ⚠️ On a COUNT mismatch the whole batch is discarded and NOT retried. A shifted mapping files
        one call's cost under another call's activity and looks perfectly healthy; an unlabelled call
        shows up as `unclassified`, which is honest and recoverable. And asking the same question
        again invites the same wrong answer - it was wrong, not missing.
        """
        if not self._token or not self._base:
            return [None] * len(batch), False
        numbered = "\n".join(
            "%d. %s" % (i + 1, _evidence(call)) for i, (_mid, call) in enumerate(batch))
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    f"{self._base}/v1/messages",
                    headers={"x-api-key": self._token, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": self._model,
                          # 12 characters per label is ample for the longest word plus its quoting.
                          "max_tokens": 16 + 12 * len(batch),
                          "messages": [{"role": "user", "content": _PROMPT + numbered}]})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:      # noqa: BLE001 - unlabelled is a recoverable outcome
            logger.warning("Activity classification failed for %d call(s): %s", len(batch), e)
            return [None] * len(batch), True

        # This call is itself metered by the gateway, so label it rather than leaving it to expire
        # into `unclassified`.
        self.observe_sidecar(data.get("id"))
        # And reported like the title and satisfaction sidecars, so it lands in the project's usage,
        # the accounts roll-up and the per-turn figure rather than being the one metered call in the
        # turn that nothing counts.
        if self._usage_sink is not None and data.get("usage"):
            self._usage_sink.append({"model": self._model, "usage": data["usage"],
                                     "message_id": data.get("id")})
        text = "".join(p.get("text", "") for p in data.get("content", [])
                       if p.get("type") == "text")
        parsed = _parse_labels(text)
        if parsed is None or len(parsed) != len(batch):
            self.discarded_batches += 1
            logger.warning("Activity classification returned %s labels for %d call(s); discarding "
                           "the batch so nothing is mislabelled",
                           "no usable" if parsed is None else len(parsed), len(batch))
            return [None] * len(batch), False
        self.classified += len(batch)
        return [p if p in VOCABULARY else None for p in parsed], False

    async def _send_labels(self, body: dict):
        """The HTTP half of a push: ``(status, parsed body)``. Separate so the reply handling in
        `_push`, where the retry rules live, can be tested without a gateway."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self._gateway}/gateway/label", json=body,
                headers={"x-api-key": self._token, "content-type": "application/json"})
        return resp.status_code, (resp.json() if resp.content and resp.status_code < 400 else {})

    async def _push(self, labels: list) -> bool:
        """True when the gateway accepted the push. False means the labels are still owed."""
        # ⚠️ The CONFIGURED interval, not the tightened one. The gateway needs an upper bound on how
        # long it may have to wait, and the configured age is exactly that: the count trigger only
        # ever makes pushes more frequent. Reporting the tightened value instead closes a feedback
        # loop - the gateway sizes its hold from what we report, we tighten from its hold - and the
        # deadline then shrinks on every push until there is one classification call per model call.
        body = {"push_interval_seconds": self._configured_age,
                "calls": [{"message_id": mid, "activity": act} for mid, act in labels]}
        try:
            status, out = await self._send_labels(body)
            if status >= 400:
                logger.warning("Gateway label push returned %s for %d call(s)", status, len(labels))
                # 4xx other than 429 is our own fault and will not fix itself; retrying a malformed
                # push every minute for the rest of the turn helps nobody.
                return status < 500 and status != 429
        except Exception as e:      # noqa: BLE001
            logger.warning("Gateway label push failed for %d call(s): %s", len(labels), e)
            return False

        self.pushed += len(labels)
        self._done.update(mid for mid, _a in labels)
        unmatched = out.get("unmatched") or []
        late = out.get("already_delivered") or []
        # Owed ONE more try: `unmatched` can be a label that beat its own record to the gateway, and
        # `in_flight` is a record being posted right now that becomes labellable again if that post
        # fails. A label still unmatched after its retry is a real join failure.
        retry = [mid for mid in list(unmatched) + list(out.get("in_flight") or [])
                 if mid not in self._retried]
        if retry:
            self._retried.update(retry)
            owed = set(retry)
            await self._requeue_labels([(m, a) for m, a in labels if m in owed])
            self._retry_not_before = time.monotonic() + UNMATCHED_RETRY_SECONDS
        given_up = [mid for mid in unmatched if mid not in set(retry)]
        if given_up:
            # Not cosmetic: an unmatched label means that cost is filed under `unclassified` for
            # good, so this is the line that says the join is broken.
            logger.warning("Gateway did not recognise %d labelled call(s): %s",
                           len(given_up), given_up[:5])
        if late:
            logger.info("%d call(s) were already delivered before we labelled them", len(late))
        # Tighten our own deadline to stay inside whatever hold the gateway settled on. It may have
        # clamped the cadence we asked for, and finding that out from the reply beats assuming.
        hold = out.get("hold_seconds")
        if isinstance(hold, (int, float)) and hold > 0:
            # From the CONFIGURED age, never from the current one: deriving it from the current value
            # compounds on every push and shrinks without bound whenever the gateway's multiplier is
            # smaller than the divisor here.
            tightened = max(MIN_AGE_SECONDS, min(self._configured_age, hold / HOLD_SAFETY_DIVISOR))
            if tightened != self._age:
                logger.info("Gateway holds records for %.0fs; pushing labels every %.0fs",
                            hold, tightened)
                self._age = tightened
        # Anything the gateway says it could not apply is owed, not done.
        not_applied = out.get("not_applied") or []
        if not_applied:
            logger.warning("Gateway applied none of %d label(s); will retry", len(not_applied))
            owed = {mid for mid in not_applied}
            await self._requeue_labels([(m, a) for m, a in labels if m in owed])
        return True


def _parse_labels(text: str):
    """A JSON array of strings, tolerant of fences and stray prose around it."""
    if not text:
        return None
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        arr = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(arr, list):
        return None
    return [s.strip() if isinstance(s, str) else None for s in arr]


def _opted_in() -> bool:
    """A self-hosted box asking to be classified anyway, on its own key."""
    return (os.environ.get("DATAFYE_AGENT_CLASSIFY_ACTIVITY") or "").strip().lower() in ("1", "true", "yes")


def gateway_from_env(default_base: str) -> Optional[str]:
    """The gateway URL, or None when model calls go straight to the provider.

    Taken from ANTHROPIC_BASE_URL, which `main._apply_gateway_url` sets from the accounts-SIGNED
    bootstrap claim and from nowhere else. A box talking directly to the provider has no records to
    label, so everything here stays switched off.
    """
    base = (os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    if not base or base == default_base.rstrip("/"):
        return None
    return base
