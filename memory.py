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

Three SCOPES. All of it is the agent's memory, so the distinguishing word is
how far the knowledge reaches:

  - FLEET   (<app>/fleet_memory/): lessons distilled from across the whole
    fleet and shipped WITH THE AGENT BUILD. Read-only — curated out of band,
    never written by an agent. Because it lives in the app directory (which the
    installer replaces wholesale on every install and upgrade) the bank arrives
    as a unit, self-prunes deleted files, and cannot collide with anything the
    agent writes.

  - USER    (<state>/memory/ + <state>/CLAUDE.md): facts about THIS user's
    workspace that hold across their projects — preferences, reusable
    patterns, lessons from one project that apply to the next. Owned by this
    module. (Called GLOBAL until fleet memory made that word ambiguous.)

  - PROJECT (<project>/memory/ + <project>/CLAUDE.md): facts specific to
    one project. The project folder and its CLAUDE.md/memory are scaffolded by
    conversations.py; the SDK auto-loads the project's CLAUDE.md as project
    memory (setting_sources=["project"]), so this module injects only the things
    the SDK does NOT auto-load: the user notes, the user index, the fleet index,
    and the per-project memory INDEX (memory/MEMORY.md is not auto-loaded;
    CLAUDE.md is).

build_memory_context() renders the always-on block for the system prompt: the
protocol plus the current index/notes content. Bodies of individual memory
files are read on demand by the agent, not injected — only the one-line indexes
are always-on, to keep the per-turn context small. That is also why the fleet
bank must stay a small number of TOPIC files rewritten as they accumulate
rather than one file per lesson: its index is paid for on every single turn.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import paths

logger = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent

# FLEET — read-only, ships with the app clone (mirrors skills.py's system tier).
FLEET_DIR = Path(
    os.environ.get(
        "DATAFYE_AGENT_FLEET_MEMORY_DIR",
        str(_APP_DIR / "fleet_memory"),
    )
)
FLEET_INDEX = FLEET_DIR / "MEMORY.md"

# USER — this user's workspace, across their projects. Agent-writable.
USER_DIR = Path(paths.state_path("memory"))
USER_INDEX = USER_DIR / "MEMORY.md"
USER_CLAUDE_MD = Path(paths.state_path("CLAUDE.md"))

_USER_INDEX_TEMPLATE = """# User Memory

Memory for this user's workspace: preferences, reusable patterns, and lessons
that apply to more than one of their projects. One line per memory file.
(Empty for now.)
"""

_USER_CLAUDE_TEMPLATE = """# Working Notes

Durable notes for this user's Datafye workspace, across projects. Keep it
concise. (Empty for now.)
"""


def ensure_user_memory() -> None:
    """Scaffold the user memory dir, its index, and the user CLAUDE.md if
    absent. Best-effort; idempotent. (Fleet memory needs no scaffolding — it
    ships with the build, and its absence is a valid state.)"""
    try:
        USER_DIR.mkdir(parents=True, exist_ok=True)
        if not USER_INDEX.exists():
            USER_INDEX.write_text(_USER_INDEX_TEMPLATE)
        if not USER_CLAUDE_MD.exists():
            USER_CLAUDE_MD.write_text(_USER_CLAUDE_TEMPLATE)
    except OSError as e:
        logger.warning("Could not scaffold user memory at %s: %s", USER_DIR, e)


def _read(path: os.PathLike | str) -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


def build_memory_context(project_cwd: str | None) -> str:
    """Render the always-on MEMORY block for the system prompt.

    `project_cwd` is the current project folder (the chat turn's cwd), or None
    for conversation-less/fallback requests (then only fleet + user memory)."""
    user_notes = _read(USER_CLAUDE_MD) or "(none yet)"
    user_index = _read(USER_INDEX) or "(empty)"
    fleet_index = _read(FLEET_INDEX)

    project_mem_dir = os.path.join(project_cwd, "memory") if project_cwd else None
    project_index = _read(os.path.join(project_mem_dir, "MEMORY.md")) if project_mem_dir else ""

    # Fleet memory is optional: a self-hosted or pre-seed instance has no bank,
    # and an empty stub in the prompt would only invite the model to wonder
    # about it. Omit the scope entirely rather than announce that it is empty.
    # An index carrying only its header counts as EMPTY — the bank ships as a
    # scaffold before it is seeded, so presence of the file proves nothing.
    has_fleet = any(
        line.lstrip().startswith("- ") for line in fleet_index.splitlines()
    )

    fleet_scope_line = (
        f"- FLEET memory ({FLEET_DIR}): lessons distilled from across the whole\n"
        f"  Datafye fleet, shipped with this agent build. READ-ONLY — treat it as\n"
        f"  established practice, and never write to it (see below).\n"
        if has_fleet else ""
    )
    fleet_section = (
        f"\n\nFLEET MEMORY INDEX (read-only):\n{fleet_index}"
        if has_fleet else ""
    )
    fleet_write_rule = (
        "\n- NEVER write to or edit FLEET memory. It is curated centrally and replaced\n"
        "  wholesale on each agent upgrade, so an edit there would be lost anyway. When\n"
        "  a fleet lesson needs correcting, record what you found in USER memory."
        if has_fleet else ""
    )

    per_project_line = (
        f"- PROJECT memory ({project_mem_dir}) plus this project's CLAUDE.md: "
        f"facts specific to the project you are working on now."
        if project_mem_dir else
        "- PROJECT memory is unavailable for this request (no project folder)."
    )

    project_section = (
        f"\n\nTHIS PROJECT'S MEMORY INDEX:\n{project_index or '(empty)'}"
        if project_mem_dir else ""
    )

    return f"""MEMORY:
You keep durable memory across sessions as plain markdown files, so you do not
relearn the same things each time. It is organised by SCOPE — how far the
knowledge reaches:
{fleet_scope_line}- USER memory ({USER_DIR}): facts about this user's workspace that hold across
  their projects — their preferences, reusable patterns, lessons from one
  project that apply to the next.
{per_project_line}

How to use it:
- The indexes below list what is remembered (one line each). When a line looks
  relevant, Read that file for the detail — do not guess from the one-liner.
- When you learn something durable and useful for a FUTURE session, write a short
  markdown file in the right memory dir and add a one-line pointer to that dir's
  MEMORY.md. Choose USER vs PROJECT by whether it is reusable across
  projects. Keep this project's CLAUDE.md (working memory) and PROJECT.md
  current too.
- Do NOT record transient, conversation-only details, secrets/API keys, or
  anything already obvious from the code and files.{fleet_write_rule}

⚠️ Two kinds of thing get saved, and they need different evidence:
- An OBSERVATION about this user's setup ("their foundry runs SIP") is cheap to
  record and cheap to be wrong about. Write it freely.
- A GENERAL RULE about how the platform behaves ("X always means Y") is expensive
  to be wrong about, because you will apply it confidently next time without
  re-checking. Never write one from a single observation or a plausible
  inference. Save it only once you have SEEN it hold, and say how you checked. If
  you cannot say how you verified it, it is a hypothesis — leave it out, or label
  it as one. An agent has already saved a confidently wrong diagnostic rule this
  way; check fleet memory first, and if it contradicts you, it wins.

WORKING NOTES (this user, across projects):
{user_notes}

USER MEMORY INDEX:
{user_index}{fleet_section}{project_section}"""
