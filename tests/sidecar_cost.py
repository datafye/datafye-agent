#!/usr/bin/env python3
"""The agent's own side-calls are priced, exactly as the metering gateway prices them (DAT-485).

They used to be reported to accounts at $0, so a project's reported cost was quietly lower than the
gateway's: on u3, Haiku 9,764 in / 448 out shown as $0.0000 that the gateway priced at $0.0120.

Offline. The parity checks need a checkout of nvx-llm-gateway: set GATEWAY_REPO, or keep it beside this
repo. Without one they are reported as NOT RUN, and the suite exits 2 rather than claiming a pass.

    python tests/sidecar_cost.py
"""
import glob
import importlib.util
import logging
import os
import re
import sys

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, HERE)
import pricing  # noqa: E402

FAILED, NOT_RUN = [], []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        FAILED.append(what)


def gateway_repo():
    candidates = [os.environ.get("GATEWAY_REPO")] + [os.path.join(HERE, *[".."] * up, "nvx-llm-gateway") for up in (1, 2, 3)]
    found = None
    for c in candidates:
        if found is None and c and os.path.isfile(os.path.join(c, "src", "llm_gateway", "pricing.py")):
            found = os.path.abspath(c)
    return found


def test_the_measured_u3_turn():
    # Haiku 4.5: $1 in, $5 out per million. 9,764 x 1 + 448 x 5 = 12,004 micros = $0.0120.
    cost = pricing.cost_micros("claude-haiku-4-5-20251001", {"input_tokens": 9764, "output_tokens": 448})
    check(cost == 12004, f"the u3 side-calls cost 12,004 micros, the gateway's $0.0120 ({cost})")


def test_the_answering_model_and_the_alias_price_the_same():
    usage = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 50,
             "cache_creation_input_tokens": 200}
    a = pricing.cost_micros("claude-haiku-4-5", usage)
    b = pricing.cost_micros("claude-haiku-4-5-20251001", usage)
    check(a == b == 1000 + 500 + 5 + 250, f"alias and dated id both price as Haiku 4.5 ({a}, {b})")


def test_a_one_hour_cache_write_is_priced_at_twice_input():
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 300,
             "cache_creation": {"ephemeral_1h_input_tokens": 100}}
    cost = pricing.cost_micros("claude-haiku-4-5", usage)
    check(cost == 200 * 1.25 + 100 * 2, f"200 five-minute + 100 one-hour writes ({cost})")


def test_an_unpriced_model_is_none_with_a_warning_never_a_silent_zero():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    pricing.logger.addHandler(Grab())
    cost = pricing.cost_micros("kimi-k2", {"input_tokens": 10, "output_tokens": 10})
    check(cost is None, f"an unpriced model has no cost ({cost})")
    check(any("no price for model kimi-k2" in r for r in records), "and says so in a warning")
    check(pricing.cost_micros("claude-haiku-4-5", {}) is None, "no usage, no cost")


def test_every_sink_records_the_model_that_answered():
    text = open(os.path.join(HERE, "main.py")).read() + open(os.path.join(HERE, "activity.py")).read()
    sinks = re.findall(r'\.append\(\{"model":[^\n]*"usage": data\["usage"\][^\n]*', text)
    check(sinks and all('"answered_by": data.get("model")' in s for s in sinks),
          f"every side-call sink records answered_by ({len(sinks)} sinks)")
    fold = open(os.path.join(HERE, "main.py")).read()
    check("pricing.cost_micros(" in fold and "Tokens are captured; cost is 0" not in fold,
          "the turn's sidecar fold prices each call")


def test_the_table_and_the_algorithm_match_the_gateway():
    repo = gateway_repo()
    if repo is None:
        NOT_RUN.append("gateway parity (no nvx-llm-gateway checkout: set GATEWAY_REPO)")
        print("NOT RUN gateway parity: no nvx-llm-gateway checkout found (set GATEWAY_REPO)")
    else:
        import yaml
        for f in sorted(glob.glob(os.path.join(repo, "deploy", "*.yaml"))):
            theirs = yaml.safe_load(open(f)).get("prices") or []
            check(theirs == pricing.PRICES, f"PRICES equals the gateway's {os.path.basename(f)} table")
        spec = importlib.util.spec_from_file_location("gw_pricing", os.path.join(repo, "src", "llm_gateway", "pricing.py"))
        gw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gw)
        table = [gw.Price(**row) for row in pricing.PRICES]
        samples = [("claude-haiku-4-5-20251001", {"input_tokens": 9764, "output_tokens": 448}),
                   ("claude-opus-4-8", {"input_tokens": 12, "output_tokens": 3000, "cache_read_input_tokens": 90000,
                                        "cache_creation_input_tokens": 5000,
                                        "cache_creation": {"ephemeral_1h_input_tokens": 1000}}),
                   ("claude-sonnet-4-6", {"input_tokens": 7, "output_tokens": 1}),
                   ("claude-opus-4-1-20250805", {"input_tokens": 333, "output_tokens": 333}),
                   ("unknown-model", {"input_tokens": 5})]
        for model, usage in samples:
            mine, theirs = pricing.cost_micros(model, usage), gw.cost_micros(table, model, usage)
            check(mine == theirs, f"{model} costs the same here and at the gateway ({mine} vs {theirs})")


if __name__ == "__main__":
    test_the_measured_u3_turn()
    test_the_answering_model_and_the_alias_price_the_same()
    test_a_one_hour_cache_write_is_priced_at_twice_input()
    test_an_unpriced_model_is_none_with_a_warning_never_a_silent_zero()
    test_every_sink_records_the_model_that_answered()
    test_the_table_and_the_algorithm_match_the_gateway()
    if FAILED:
        print(f"\n{len(FAILED)} failure(s)")
    elif NOT_RUN:
        print(f"\nall run checks pass; NOT RUN: {NOT_RUN}")
    else:
        print("\nall checks pass")
    sys.exit(1 if FAILED else 2 if NOT_RUN else 0)
