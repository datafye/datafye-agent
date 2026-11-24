#!/usr/bin/env python3
"""A turn reports only its own usage (DAT-306 / SUT-95).

Each turn resumes the project's SDK session, and on a resumed session the CLI's `model_usage` and
`total_cost_usd` are CUMULATIVE: turn N reports turns 1..N. The figures below are the real ones from
claude-agent-sdk 0.2.188 / CLI 2.1.331 (2026-11-24), two Haiku turns, the second resuming the first.

Every case here is driven through the real turn code (`stream_agent_response`) with the SDK faked
and the accounts report captured. The fake flat `usage` is deliberately NOT any turn's real figure,
so a turn that wrongly falls back to it is caught.

    python tests/turn_usage.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_STATE = tempfile.mkdtemp(prefix="turn-usage-")
os.environ["DATAFYE_AGENT_STATE_DIR"] = _STATE
os.environ["DATAFYE_AGENT_WORKSPACE"] = os.path.join(_STATE, "workspace")
os.environ.pop("ANTHROPIC_BASE_URL", None)
os.environ["ANTHROPIC_API_KEY"] = "sk-test"

import conversations  # noqa: E402
import main  # noqa: E402
import project_archive  # noqa: E402
from claude_agent_sdk import ResultMessage, SystemMessage  # noqa: E402

FAILED = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        FAILED.append(what)


H = "claude-haiku-4-5"
O = "claude-opus-5"
FLAT = {"input_tokens": 999, "output_tokens": 999}     # nobody's real figure


def entry(i, o, cr, cw, cost, **extra):
    e = {"inputTokens": i, "outputTokens": o, "cacheReadInputTokens": cr,
         "cacheCreationInputTokens": cw, "webSearchRequests": 0, "costUSD": cost,
         "contextWindow": 200000, "maxOutputTokens": 64000}
    e.update(extra)
    return e


# Measured on the real CLI: turn 1, then turn 2 as reported (cumulative), one session.
T1 = {H: entry(10, 31, 0, 16509, 0.02080125)}
T2 = {H: entry(20, 62, 16509, 16937, 0.02315215)}


# ── the fake SDK and the captured accounts report ─────────────────────────────────────────────────
SCRIPT = {}        # prompt -> list of {"session", "mu", "cost", "text", "delay"}, one per turn
REPORTED = []      # (conversation_id, model, delta, idem)


async def fake_query(prompt, options):
    step = SCRIPT[prompt].pop(0)
    yield SystemMessage(subtype="init", data={"session_id": step["session"]})
    await asyncio.sleep(step.get("delay", 0))
    yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                        num_turns=1, session_id=step["session"], total_cost_usd=step.get("cost"),
                        usage=FLAT, result=step.get("text", "done"), model_usage=step.get("mu"))


async def capture(conversation_id, model, delta, idem, auth_token):
    REPORTED.append((conversation_id, model, dict(delta), idem))


async def nothing(*a, **k):
    return None


main.query = fake_query
main._report_usage_to_accounts = capture
main._report_satisfaction_to_accounts = nothing
main._fetch_deployment_state = nothing
main.AGENT_USERNAME = None            # no side-calls: they are not what is under test
main.credentials = {}


async def run(cid, prompt):
    async for _ in main.stream_agent_response(prompt, cid, None, "jwt"):
        pass


def reports(cid):
    return [r for r in REPORTED if r[0] == cid]


def tokens_in(cid, model=H):
    return [r[2].get("tokens_in") for r in reports(cid) if r[1] == model]


# ── the pure parts ────────────────────────────────────────────────────────────────────────────────
def test_the_subtraction():
    own = conversations.usage_since(T1, T2)[H]
    check((own["inputTokens"], own["outputTokens"], own["cacheReadInputTokens"],
           own["cacheCreationInputTokens"]) == (10, 31, 16509, 428), f"turn 2 minus turn 1 ({own})")
    check(own["contextWindow"] == 200000, "fields that describe the model pass through")
    check(conversations.usage_since(T2, T1) is None, "a total that went down is reported as untrustworthy")
    check(set(conversations.usage_since(T1, {H: T1[H], O: entry(5, 7, 0, 0, 0.0002)})) == {O},
          "only a model that ran in between is reported")
    snake = {H: {"input_tokens": 20, "output_tokens": 62, "cache_read_input_tokens": 16509,
                 "cache_creation_input_tokens": 16937, "cost_usd": 0.02315215}}
    own = conversations.usage_since({H: {"input_tokens": 10, "output_tokens": 31,
                                         "cache_creation_input_tokens": 16509, "cost_usd": 0.02080125}},
                                    snake)[H]
    check(own["inputTokens"] == 10 and own["cacheCreationInputTokens"] == 428,
          f"snake_case spellings are subtracted too ({own})")


# ── through the agent ─────────────────────────────────────────────────────────────────────────────
async def test_two_resumed_turns():
    cid = "proj-two"
    conversations.ensure(cid)
    SCRIPT["two"] = [{"session": "s", "mu": T1, "cost": 0.02080125},
                     {"session": "s", "mu": T2, "cost": 0.02315215}]
    await run(cid, "two")
    await run(cid, "two")
    check(tokens_in(cid) == [10, 10], f"each turn reports only itself ({tokens_in(cid)})")
    t2 = [r for r in reports(cid) if r[1] == H][1][2]
    check(t2["cache_create"] == 428 and abs(t2["cost_micros"] - 2351) <= 1,
          f"turn 2's cache writes and cost are its own ({t2['cache_create']}, {t2['cost_micros']})")
    check(conversations.usage_public(conversations.get(cid))["totals"]["tokens_in"] == 20,
          "the project's total is the sum of its turns")


async def test_a_turn_with_no_reply_is_still_counted():
    """Review finding 1: the key used to be a count of saved replies, which a turn that ends without
    one does not move, so it shared the previous turn's key and was dropped as a duplicate."""
    cid = "proj-noreply"
    conversations.ensure(cid)
    SCRIPT["noreply"] = [{"session": "s", "mu": T1, "cost": 0.02080125},
                         {"session": "s", "mu": T2, "cost": 0.02315215, "text": ""},
                         {"session": "s", "mu": {H: entry(30, 93, 33018, 17365, 0.0255)}, "cost": 0.0255}]
    for _ in range(3):
        await run(cid, "noreply")
    keys = [r[3] for r in reports(cid)]
    check(len(set(keys)) == 3, f"every turn has its own key ({keys})")
    check(conversations.usage_public(conversations.get(cid))["totals"]["tokens_in"] == 30,
          "and the turn with no reply is in the project's total")


async def test_a_turn_without_model_usage_is_counted_once():
    """Review finding 2: the degraded path (no model_usage) on a RESUMED turn reports nothing, and the
    next turn's difference carries it. It used to be counted there and again by the next turn."""
    cid = "proj-fallback"
    conversations.ensure(cid)
    SCRIPT["fallback"] = [{"session": "s", "mu": T1, "cost": 0.02080125},
                          {"session": "s", "mu": None, "cost": 0.02315215},
                          {"session": "s", "mu": {H: entry(30, 93, 33018, 17365, 0.0255)}, "cost": 0.0255}]
    for _ in range(3):
        await run(cid, "fallback")
    every = [r[2].get("tokens_in") for r in reports(cid) if r[2].get("tokens_in")]   # every model
    check(every == [10, 20], f"the resumed fallback turn is reported once, by the next turn ({every})")
    cid = "proj-fallback-first"
    conversations.ensure(cid)
    SCRIPT["fallback-first"] = [{"session": "s1", "mu": None, "cost": 0.004}]
    await run(cid, "fallback-first")
    flat = [r[2].get("tokens_in") for r in reports(cid) if r[2].get("tokens_in")]   # configured model
    check(flat == [999], f"a session's FIRST turn with no model_usage still reports its flat usage ({flat})")


async def test_an_upgrade_does_not_rebill_a_live_session():
    """Review finding 3: a project resuming a session with no running total on record (the first turn
    after the agent is upgraded, or after an import) cannot tell its own usage from the earlier
    turns', so it reports nothing and starts a running total. It used to report the whole session."""
    cid = "proj-upgrade"
    conversations.ensure(cid)
    conversations.set_sdk_session(cid, "s-old")                # a live session, no baseline
    big = {H: entry(5000, 9000, 900000, 80000, 1.5)}
    SCRIPT["upgrade"] = [{"session": "s-old", "mu": big, "cost": 1.5},
                         {"session": "s-old", "mu": {H: entry(5010, 9031, 916509, 80428, 1.5024)}, "cost": 1.5024}]
    await run(cid, "upgrade")
    check(tokens_in(cid) == [], "the first turn after the upgrade does not re-bill the whole session")
    await run(cid, "upgrade")
    check(tokens_in(cid) == [10], f"and the next turn reports only itself ({tokens_in(cid)})")


async def test_a_new_session_on_resume_is_counted_in_full():
    cid = "proj-newsession"
    conversations.ensure(cid)
    conversations.set_sdk_session(cid, "s-gone")
    SCRIPT["newsession"] = [{"session": "s-new", "mu": T1, "cost": 0.02080125}]
    await run(cid, "newsession")
    check(tokens_in(cid) == [10], "a turn that ends in a different session from the one it resumed is all its own")


async def test_a_total_that_went_down_is_not_rebilled():
    """Review finding 5: a total that fell within the SAME session cannot be trusted. It used to be
    taken as a fresh session and reported in full, re-billing everything."""
    cid = "proj-down"
    conversations.ensure(cid)
    SCRIPT["down"] = [{"session": "s", "mu": T2, "cost": 0.02315215},
                      {"session": "s", "mu": T1, "cost": 0.02080125},
                      {"session": "s", "mu": {H: entry(15, 50, 100, 16600, 0.021)}, "cost": 0.021}]
    for _ in range(3):
        await run(cid, "down")
    check(tokens_in(cid) == [20, 5], f"the turn whose total fell reports nothing; the next diffs from it ({tokens_in(cid)})")


async def test_two_turns_at_once_each_count_their_own():
    """Review finding 7: two turns on one project both resume from the same running total, so each
    is measured against that snapshot, not against what the other saved meanwhile."""
    cid = "proj-concurrent"
    conversations.ensure(cid)
    SCRIPT["c0"] = [{"session": "s", "mu": T1, "cost": 0.02080125}]
    await run(cid, "c0")
    SCRIPT["ca"] = [{"session": "s", "mu": {H: entry(13, 40, 100, 16600, 0.0211)}, "cost": 0.0211, "delay": 0.05}]
    SCRIPT["cb"] = [{"session": "s", "mu": {H: entry(17, 50, 200, 16700, 0.0212)}, "cost": 0.0212, "delay": 0.1}]
    await asyncio.gather(run(cid, "ca"), run(cid, "cb"))
    check(sorted(tokens_in(cid)[1:]) == [3, 7], f"turn A reports 3 and turn B reports 7 ({tokens_in(cid)})")


async def test_the_deltas_and_the_running_total_are_one_write():
    """Review finding 6: a turn cancelled between recording its usage and moving the running total
    would lose that usage for good, so the two are written together."""
    cid = "proj-atomic"
    conversations.ensure(cid)
    writes = []
    real = conversations._write

    def counting(record):
        writes.append(json.loads(json.dumps(record)))
        real(record)

    conversations._write = counting
    try:
        conversations.commit_turn_usage(cid, "s", T1, {H: {"tokens_in": 10, "tokens_out": 31}}, "s:abc")
    finally:
        conversations._write = real
    check(len(writes) == 1, f"one write ({len(writes)})")
    saved = writes[-1] if writes else {}
    check(saved.get("usage", {}).get("totals", {}).get("tokens_in") == 10
          and saved.get("sdk_usage_baseline", {}).get("model_usage") == T1,
          "and it carries both the usage and the new running total")


def test_an_import_drops_the_running_total():
    """Review finding 4: an archive's running total belongs to the box and moment it came from."""
    d = tempfile.mkdtemp()
    meta = os.path.join(d, "meta.json")
    with open(meta, "w") as f:
        json.dump({"id": "proj-old", "sdk_session_id": "s",
                   "sdk_usage_baseline": {"session_id": "s", "model_usage": T1}}, f)
    from pathlib import Path
    project_archive._retarget_record(Path(meta), "proj-new")
    rec = json.load(open(meta))
    check("sdk_usage_baseline" not in rec and rec["id"] == "proj-new",
          "an imported project starts without a running total")


async def test_the_running_total_is_saved_before_the_side_calls():
    """Round-3 review: it was saved only after the title, satisfaction and labelling calls, so a turn
    that began in that gap snapshotted a total without this turn and billed it again."""
    cid = "proj-timing"
    conversations.ensure(cid)
    seen = []

    async def title(message, sink=None, **kw):
        seen.append((conversations.get(cid) or {}).get("sdk_usage_baseline"))
        return None

    real, main.generate_title = main.generate_title, title
    try:
        SCRIPT["timing"] = [{"session": "s", "mu": T1, "cost": 0.02080125}]
        await run(cid, "timing")
    finally:
        main.generate_title = real
    check(bool(seen) and seen[0] and seen[0].get("model_usage") == T1,
          "the running total is already saved when the side-calls start")


def test_the_running_total_never_moves_backwards():
    """Round-3 review: two turns at once finish in either order, and the later save used to win."""
    cid = "proj-backwards"
    conversations.ensure(cid)
    conversations.set_sdk_session(cid, "s")
    conversations.commit_turn_usage(cid, "s", T2, {}, "k1")
    conversations.commit_turn_usage(cid, "s", T1, {}, "k2")
    check(conversations.usage_baseline(cid)["model_usage"] == T2, "a smaller total does not replace a larger one")
    conversations.commit_turn_usage(cid, "s-other", T1, {}, "k3")
    check(conversations.usage_baseline(cid)["session_id"] == "s",
          "nor does a total for a session the project does not hold")


async def test_a_resumed_turn_with_no_session_id_is_not_rebilled():
    cid = "proj-nosession"
    conversations.ensure(cid)
    SCRIPT["nosession"] = [{"session": "s", "mu": T1, "cost": 0.02080125},
                           {"session": "", "mu": T2, "cost": 0.02315215}]
    await run(cid, "nosession")
    await run(cid, "nosession")
    check(tokens_in(cid) == [10], f"a resumed result naming no session reports nothing ({tokens_in(cid)})")


async def test_a_cli_that_never_sends_model_usage_still_reports_flat_usage():
    """Round-3 review: every resumed turn was DEFERRED to a difference that never comes."""
    cid = "proj-neverusage"
    conversations.ensure(cid)
    SCRIPT["neverusage"] = [{"session": "s", "mu": None, "cost": 0.004}, {"session": "s", "mu": None, "cost": 0.008}]
    await run(cid, "neverusage")
    await run(cid, "neverusage")
    flat = [r[2] for r in reports(cid) if r[2].get("tokens_in")]
    check([d["tokens_in"] for d in flat] == [999, 999], f"each turn reports its flat usage ({[d['tokens_in'] for d in flat]})")
    check(flat[1].get("cost_micros") == 0, "and the cumulative cost is withheld on the resumed one")


async def test_turn_counts_are_kept_when_no_tokens_are_reported():
    """Round-3 review: tool_calls and turns are per invocation, so a turn whose token usage is not
    reported (here: the first turn after an upgrade) still counts as a turn."""
    cid = "proj-counts"
    conversations.ensure(cid)
    conversations.set_sdk_session(cid, "s-live")
    SCRIPT["counts"] = [{"session": "s-live", "mu": T2, "cost": 0.02315215}]
    await run(cid, "counts")
    totals = conversations.usage_public(conversations.get(cid))["totals"]
    check(totals.get("turns") == 1 and totals.get("tokens_in") == 0,
          f"the turn is counted, its tokens are not ({totals.get('turns')}, {totals.get('tokens_in')})")


def test_the_agent_parses_on_the_boxes_python():
    """Round-3 review: a nested-quote f-string parsed on 3.12+ and was a SyntaxError on 3.11, the
    Python the installer builds the agent's venv with, so the agent would not have started."""
    import shutil
    import subprocess
    py = shutil.which("python3.11")
    if not py:
        print("SKIP python3.11 is not installed here; the agent must still parse on it")
        return
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    bad = []
    for name in sorted(os.listdir(here)):
        if name.endswith(".py"):
            r = subprocess.run([py, "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read())",
                                os.path.join(here, name)], capture_output=True, text=True)
            if r.returncode:
                bad.append(name)
    check(not bad, f"every module parses on Python 3.11 ({bad or 'all'})")


if __name__ == "__main__":
    test_the_subtraction()
    test_an_import_drops_the_running_total()
    test_the_running_total_never_moves_backwards()
    test_the_agent_parses_on_the_boxes_python()
    for t in (test_two_resumed_turns, test_a_turn_with_no_reply_is_still_counted,
              test_a_turn_without_model_usage_is_counted_once, test_an_upgrade_does_not_rebill_a_live_session,
              test_a_new_session_on_resume_is_counted_in_full, test_a_total_that_went_down_is_not_rebilled,
              test_two_turns_at_once_each_count_their_own, test_the_deltas_and_the_running_total_are_one_write,
              test_the_running_total_is_saved_before_the_side_calls, test_a_resumed_turn_with_no_session_id_is_not_rebilled,
              test_a_cli_that_never_sends_model_usage_still_reports_flat_usage,
              test_turn_counts_are_kept_when_no_tokens_are_reported):
        print(f"--- {t.__name__} ---")
        asyncio.run(t())
    print(f"\n{len(FAILED)} failure(s)" if FAILED else "\nall checks pass")
    sys.exit(1 if FAILED else 0)
