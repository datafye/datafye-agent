#!/usr/bin/env python3
"""PreToolUse guard checks (DAT-272).

Leaving a tool out of `allowed_tools` does NOT remove it. `allowed_tools`
becomes the CLI's `--allowedTools`, a PERMISSION allowlist, and this agent runs
`permission_mode="bypassPermissions"`, which makes permission checks moot.

⚠️ WHY THIS FILE EXISTS. `INTERNAL_TOOLS` has carried a comment saying Task "is
deliberately ABSENT, and stays absent" since the agent was written. The comment
is right about why and wrong that absence achieved it. Sutra ran the identical
arrangement: SUT-36 removed Task on 2026-08-04 and shipped it; on 2026-08-31
that agent launched six subagents anyway. Asserting the allowlist would have
passed green throughout, which is why this asserts the DENIAL, and asserts it
against the CONSTRUCTED options rather than the source text -- a formatter
wrapping a HookMatcher call would turn a source grep red while the hook was
correctly registered, and deleting the wiring while leaving a comment that
quotes it would keep it green with nothing registered.

    .venv/bin/python tests/test_tool_guards.py

Needs no Anthropic key and no provisioned agent. Exits non-zero on any failure.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import main  # noqa: E402

OK = FAILED = 0


def check(label, cond, detail=""):
    global OK, FAILED
    if cond:
        OK += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}  {detail}")


def denied(result):
    out = (result or {}).get("hookSpecificOutput") or {}
    return out.get("permissionDecision") == "deny"


async def run():
    # ---- the denial itself -------------------------------------------
    r = await main.deny_delegation({"tool_name": "Task", "tool_input": {"prompt": "go"}}, None, None)
    check("delegation is denied", denied(r), str(r))
    check("and the refusal explains itself rather than just saying no",
          len(((r or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")) > 120)

    # Fail-CLOSED: this guard exists to stop something, so an input shape it
    # does not recognise must still be a denial. The opposite of the read guard.
    for odd in ({}, {"tool_input": None}, {"tool_name": "Task"}):
        check(f"denied for an unrecognised input shape {odd}",
              denied(await main.deny_delegation(odd, None, None)))

    # ---- the wiring, against the real object -------------------------
    opts = main.build_agent_options_for_test()

    matched = set()
    for m in (opts.hooks or {}).get("PreToolUse", []):
        if main.deny_delegation in (getattr(m, "hooks", []) or []):
            matched.add(getattr(m, "matcher", None))
    for tool in main.DELEGATION_TOOLS:
        check(f"{tool} is hooked to the denial", tool in matched, str(sorted(matched)))

    check("Read is still hooked to the size guard",
          any(getattr(m, "matcher", None) == "Read"
              and main.guard_oversized_read in (getattr(m, "hooks", []) or [])
              for m in (opts.hooks or {}).get("PreToolUse", [])))

    for tool in main.DISALLOWED_DELEGATION_TOOLS:
        check(f"{tool} is also in disallowed_tools", tool in (opts.disallowed_tools or []))

    # The deny list is deliberately narrower than the hook list: the CLI warns
    # once per turn for a rule naming a tool it does not know.
    check("the deny list carries no name the CLI would warn about",
          "RunWorkflow" not in (opts.disallowed_tools or []))
    check("but RunWorkflow is still hooked, which defends a rename",
          "RunWorkflow" in main.DELEGATION_TOOLS)

    # Task is the ALIAS; Agent is the real name; Workflow and RemoteTrigger are
    # separate routes. Verified against claude 2.1.259.
    for tool in ("Task", "Agent", "Workflow", "RemoteTrigger"):
        check(f"{tool} is covered", tool in main.DELEGATION_TOOLS)

    # Pinned as a decision, not an oversight: CronCreate enqueues a prompt into
    # THIS session, so the scheduled turn inherits the prompt.
    check("CronCreate is deliberately NOT banned", "CronCreate" not in main.DELEGATION_TOOLS)

    # The real turn and this test must share ONE definition, not two that
    # happen to agree today.
    src_all = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "main.py")).read()
    check("the real construction uses the same hook definition the test does",
          src_all.count("hooks=pre_tool_use_hooks()") >= 2)

    src = src_all
    allowlist = src.split("INTERNAL_TOOLS = [")[1].split("\n]")[0]
    entries = "".join(ln.split("#")[0] for ln in allowlist.split("\n"))
    check("Task is still absent from the allowlist itself", '"Task"' not in entries)

    print(f"\n{OK} passed, {FAILED} failed")
    return 1 if FAILED else 0


sys.exit(asyncio.run(run()))
