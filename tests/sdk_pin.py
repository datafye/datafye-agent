#!/usr/bin/env python3
"""The Claude Agent SDK, and so the Claude Code every turn runs on, is pinned and verified (DAT-486).

The SDK launches the Claude Code it BUNDLES, so one requirements line decides the harness. With a
range, each box kept whatever resolved the day it was built, and the newest in range would have moved
Claude Code past 2.1.277, where a resumed session's usage becomes a running total.

Offline: the installer step runs against a stub SDK and a stub CLI, so no network, no real SDK.

    python tests/sdk_pin.py
"""
import asyncio
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, HERE)
FAILED = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        FAILED.append(what)


def requirements():
    return open(os.path.join(HERE, "requirements.txt")).read()


def installer():
    return open(os.path.join(HERE, "install", "install_template.sh")).read()


def test_the_sdk_is_pinned_exactly():
    lines = [l.strip() for l in requirements().splitlines() if l.strip().startswith("claude-agent-sdk")]
    check(len(lines) == 1 and re.fullmatch(r"claude-agent-sdk==\d+\.\d+\.\d+", lines[0]) is not None,
          f"requirements.txt pins claude-agent-sdk to one exact version ({lines})")


def test_claude_code_is_never_installed_unversioned():
    text = installer()
    bare = re.findall(r"claude\.ai/install\.sh \| bash(?! -s)", text)
    check(not bare, f"claude.ai/install.sh is always given a version ({len(bare)} bare call(s))")
    check("Claude Code CLI already installed" not in text,
          "no 'already installed, skip' path is left to keep a box on whatever it first got")


def _run_step(requirements_line, sdk_reports, bundled_reports):
    """Run the installer's pin step with a stub venv python and a stub bundled CLI."""
    text = installer()
    a = text.index("# ── Step: The pinned Claude Agent SDK")
    b = text.index("\n# ── Step:", a + 10)
    step = re.sub(r'CLAUDE_BIN="/home/[a-z]+/\.local/bin/claude"', 'CLAUDE_BIN="$T/none"', text[a:b])
    step = step.replace("curl -fsSL https://claude.ai/install.sh | bash -s", "true")
    t = tempfile.mkdtemp()
    os.makedirs(f"{t}/venv/bin"); os.makedirs(f"{t}/app"); os.makedirs(f"{t}/bin")
    open(f"{t}/app/requirements.txt", "w").write(requirements_line + "\n")
    harness = f"{t}/claude"
    open(harness, "w").write(f"#!/bin/bash\necho '{bundled_reports} (Claude Code)'\n")
    open(f"{t}/venv/bin/python", "w").write(f"#!/bin/bash\ncat >/dev/null\necho '{sdk_reports.replace('HARNESS', harness)}'\n")
    open(f"{t}/bin/timeout", "w").write("#!/bin/bash\nshift; exec \"$@\"\n")
    open(f"{t}/bin/sudo", "w").write("#!/bin/bash\nwhile [ \"$1\" = -u ] || [ \"$1\" = -H ]; do [ \"$1\" = -u ] && shift; shift; done\nexec \"$@\"\n")
    for f in (harness, f"{t}/venv/bin/python", f"{t}/bin/timeout", f"{t}/bin/sudo"):
        os.chmod(f, 0o755)
    open(f"{t}/step.sh", "w").write(step)
    script = (f'export PATH="{t}/bin:$PATH"; set -e; T="{t}"; info(){{ :; }}; ok(){{ :; }}; warn(){{ :; }};'
              f' error(){{ echo "$*" >&2; }}; next_step(){{ :; }}; STEP=1; TOTAL_STEPS=1;'
              f' VENV_DIR="{t}/venv"; AGENT_CODE_DIR="{t}/app"; source "{t}/step.sh"')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r.returncode, r.stderr.strip()


def test_the_installer_step_passes_only_the_pinned_sdk_and_its_bundled_cli():
    code, _ = _run_step("claude-agent-sdk==0.2.152", "0.2.152 HARNESS 2.1.259", "2.1.259")
    check(code == 0, "the pinned SDK with its own bundled Claude Code passes")
    code, err = _run_step("claude-agent-sdk>=0.2.128,<0.3", "0.2.152 HARNESS 2.1.259", "2.1.259")
    check(code != 0 and "exact version" in err, f"a range instead of a pin fails the install ({err})")
    code, err = _run_step("claude-agent-sdk==0.2.188", "0.2.152 HARNESS 2.1.259", "2.1.259")
    check(code != 0 and "pins 0.2.188" in err, f"an installed SDK that is not the pin fails the install ({err})")
    code, err = _run_step("claude-agent-sdk==0.2.152", "0.2.152 - -", "2.1.259")
    check(code != 0 and "no bundled Claude Code" in err, f"an SDK with no bundled CLI fails the install ({err})")
    code, err = _run_step("claude-agent-sdk==0.2.152", "0.2.152 HARNESS 2.1.259", "2.1.331")
    check(code != 0 and "expected 2.1.259" in err, f"a bundled CLI that is not the SDK's own fails the install ({err})")


def test_bom_reports_the_harness():
    os.environ.setdefault("DATAFYE_AGENT_STATE_DIR", tempfile.mkdtemp())
    import harness
    import main
    real = harness.describe
    harness.describe = lambda: {"sdk_version": "9.9.9", "cli_version": "1.2.3 (Claude Code)", "cli_source": "bundled"}
    try:
        doc = asyncio.run(main.bom())
    finally:
        harness.describe = real
    check(doc.get("harness", {}).get("sdk_version") == "9.9.9" and doc["harness"].get("cli_source") == "bundled",
          f"/v1/bom carries the harness block ({doc.get('harness')})")


if __name__ == "__main__":
    test_the_sdk_is_pinned_exactly()
    test_claude_code_is_never_installed_unversioned()
    test_the_installer_step_passes_only_the_pinned_sdk_and_its_bundled_cli()
    test_bom_reports_the_harness()
    print(f"\n{len(FAILED)} failure(s)" if FAILED else "\nall checks pass")
    sys.exit(1 if FAILED else 0)
