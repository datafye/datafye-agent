#!/usr/bin/env python3
"""The agent defaults to the npm MCP server, and the installer never persists a default (DAT-487).

An agent upgrades itself within minutes of a release; a deployment only when it is re-provisioned. A
2.0.63 deployment has no /mcp, so the Java default would leave an upgraded agent with no Datafye
tools. And because agent.env is carried forward on every upgrade, an installer that writes the
default there would freeze each box on this release's default for good.

    python tests/mcp_server_default.py
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FAILED = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        FAILED.append(what)


def resolved_server(env_value):
    """What main.py resolves DATAFYE_API_MCP_SERVER to, in a fresh interpreter."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATAFYE_AGENT_API_MCP")}
    env["DATAFYE_AGENT_STATE_DIR"] = tempfile.mkdtemp()
    if env_value is not None:
        env["DATAFYE_AGENT_API_MCP_SERVER"] = env_value
    r = subprocess.run([sys.executable, "-c", "import main; print(main.DATAFYE_API_MCP_SERVER, main.DATAFYE_API_MCP_URL)"],
                       cwd=HERE, env=env, capture_output=True, text=True)
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else f"exit {r.returncode}"


def installer_validation(value):
    """Run the installer's own validation block with MCP_SERVER=value; return (exit code, MCP_SERVER after)."""
    text = open(os.path.join(HERE, "install", "install_template.sh")).read()
    a = text.index("# ── Validate the MCP server choice")
    b = text.index("\n# ──", a + 10)
    script = f'set -e; error(){{ :; }}; MCP_SERVER="{value}"\n{text[a:b]}\necho "[$MCP_SERVER]"'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def main():
    unset = resolved_server(None)
    check(unset.startswith("npm ") and ":3200/mcp" in unset, f"with no setting the agent uses npm ({unset})")
    empty = resolved_server("")
    check(empty.startswith("npm "), f"an empty setting (what the installer writes for 'no choice') means npm ({empty})")
    java = resolved_server("java")
    check(java.startswith("java ") and ":7776/mcp" in java, f"DATAFYE_AGENT_API_MCP_SERVER=java still selects the Java server ({java})")

    code, out = installer_validation("")
    check(code == 0 and out == "[]", f"the installer leaves an unset choice unset, so agent.env carries no default ({out})")
    code, out = installer_validation("java")
    check(code == 0 and out == "[java]", f"an explicit --mcp-server java is kept ({out})")
    code, _ = installer_validation("bogus")
    check(code != 0, "an invalid --mcp-server still fails the install")

    text = open(os.path.join(HERE, "install", "install_template.sh")).read()
    check(re.search(r'MCP_SERVER="\$\{MCP_SERVER:-', text) is None,
          "nothing in the installer substitutes a default MCP server")
    print(f"\n{len(FAILED)} failure(s)" if FAILED else "\nall checks pass")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
