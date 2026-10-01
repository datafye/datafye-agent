#!/usr/bin/env python3
"""Does the classifier actually label a call correctly? (DAT-319, ported from SUT-114)

The question changed with the design. The old classifier inferred an intent, a per-intent track and
a stage within it: where the PROJECT was. This asks what ONE CALL did, which is a much easier
question - so the interesting cases are the ones where a plain reading still misleads: a backtest is
not Validate, advice about going live is not Deploy, an acknowledgement is not an activity of its
own, and work that never becomes a project still has to land somewhere other than `unclassified`.

Evidence is the shape the collector really builds (`activity._evidence`): tool names first, then
thinking, then what the model said. Tool names lead because they settle most cases outright.

    DATAFYE_AGENT_ANTHROPIC_API_KEY=sk-ant-... python tests/activity_classifier.py [runs]

`runs` (default 3) repeats every case: a classifier that is right once and wrong twice is not fixed.
Costs a few cents. Exits non-zero if any case fails.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

if not os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ.get("DATAFYE_AGENT_ANTHROPIC_API_KEY", "")

import activity  # noqa: E402

# (name, tools, thinking, said, expected)
#
# The traps the ticket names are the point: `backtest_not_validate`, `going_live_advice`, and the two
# Explore cases that keep `unclassified` meaning "labelling broke".
CASES = [
    ("explore", [], "They have not said what market or horizon yet.",
     "Before I build anything: which symbols, and are you after intraday or swing trades?", "Explore"),
    ("explore_one_off_research", ["Bash"], "A one-off question about the data, not a project.",
     "Over the last year SPY gapped up more than 1% on 23 days; here is the table.", "Explore"),
    ("explore_about_datafye", [], "This is a question about the platform itself.",
     "Datafye replays history as source-time clock ticks, so a backtest sees bars as they closed.",
     "Explore"),
    ("design", ["Read"], "Working out the rules before writing anything.",
     "I propose a 20/50 EMA crossover on 5-minute bars with an ATR stop; here are the trade-offs.",
     "Design"),
    ("build", ["Write", "Edit"], "Writing the strategy module.",
     "Added strategy.py with the crossover signal and the position sizing.", "Build"),
    ("build_environment_for_a_backtest", ["Bash"],
     "The backtest needs a year of minute bars first.",
     "Added the SIP dataset and started the history fetch for AAPL and MSFT.", "Build"),
    ("backtest", ["Bash"], "Running it over 2025.",
     "Backtest finished: 14.2% return, max drawdown 6.1%, 212 trades.", "Backtest"),
    # ⚠️ The first trap: a careful backtest is still a backtest.
    ("backtest_not_validate", ["Bash"], "Re-running with a tighter stop to see if it holds up.",
     "With the 1.5 ATR stop the drawdown falls to 4.8% and the return to 12.9%.", "Backtest"),
    ("validate", ["Bash"], "Checking the result on data it was not tuned on.",
     "Out of sample (2024) the Sharpe is 0.4 against 1.6 in sample, so it is overfit.", "Validate"),
    # ⚠️ The second trap: advice about going live is not going live.
    ("going_live_advice", [], "They asked whether the strategy is ready to go live.",
     "Not yet: it has two weeks of paper trading; I would want a month of results that match the "
     "backtest before risking money.", "Validate"),
    ("deploy", ["Bash"], "They confirmed; switching to live trading.",
     "It is now trading live with real money on your linked account, 1 share per signal.", "Deploy"),
    ("deploy_dashboard", ["Bash", "Write"], "Standing the dashboard up for them to use.",
     "Your dashboard is running at https://u1.app.datafye.io:10010/ and updates every minute.",
     "Deploy"),
]


async def run(runs):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SKIP: no ANTHROPIC_API_KEY / DATAFYE_AGENT_ANTHROPIC_API_KEY set")
        return 0
    c = activity.Collector(gateway_url=None, token=os.environ["ANTHROPIC_API_KEY"],
                           model=os.environ.get("DATAFYE_AGENT_TITLE_MODEL", "claude-haiku-4-5"),
                           base_url=os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com")
    failed = 0
    # One call per run, all cases in ONE batch - which is also how it runs in production, so this
    # exercises the positional mapping rather than a dozen independent single-call classifications.
    # `_classify` returns (labels, retry); `retry` is True only when the answer was ABSENT (a
    # transport error) rather than wrong, which for this test would mean the run did not happen.
    replies = await asyncio.gather(*[
        c._classify([(name, {"tools": tools, "thinking": [thinking], "texts": [said]})
                     for name, tools, thinking, said, _ in CASES])
        for _ in range(runs)])
    batches = [labels for labels, retry in replies if not retry]
    if not batches:
        print("  FAIL  every run failed to reach the model; nothing was classified")
        return len(CASES)
    for i, (name, _t, _th, _s, expected) in enumerate(CASES):
        got = [b[i] for b in batches]
        ok = all(g == expected for g in got)
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name:26} expect={expected:7} got={got}")
    print(f"  => {len(CASES) - failed}/{len(CASES)} cases correct over {runs} run(s)")
    return failed


if __name__ == "__main__":
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    print(f"--- activity classifier, {runs} run(s), all {len(CASES)} cases in one batch ---")
    sys.exit(1 if asyncio.run(run(runs)) else 0)
