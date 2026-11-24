#!/usr/bin/env python3
"""A resumed turn reports only its own usage (DAT-306 / SUT-95).

Each turn resumes the project's SDK session, and on a resumed session the CLI's `model_usage` and
`total_cost_usd` are CUMULATIVE: turn N reports turns 1..N. The figures below are the real ones from
claude-agent-sdk 0.2.188 / CLI 2.1.331 (2026-11-24), two Haiku turns, the second resuming the first.

Offline: the SDK is faked, and the accounts report is captured instead of sent.

    python tests/turn_usage.py
"""
import asyncio
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
from claude_agent_sdk import ResultMessage, SystemMessage  # noqa: E402

FAILED = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        FAILED.append(what)


H = "claude-haiku-4-5"
O = "claude-opus-5"


def entry(i, o, cr, cw, cost, **extra):
    e = {"inputTokens": i, "outputTokens": o, "cacheReadInputTokens": cr,
         "cacheCreationInputTokens": cw, "webSearchRequests": 0, "costUSD": cost,
         "contextWindow": 200000, "maxOutputTokens": 64000}
    e.update(extra)
    return e


# Measured: turn 1, then turn 2 as the CLI reports it (cumulative), on one session.
TURN1 = {H: entry(10, 31, 0, 16509, 0.02080125)}
TURN2_CUMULATIVE = {H: entry(20, 62, 16509, 16937, 0.02315215)}


def test_the_pure_subtraction():
    own = conversations.usage_since(TURN1, TURN2_CUMULATIVE)[H]
    check((own["inputTokens"], own["outputTokens"], own["cacheReadInputTokens"],
           own["cacheCreationInputTokens"]) == (10, 31, 16509, 428),
          f"turn 2's own tokens are turn 2's, not turns 1+2 ({own})")
    check(abs(own["costUSD"] - 0.0023509) < 1e-6, f"and so is its cost ({own['costUSD']:.7f})")
    check(own["contextWindow"] == 200000 and own["maxOutputTokens"] == 64000,
          "fields that describe the model, not the turn, pass through unchanged")
    check(conversations.usage_since(None, TURN1) == TURN1, "no baseline: the whole figure is this turn's")
    # A model that did not run this turn is not reported as a row of zeros; one that ran for the first
    # time in this session is reported in full.
    both = {H: TURN1[H], O: entry(5, 7, 0, 0, 0.0002)}
    own = conversations.usage_since(TURN1, both)
    check(set(own) == {O} and own[O]["inputTokens"] == 5,
          f"only the model that ran this turn is reported ({sorted(own)})")
    # A running total that went DOWN means the session started over: subtracting would go negative.
    restarted = {H: entry(3, 4, 0, 100, 0.0001)}
    check(conversations.usage_since(TURN2_CUMULATIVE, restarted) == restarted,
          "a figure that went down is a new session: reported in full, never negative")


def test_the_baseline_is_per_session_and_persisted():
    cid = "proj-baseline"
    conversations.ensure(cid)
    u1, c1 = conversations.take_turn_usage(cid, "sess-1", TURN1, 0.02080125)
    check(u1 == TURN1 and abs(c1 - 0.02080125) < 1e-9, "the session's first turn reports itself")
    # Read back from disk, as a restarted agent would.
    saved = conversations.get(cid)["sdk_usage_baseline"]
    check(saved["session_id"] == "sess-1" and saved["model_usage"] == TURN1,
          "the running total is kept in the project's meta.json, so it survives a restart")
    u2, c2 = conversations.take_turn_usage(cid, "sess-1", TURN2_CUMULATIVE, 0.02315215)
    check(u2[H]["inputTokens"] == 10 and abs(c2 - 0.0023509) < 1e-6,
          f"the resumed turn reports only itself, total cost included ({u2[H]['inputTokens']}, {c2:.7f})")
    u3, _ = conversations.take_turn_usage(cid, "sess-1", TURN2_CUMULATIVE, 0.02315215)
    check(u3 == {}, "asking again with the same total reports nothing: a turn is never counted twice")
    u4, c4 = conversations.take_turn_usage(cid, "sess-2", TURN1, 0.02080125)
    check(u4 == TURN1 and abs(c4 - 0.02080125) < 1e-9,
          "a different session is a new running total, not a subtraction from the old one")


async def test_two_resumed_turns_through_the_agent():
    """The acceptance test: two turns of one project through stream_agent_response, the SDK faked to
    report exactly what the real CLI does. The second report must exclude the first."""
    cid = "proj-two-turns"
    conversations.ensure(cid)
    results = [TURN1, TURN2_CUMULATIVE]
    costs = [0.02080125, 0.02315215]
    reported = []

    async def fake_query(prompt, options):
        n = len(reported_turns)
        reported_turns.append(n)
        yield SystemMessage(subtype="init", data={"session_id": "sess-real"})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="sess-real", total_cost_usd=costs[n],
                            usage={"input_tokens": 10, "output_tokens": 31}, result="done",
                            model_usage=results[n])

    async def capture(conversation_id, model, delta, idem, auth_token):
        reported.append((len(reported_turns), model, dict(delta)))

    async def nothing(*a, **k):
        return None

    main._fetch_deployment_state = nothing

    reported_turns = []
    main.query = fake_query
    main._report_usage_to_accounts = capture
    main._report_satisfaction_to_accounts = nothing
    main.AGENT_USERNAME = None            # no side-calls: they are not what is under test
    main.credentials = {}

    for _ in range(2):
        async for _ in main.stream_agent_response("hello", cid, None, "jwt"):
            pass

    by_turn = {t: [d for (tt, m, d) in reported if tt == t and m == H] for t in (1, 2)}
    t1 = by_turn[1][0] if by_turn[1] else {}
    t2 = by_turn[2][0] if by_turn[2] else {}
    check(t1.get("tokens_in") == 10 and t1.get("cache_create") == 16509,
          f"turn 1 reports turn 1 ({t1.get('tokens_in')}, {t1.get('cache_create')})")
    check(t2.get("tokens_in") == 10 and t2.get("tokens_out") == 31 and t2.get("cache_create") == 428,
          f"turn 2 reports ONLY turn 2, not turns 1+2 ({t2.get('tokens_in')}, {t2.get('tokens_out')}, "
          f"{t2.get('cache_create')})")
    check(abs((t2.get("cost_micros") or 0) - 2351) <= 1,
          f"and its cost is turn 2's own (${(t2.get('cost_micros') or 0) / 1e6:.6f}; expected about "
          f"$0.002351, the cumulative figure is $0.023152)")
    total = conversations.usage_public(conversations.get(cid)).get("totals", {})
    check(total.get("tokens_in") == 20, f"the project's total is the sum of its turns ({total.get('tokens_in')})")


if __name__ == "__main__":
    test_the_pure_subtraction()
    test_the_baseline_is_per_session_and_persisted()
    asyncio.run(test_two_resumed_turns_through_the_agent())
    print(f"\n{len(FAILED)} failure(s)" if FAILED else "\nall checks pass")
    sys.exit(1 if FAILED else 0)
