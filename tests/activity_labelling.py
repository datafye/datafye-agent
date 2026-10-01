#!/usr/bin/env python3
"""The mechanics of per-call activity labelling (DAT-319, ported from SUT-114). Offline and deterministic.

The companion `activity_classifier.py` asks a real model whether it labels turns correctly, which
costs money and is not always right. THIS file asserts the parts that must never be wrong whatever
the model says: that a shifted mapping can never be applied, that one call's evidence is not counted
twice, that a batch goes when it is due, and that apportionment adds back up to the whole.

    python tests/activity_labelling.py

Exits non-zero on any failure.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import activity  # noqa: E402

FAILED = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILED.append(what)


class StubCollector(activity.Collector):
    """A Collector whose model and gateway are replaced by recordings.

    Subclassed rather than monkeypatched so the real batching, dedupe, discard and weight logic is
    the code under test - only the two network calls are stood in for.
    """

    def __init__(self, replies=None, **kw):
        kw.setdefault("gateway_url", "http://gw")
        kw.setdefault("token", "gwt1.test")
        kw.setdefault("model", "haiku")
        # In production this always comes from `_anthropic_base()`, which falls back to the provider,
        # so it is never empty. The stub has to supply it or `classifying` reads false for the wrong
        # reason and a test about having no GATEWAY would pass by having no BASE URL either.
        kw.setdefault("base_url", "http://api")
        super().__init__(**kw)
        self.replies = list(replies or [])
        self.classify_calls = []
        self.pushes = []
        self.classify_fails = 0
        self.push_fails = 0

    async def _classify(self, batch):
        self.classify_calls.append([mid for mid, _ in batch])
        if self.classify_fails:
            self.classify_fails -= 1
            return [None] * len(batch), True        # absent, not wrong: retryable
        reply = self.replies.pop(0) if self.replies else None
        if reply is None:
            return [None] * len(batch), False
        # Go through the real parsing and the real count check, so a stub cannot make a bug
        # disappear by handing back a clean list.
        parsed = activity._parse_labels(json.dumps(reply))
        if parsed is None or len(parsed) != len(batch):
            self.discarded_batches += 1
            return [None] * len(batch), False       # wrong, not absent: never retried
        self.classified += len(batch)
        return [p if p in activity.VOCABULARY else None for p in parsed], False

    async def _push(self, labels):
        self.pushes.append(list(labels))
        if self.push_fails:
            self.push_fails -= 1
            return False
        self.pushed += len(labels)
        return True


async def test_a_shifted_mapping_is_never_applied():
    """A count mismatch discards the WHOLE batch. A shifted mapping files one call's cost under
    another call's activity and looks perfectly healthy; an unlabelled call shows up as
    `unclassified`, which is honest and recoverable."""
    c = StubCollector(replies=[["Build", "Design"]])       # two labels for three calls
    for i in range(3):
        c.observe(f"msg_{i}", tools=["Write"])
    await c.finish()
    check(c.pushes == [], "nothing is pushed when the label count does not match")
    check(c.discarded_batches == 1, "the discard is counted, not silent")
    # ⚠️ The weight goes to `unclassified`, NOT nowhere. Dropping it redistributes the discarded
    # calls' cost across whatever else was labelled, which is how a turn with three heavy discarded
    # calls and one light `Explore` call files the whole turn under Explore - confidently wrong, and
    # disagreeing with the gateway, which calls those same calls `unclassified`.
    # ⚠️ And nothing is RECORDED as an activity either: nothing was decided about those calls, so the
    # agent claims nothing. The gateway releases their records as `unclassified`, which is the one
    # place that fact belongs.
    check(c.activities == [], f"no activity is claimed for a discarded batch (got {c.activities})")


async def test_an_unknown_label_drops_only_its_own_call():
    c = StubCollector(replies=[["Build", "Sideways", "Design"]])
    for i in range(3):
        c.observe(f"msg_{i}", tools=["Write"])
    await c.finish()
    pushed = dict(c.pushes[0])
    check(pushed == {"msg_0": "Build", "msg_2": "Design"},
          f"the two good labels still apply (got {pushed})")
    check(sorted(c.activities) == ["Build", "Design"],
          f"and only the decided ones are recorded (got {sorted(c.activities)})")


async def test_a_batch_goes_when_it_is_full():
    c = StubCollector(replies=[["Build"] * activity.BATCH_CALLS, ["Design"]])
    for i in range(activity.BATCH_CALLS):
        c.observe(f"msg_{i}", tools=["Write"])
    check(c.due(), "a full batch is due")
    c.flush_soon()
    await asyncio.sleep(0)          # let the background task run
    c.observe("late", tools=["Bash"])
    await c.finish()
    check(len(c.pushes) >= 2, f"the full batch went mid-turn, the rest at the end ({len(c.pushes)})")
    check(sum(len(p) for p in c.pushes) == activity.BATCH_CALLS + 1, "every call was pushed once")


async def test_an_aging_call_goes_before_the_batch_fills():
    """⚠️ This is the trigger that makes a twelve-hour turn work. Without it a turn that never
    accumulates 25 calls would hold its labels until the end, long past the gateway's deadline."""
    c = StubCollector(replies=[["Build"]], age_seconds=0.0)
    c.observe("msg_1", tools=["Write"])
    check(c.due(), "a call older than the age deadline is due on its own")
    await c.finish()
    check(c.pushes == [[("msg_1", "Build")]], "and it goes")


async def test_a_sidecar_is_labelled_without_asking_a_model():
    """Paying a classifier call to label a classifier call would be an amusing way to spend money -
    and leaving them unlabelled would make the agent's own calls the biggest contributor to
    `unclassified`, which has to keep meaning "labelling is broken"."""
    c = StubCollector()
    c.observe_sidecar("msg_title")
    await c.finish()
    check(c.classify_calls == [], "no classification call was made")
    check(c.pushes == [[("msg_title", activity.SIDECAR_ACTIVITY)]], "it is labelled anyway")
    # ⚠️ Recorded as an activity, but carrying no usage of its own: the agent attributes usage per
    # MODEL per turn, and the sidecars are reported through that path like any other model.
    check(c.activities == [activity.SIDECAR_ACTIVITY], "and is recorded as an activity")


async def test_the_classifiers_own_spend_is_reported_like_any_other_sidecar():
    """The classifier is itself a metered call. Reported through the same sink as the title and
    satisfaction sidecars, or it would be the one call in the turn that nothing counts."""
    sink = []
    c = StubCollector(replies=[["Build"]], usage_sink=sink)
    # The stub replaces _classify, so drive the sink the way the real one does.
    c._usage_sink.append({"model": "haiku", "usage": {"input_tokens": 120}, "message_id": "msg_cls"})
    check(sink and sink[0]["message_id"] == "msg_cls", "its spend reaches the turn's sidecar sink")
    check(sink[0]["usage"]["input_tokens"] == 120, "with its tokens")
    del c


async def test_evidence_is_capped_at_ingest():
    """Capped where it is stored, not only where it is rendered: a single call can carry far more
    prose than the classifier will ever see, and holding all of it until then is pure cost."""
    c = StubCollector()
    c.observe("msg_1", texts=["x" * 10_000])
    stored = sum(len(t) for t in c._calls["msg_1"]["texts"])
    check(stored <= activity.MAX_EVIDENCE_CHARS,
          f"stored {stored} chars, capped at {activity.MAX_EVIDENCE_CHARS}")


async def test_a_call_with_no_message_id_is_ignored():
    c = StubCollector()
    c.observe(None, tools=["Write"])
    await c.finish()
    check(c.pushes == [] and c.activities == [], "nothing can name it, so nothing can label it")


async def test_nothing_happens_with_neither_a_gateway_nor_a_key():
    c = StubCollector(gateway_url=None, token=None)
    check(not c.enabled and not c.classifying, "with no key there is nothing to be done")
    c.observe("msg_1", tools=["Write"])
    await c.finish()
    check(c.pushes == [] and c.classify_calls == [], "and no work is done")
    check(c._calls == {}, "and nothing is buffered, so a long turn does not grow in memory")


async def test_a_self_hosted_box_is_not_charged_for_classification():
    """⚠️ A self-hosted box runs on the USER's own key and has no gateway, so classifying it would
    spend their money on something they did not ask for. It is off, and the turn reports as
    `unlabelled` - which is honest and free. An earlier version classified whenever a key was
    present: that fixed a real bug (the turn's whole cost filed under `unclassified`, telling the
    user their reporting was broken) by spending their money, and the WORD fixes it instead."""
    c = StubCollector(replies=[["Build"]], gateway_url=None)      # a key, but no gateway
    check(not c.classifying and not c.enabled, "nothing to push to, so nothing is classified")
    c.observe("msg_1", tools=["Write"])
    await c.finish()
    check(c.classify_calls == [] and c.pushes == [], "and no model call was made on their key")
    check(c.activities == [], "and nothing is claimed about what the work was")


async def test_a_self_hosted_box_can_opt_IN_to_classification():
    """For a user who does want the breakdown and is content to pay the classifier calls for it."""
    import os
    os.environ["DATAFYE_AGENT_CLASSIFY_ACTIVITY"] = "1"
    try:
        c = StubCollector(replies=[["Build"]], gateway_url=None)
        check(c.classifying and not c.enabled, "classifies on request, with nowhere to push")
        c.observe("msg_1", tools=["Write"])
        await c.finish()
        check(c.classify_calls == [["msg_1"]], "the call was classified")
        check(c.pushes == [], "and nothing was pushed, since there is no gateway")
        check(c.activities == ["Build"], "and the activity is recorded")
    finally:
        os.environ.pop("DATAFYE_AGENT_CLASSIFY_ACTIVITY", None)


async def test_a_transient_classify_failure_is_retried_not_forfeited():
    """⚠️ The buffer is drained before the classify call, so a 429 or a timeout used to forfeit that
    batch's labels for good - even though the gateway holds those records for about three times the
    cadence and would still accept them a minute later."""
    c = StubCollector(replies=[["Build"]])
    c.classify_fails = 1
    c.observe("msg_1", tools=["Write"])
    await c.finish()                 # first attempt fails, second succeeds inside finish()
    check(len(c.classify_calls) == 2, f"it asked again ({len(c.classify_calls)} calls)")
    check(c.pushes == [[("msg_1", "Build")]], "and the label was not lost")


async def test_a_wrong_answer_is_NOT_retried():
    """A count mismatch is a WRONG answer, not an absent one, so asking again invites the same
    wrongness. Discarded for good, and the calls land in `unclassified` - which is honest."""
    c = StubCollector(replies=[["Build", "Design"]])       # two labels for one call
    c.observe("msg_1", tools=["Write"])
    await c.finish()
    check(len(c.classify_calls) == 1, f"asked once only ({len(c.classify_calls)})")
    check(c.pushes == [], "nothing pushed")
    check(c.activities == [], "and no activity is claimed")


async def test_a_failed_push_is_retried_so_the_two_series_agree():
    """⚠️ A push that never landed means the gateway releases those records as `unclassified` even
    though they WERE classified, so the breakdown loses work that was correctly identified. The
    records are still held, so a retry seconds later lands."""
    c = StubCollector(replies=[["Build"]])
    c.push_fails = 1
    c.observe("msg_1", tools=["Write"])
    await c.finish()
    check(len(c.pushes) == 2, f"the push was retried ({len(c.pushes)})")
    check(c.pushes[-1] == [("msg_1", "Build")], "with the same label, not a re-classification")
    check(len(c.classify_calls) == 1, "and without paying to classify it twice")


def test_the_vocabulary_is_one_flat_set():
    """DAT-319: one flat set, no per-intent tracks. `Ship` is absent because `Deploy` covers going live
    for every artifact; not OFFERING a word is strictly stronger than filtering it afterwards."""
    check(activity.VOCABULARY == ("Explore", "Design", "Build", "Backtest", "Validate", "Deploy"),
          f"the vocabulary is the ticket's six words (got {activity.VOCABULARY})")
    check("Ship" not in activity.VOCABULARY, "Ship is absent from the vocabulary")
    check("Ship" not in activity._PROMPT, "and from the prompt, so the model cannot reach for it")
    check(activity.UNCLASSIFIED not in activity.VOCABULARY,
          "the gateway's reserved word is never something we send")
    check(activity.SIDECAR_ACTIVITY not in activity.VOCABULARY,
          "and the sidecars' own word is not offered to the classifier either")


def test_the_prompt_names_the_traps():
    """The ticket's traps, each stated in the prompt: a backtest is not Validate, advice about going
    live is not Deploy, a bare acknowledgement is not an activity of its own, and Explore is wide
    enough to absorb work that never becomes a project (or `unclassified` stops meaning "broken")."""
    p = activity._PROMPT
    check("Running a backtest is Backtest, not Validate" in p, "a backtest is not Validate")
    check("Advising on going live" in p and "is NOT Deploy" in p, "advice about going live is not Deploy")
    check("bare acknowledgement" in p, "an acknowledgement takes the activity of what it wraps up")
    check("question about Datafye" in p and "one-off" in p, "Explore absorbs one-off work and product questions")
    for artifact in ("real money", "publishing a", "dashboard or tool"):
        check(artifact in p, f"Deploy covers going live for every artifact: {artifact!r}")
    check(all(ord(c) < 128 for c in p), "and the prompt is plain ASCII")


def test_the_module_is_not_shadowed_in_main():
    """⚠️ main.py defines a route handler called `activity` (POST /v1/activity), so `import activity`
    there binds a name the function definition later OVERWRITES. The import succeeds, and the failure
    surfaces only when a turn reaches the first use - `'function' object has no attribute
    'Collector'` - which cost a green unit run and a red end-to-end one. Hence the alias, and hence
    this check that it is still an alias."""
    import types
    try:
        import main
    except Exception as e:      # noqa: BLE001 - a missing SDK should not fail the whole file
        check(False, f"could not import main to check the alias: {e}")
        return
    check(isinstance(getattr(main, "activity_labels", None), types.ModuleType),
          "main.activity_labels is the module, not something that shadowed it")
    check(hasattr(main.activity_labels, "Collector"), "and it still carries Collector")


async def test_the_age_deadline_cannot_decay_without_bound():
    """⚠️ Tightening from the CURRENT age on every push is a feedback loop: with a gateway multiplier
    of 2 the deadline shrinks by a third each time (60 -> 40 -> 26.7 -> ...) until there is one
    classification call per model call. It is recomputed from the configured value instead."""
    c = StubCollector(age_seconds=60.0)
    seen = []
    for _ in range(6):
        # The gateway sizes its hold from the cadence we REPORT, which is the configured one.
        hold = 2.0 * c._configured_age              # a gateway whose multiplier is 2, not 3
        c._age = max(activity.MIN_AGE_SECONDS,
                     min(c._configured_age, hold / activity.HOLD_SAFETY_DIVISOR))
        seen.append(c._age)
    check(len(set(seen)) == 1 and seen[0] == 40.0,
          f"it settles on one value instead of decaying (saw {seen})")
    check(c._age >= activity.MIN_AGE_SECONDS, "and never goes below the floor")
    # And the report itself never moves, which is what closes the loop.
    check(c._configured_age == 60.0, "the reported interval is the configured one, not the tightened")


async def test_a_long_thinking_block_does_not_starve_what_the_call_said():
    """⚠️ The Backtest-vs-Validate traps turn on exactly what the call SAID ("Sharpe 1.4 out of
    sample"), and thinking is rendered first, so one shared budget over the joined string
    dropped the text entirely on a call that thought at length."""
    c = StubCollector()
    c.observe("msg_1", thinking=["T" * 5000], texts=["Sharpe 1.4 out of sample."],
              tools=["Bash"])
    ev = activity._evidence(c._calls["msg_1"])
    check("said: Sharpe 1.4" in ev, "what the call said survives a long thinking block")
    check("tools: Bash" in ev, "and so do the tool names")
    check(len(ev) < 1200, f"while the whole stays bounded ({len(ev)} chars)")


async def main_async():
    for fn in (test_a_shifted_mapping_is_never_applied,
               test_a_transient_classify_failure_is_retried_not_forfeited,
               test_a_wrong_answer_is_NOT_retried,
               test_a_failed_push_is_retried_so_the_two_series_agree,
               test_an_unknown_label_drops_only_its_own_call,
               test_a_batch_goes_when_it_is_full,
               test_an_aging_call_goes_before_the_batch_fills,
               test_a_sidecar_is_labelled_without_asking_a_model,
               test_the_classifiers_own_spend_is_reported_like_any_other_sidecar,
               test_evidence_is_capped_at_ingest,
               test_the_age_deadline_cannot_decay_without_bound,
               test_a_long_thinking_block_does_not_starve_what_the_call_said,
               test_a_call_with_no_message_id_is_ignored,
               test_nothing_happens_with_neither_a_gateway_nor_a_key,
               test_a_self_hosted_box_is_not_charged_for_classification,
               test_a_self_hosted_box_can_opt_IN_to_classification):
        print(f"--- {fn.__name__} ---")
        await fn()


if __name__ == "__main__":
    asyncio.run(main_async())
    print("--- test_the_vocabulary_is_one_flat_set ---")
    test_the_vocabulary_is_one_flat_set()
    print("--- test_the_prompt_names_the_traps ---")
    test_the_prompt_names_the_traps()
    print("--- test_the_module_is_not_shadowed_in_main ---")
    test_the_module_is_not_shadowed_in_main()
    print(f"\n{'FAILED: ' + '; '.join(FAILED) if FAILED else 'all checks pass'}")
    sys.exit(1 if FAILED else 0)
