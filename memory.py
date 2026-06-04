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

"""
Cross-session memory for the Datafye Agent.

Memory is convention-based: plain markdown files the agent reads at the start
of a turn and writes during a turn, so it doesn't relearn the same things every
session. There is no special memory tool — the agent uses Read/Write, guided by
a protocol in the system prompt (the same shape Claude Code's own memory uses).

Two scopes:

  - GLOBAL  (<state>/memory/ + <state>/CLAUDE.md): cross-strategy facts — the
    user's preferences, reusable patterns, lessons that apply to more than one
    strategy. Owned by this module.

  - PER-STRATEGY (<strategy>/memory/ + <strategy>/CLAUDE.md): facts specific to
    one strategy. The strategy folder and its CLAUDE.md/memory are scaffolded by
    conversations.py; the SDK auto-loads the strategy's CLAUDE.md as project
    memory (setting_sources=["project"]), so this module injects only the things
    the SDK does NOT auto-load: the global notes, the global index, and the
    per-strategy memory INDEX (memory/MEMORY.md is not auto-loaded; CLAUDE.md is).

build_memory_context() renders the always-on block for the system prompt: the
protocol plus the current index/notes content. Bodies of individual memory
files are read on demand by the agent, not injected — only the one-line indexes
are always-on, to keep the per-turn context small.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import paths

logger = logging.getLogger(__name__)

GLOBAL_DIR = Path(paths.state_path("memory"))
GLOBAL_INDEX = GLOBAL_DIR / "MEMORY.md"
GLOBAL_CLAUDE_MD = Path(paths.state_path("CLAUDE.md"))

_GLOBAL_INDEX_TEMPLATE = """# Global Memory

Cross-strategy memory: user preferences, reusable patterns, and lessons that
apply to more than one strategy. One line per memory file. (Empty for now.)
"""

_GLOBAL_CLAUDE_TEMPLATE = """# Global Working Notes

Durable, cross-strategy notes for this user's Datafye workspace. Keep it concise.
(Empty for now.)
"""


def ensure_global_memory() -> None:
    """Scaffold the global memory dir, its index, and the global CLAUDE.md if
    absent. Best-effort; idempotent."""
    try:
        GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
        if not GLOBAL_INDEX.exists():
            GLOBAL_INDEX.write_text(_GLOBAL_INDEX_TEMPLATE)
        if not GLOBAL_CLAUDE_MD.exists():
            GLOBAL_CLAUDE_MD.write_text(_GLOBAL_CLAUDE_TEMPLATE)
    except OSError as e:
        logger.warning("Could not scaffold global memory at %s: %s", GLOBAL_DIR, e)


def _read(path: os.PathLike | str) -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


def build_memory_context(strategy_cwd: str | None) -> str:
    """Render the always-on MEMORY block for the system prompt.

    `strategy_cwd` is the current strategy folder (the chat turn's cwd), or None
    for conversation-less/fallback requests (then only global memory is shown)."""
    global_notes = _read(GLOBAL_CLAUDE_MD) or "(none yet)"
    global_index = _read(GLOBAL_INDEX) or "(empty)"

    strat_mem_dir = os.path.join(strategy_cwd, "memory") if strategy_cwd else None
    strat_index = _read(os.path.join(strat_mem_dir, "MEMORY.md")) if strat_mem_dir else ""

    per_strategy_line = (
        f"- PER-STRATEGY memory ({strat_mem_dir}) plus this strategy's CLAUDE.md: "
        f"facts specific to the strategy you are working on now."
        if strat_mem_dir else
        "- PER-STRATEGY memory is unavailable for this request (no strategy folder)."
    )

    strat_section = (
        f"\nTHIS STRATEGY'S MEMORY INDEX:\n{strat_index or '(empty)'}"
        if strat_mem_dir else ""
    )

    return f"""MEMORY:
You keep durable memory across sessions as plain markdown files, so you do not
relearn the same things each time. Two scopes:
- GLOBAL memory ({GLOBAL_DIR}): cross-strategy facts — the user's preferences,
  reusable patterns, lessons that apply to more than one strategy.
{per_strategy_line}

How to use it:
- The indexes below list what is remembered (one line each). When a line looks
  relevant, Read that file for the detail — do not guess from the one-liner.
- When you learn something durable and useful for a FUTURE session, write a short
  markdown file in the right memory dir and add a one-line pointer to that dir's
  MEMORY.md. Choose GLOBAL vs PER-STRATEGY by whether it is reusable across
  strategies. Keep this strategy's CLAUDE.md (working memory) and PROJECT.md
  current too.
- Do NOT record transient, conversation-only details, secrets/API keys, or
  anything already obvious from the code and files.

GLOBAL WORKING NOTES:
{global_notes}

GLOBAL MEMORY INDEX:
{global_index}{strat_section}"""
