#!/usr/bin/env python3
"""Usage is kept per MODEL per turn, and old records keep their numbers (DAT-319). Offline.

The agent used to key usage by `stage|model`, where the stage was the project's asserted lifecycle
position. That position is gone, and the agent does not apportion a turn across activities: the
gateway meters every call and knows its activity, so the measured breakdown is the gateway's.

What must never be wrong here is the NUMBERS. A project recorded before this change carries several
`stage|model` cells per model, and those totals are what a bill was built from, so they are folded
into one cell per model and summed, never dropped.

    python tests/usage_by_model.py

Exits non-zero on any failure.
"""
import json
import os
import shutil
import sys
import tempfile

STATE = tempfile.mkdtemp(prefix="datafye-usage-test-")
os.environ["DATAFYE_AGENT_STATE_DIR"] = STATE
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import conversations  # noqa: E402

FAILED = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILED.append(what)


def test_a_new_project_keys_usage_per_model():
    conversations.ensure("proj-a")
    conversations.add_usage("proj-a", "opus", {"tokens_in": 10, "cost_micros": 100}, "k1")
    conversations.add_usage("proj-a", "opus", {"tokens_in": 5, "cost_micros": 50}, "k2")
    view = conversations.add_usage("proj-a", "haiku", {"tokens_in": 2, "cost_micros": 1}, "k3")
    check(set(view["by_model"]) == {"opus", "haiku"}, f"one cell per model (got {set(view['by_model'])})")
    check(view["by_model"]["opus"]["tokens_in"] == 15, "a model's turns accumulate in its cell")
    check(view["totals"]["cost_micros"] == 151, "and the totals are the sum of every cell")
    check("by_stage_model" not in view, "the stage-keyed map is gone from the public view")
    again = conversations.add_usage("proj-a", "opus", {"tokens_in": 999}, "k1")
    check(again["by_model"]["opus"]["tokens_in"] == 15, "a replayed idempotency key is not counted twice")


def test_a_new_project_carries_no_lifecycle():
    rec = conversations.ensure("proj-b")
    for key in ("intent", "track", "stage", "maxStage"):
        check(key not in rec, f"a new record has no {key!r}")


def test_an_old_record_keeps_its_numbers():
    """⚠️ Several `stage|model` cells collapse into one per model. Summed, not dropped."""
    conversations.ensure("proj-old")
    path = conversations.project_dir("proj-old") / "meta.json"
    rec = json.loads(path.read_text())
    rec["intent"], rec["track"], rec["stage"], rec["maxStage"] = "algo", ["Explore"], "Explore", "Explore"
    rec["usage"] = {
        "totals": {"tokens_in": 70, "cost_micros": 700, "tokens_out": 0, "cache_read": 0,
                   "cache_create": 0, "tool_calls": 3, "turns": 2},
        "by_stage_model": {
            "Explore|opus": {"stage": "Explore", "model": "opus", "tokens_in": 30, "cost_micros": 300,
                             "tool_calls": 1, "updated_at": 5},
            "Build|opus": {"stage": "Build", "model": "opus", "tokens_in": 30, "cost_micros": 300,
                           "tool_calls": 2, "updated_at": 9},
            "research|haiku": {"stage": "research", "model": "haiku", "tokens_in": 10, "cost_micros": 100},
        },
        "updated_at": 9,
        "applied_keys": ["old-1"],
    }
    path.write_text(json.dumps(rec))

    view = conversations.usage_public(conversations.get("proj-old"))
    cells = view["by_model"]
    check(set(cells) == {"opus", "haiku"}, f"old cells fold into one per model (got {set(cells)})")
    check(cells["opus"]["tokens_in"] == 60 and cells["opus"]["cost_micros"] == 600,
          "the two opus cells are SUMMED, not one of them kept")
    check(cells["opus"]["tool_calls"] == 3, "every usage field is summed, not just tokens")
    check(cells["opus"]["updated_at"] == 9, "the newest timestamp survives")
    check(sum(c["cost_micros"] for c in cells.values()) == view["totals"]["cost_micros"],
          "the cells still add up to the totals a bill was built from")

    after = conversations.add_usage("proj-old", "opus", {"tokens_in": 1}, "new-1")
    check(after["by_model"]["opus"]["tokens_in"] == 61, "a new turn lands on the folded cell")
    stored = json.loads(path.read_text())["usage"]
    check("by_stage_model" not in stored and "by_model" in stored,
          "and the fold is written back once a turn writes the record")
    check(conversations.add_usage("proj-old", "opus", {"tokens_in": 9}, "old-1")["by_model"]["opus"]["tokens_in"] == 61,
          "the old idempotency ledger still guards against a replay")


def main():
    try:
        for fn in (test_a_new_project_keys_usage_per_model,
                   test_a_new_project_carries_no_lifecycle,
                   test_an_old_record_keeps_its_numbers):
            print(f"--- {fn.__name__} ---")
            fn()
    finally:
        shutil.rmtree(STATE, ignore_errors=True)
    print(f"\n{'FAILED: ' + '; '.join(FAILED) if FAILED else 'all checks pass'}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
