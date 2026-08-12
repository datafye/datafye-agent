# Copyright 2025 Datafye
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Which Claude Code CLI is actually running a turn (DAT-215).

The agent's behaviour depends on the harness -- BASH_MAX_TIMEOUT_MS, the SDK's
max_buffer_size framing, which tools exist, what a blocked command suggests --
and until now nothing on the box reported which one it had. That gap is not
theoretical: "the bundled CLI is 2.1.85" was written into a Linear ticket, a
correction to that ticket, and CLAUDE.md, and all three were wrong. The number
had been read off the LOCAL dev venv, which resolves a different SDK from
production. One SSH session to a real box settled it: 2.1.228.

The lesson worth keeping is general. `requirements.txt` pins a RANGE
(`claude-agent-sdk>=0.2.128,<0.3`), so the bundled CLI is whatever resolution
picked on the day a box was built. A version derived from a range is a guess
about what resolution will do, not a fact -- and a fact nobody can read without
SSH is one people will guess about instead.

⚠️ THE HARNESS IS THE SDK'S BUNDLED CLI, NOT THE ONE ON PATH. `_find_cli`
checks `claude_agent_sdk/_bundled/claude` FIRST and only falls back to
`shutil.which("claude")`. The installer also puts a CLI at
`~datafye/.local/bin/claude`, which is NOT what runs a turn -- and on the box
that was measured, the two were the same version, which is exactly the sort of
coincidence that lets a wrong mental model survive. So this reports BOTH, and
says which one is in use.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The version probe runs a subprocess, so it is computed ONCE per process rather
# than per request. That is not merely an optimisation: /v1/bom is public and
# unauthenticated, and an endpoint that forks a 300 MB binary on demand is a
# free denial-of-service. The answer cannot change without the service
# restarting anyway -- an upgrade replaces the venv and restarts it.
_CACHE: dict[str, Any] | None = None

# 60s, not 10. The bundled CLI is a ~308 MB Bun binary, and its FIRST exec on a
# box has to fault the whole thing in from a cold EBS volume before Bun even
# starts. A warm re-run of the same binary takes about a second, which is what
# makes this easy to under-size: every measurement you take by hand is warm,
# because taking it is what warms it. The probe runs once per process and
# nothing waits on it, so a generous bound costs nothing and a tight one buys
# nothing.
_VERSION_TIMEOUT_SECONDS = 60


def _bundled_cli_path() -> str | None:
    """Where the SDK keeps its bundled CLI, computed the way the SDK computes it.

    Derived from the INSTALLED package location rather than a hardcoded path, so
    it follows the venv wherever it is and cannot drift from the SDK's own
    `_find_bundled_cli`. Mirrors it deliberately rather than importing it:
    that helper is a method on `SubprocessCLITransport` under `_internal`, and
    standing up a transport to ask it a question would be a heavier and more
    fragile coupling than eight lines that say the same thing.
    """
    try:
        import claude_agent_sdk
        name = "claude.exe" if os.name == "nt" else "claude"
        path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        return str(path) if path.is_file() else None
    except Exception:
        return None


def _version_of(cli_path: str) -> tuple[str | None, str | None]:
    """`(version, error)` from `claude --version`. Never raises.

    ⚠️ Returns the REASON on failure, not just None. The first cut of this
    reported a bare null, and on the first real box it did exactly that -- with
    no way to tell a timeout from a non-zero exit from a missing binary, which
    is the same "a fact nobody can read" problem this whole module was written
    to fix, reproduced one level down. An unknown that cannot say why is barely
    better than no field at all.
    """
    try:
        result = subprocess.run(
            [cli_path, "--version"],
            capture_output=True, text=True, timeout=_VERSION_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return None, f"exit {result.returncode}" + (f": {detail[:200]}" if detail else "")
        # "2.1.228 (Claude Code)" -- keep the whole line; the parenthetical is
        # part of what the binary calls itself and trimming it would invent a
        # format the CLI never promised.
        version = (result.stdout or "").strip()
        return (version or None), (None if version else "empty output")
    except subprocess.TimeoutExpired:
        return None, f"timed out after {_VERSION_TIMEOUT_SECONDS}s"
    except Exception as exc:
        # A binary built for another architecture is the other realistic
        # failure: the bundled CLI is a Bun build that needs AVX, so it cannot
        # be exercised on some dev machines at all.
        return None, f"{type(exc).__name__}: {exc}"


def describe() -> dict[str, Any]:
    """The harness this agent runs turns with, for /v1/bom.

    Never raises: this is reporting, and an agent that cannot say which CLI it
    has is still a working agent. Every field is nullable and a caller must
    treat null as "could not determine", NOT as "absent".
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    info: dict[str, Any] = {
        "cli_version": None,
        "cli_version_error": None,
        "cli_path": None,
        "cli_source": None,
        "sdk_version": None,
        "path_cli_version": None,
    }
    try:
        try:
            import claude_agent_sdk
            info["sdk_version"] = getattr(claude_agent_sdk, "__version__", None)
        except Exception:
            pass

        bundled = _bundled_cli_path()
        path_cli = shutil.which("claude")

        # Same order as _find_cli: bundled wins.
        if bundled:
            info["cli_path"], info["cli_source"] = bundled, "bundled"
        elif path_cli:
            info["cli_path"], info["cli_source"] = path_cli, "path"

        if info["cli_path"]:
            info["cli_version"], info["cli_version_error"] = _version_of(info["cli_path"])
            if info["cli_version_error"]:
                logger.warning("could not read the harness version from %s: %s",
                               info["cli_path"], info["cli_version_error"])

        # Reported even when it is not the harness, because the two being
        # confused is the whole reason this module exists. When they differ,
        # an operator can see it here instead of inferring it.
        if path_cli and path_cli != info["cli_path"]:
            info["path_cli_version"], _ = _version_of(path_cli)
    except Exception:
        logger.warning("could not determine the harness version", exc_info=True)

    # ⚠️ CACHE SUCCESS ONLY. Caching a failure keeps the wrong answer for the
    # life of the process, and this probe is MOST likely to fail on its first
    # call -- cold binary, cold page cache, a box still settling after boot.
    # The first cut cached unconditionally and did exactly that on the first
    # real box: `cli_version: null` pinned in, while running the same binary by
    # hand a minute later took under a second. Retrying costs one subprocess on
    # a page that is polled rarely, and only until it works once.
    if info["cli_version"]:
        _CACHE = info
    return info
