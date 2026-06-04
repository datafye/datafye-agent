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
Per-user strategy store for the Datafye Agent.

A "conversation" (what the frontend calls a project, and what the user
thinks of as a *strategy*) is the agent workspace's top-level entity. Each
one is now a DIRECTORY under the agent state root:

    <state>/strategies/<id>/
      meta.json          # name, timestamps, SDK session id, messages, commentary
      CLAUDE.md          # per-strategy durable memory (auto-loaded by the SDK
                         #   as project memory when this folder is the cwd)
      PROJECT.md         # plain-language description of the strategy
      memory/MEMORY.md   # index of granular per-strategy memory files
      .claude/skills/    # per-strategy user-authored skills
      <algo code...>     # the strategy's Python lives directly in the folder

The folder IS the working directory for chat turns on this strategy, so the
agent's files, its per-strategy memory, and its per-strategy skills all live
in one place that survives a restart. This replaces the previous
one-JSON-file-per-conversation layout (`<id>.json`); legacy files are
migrated into folders on load.

meta.json is plain JSON, not encrypted — conversation content is not a
secret key, and the Claude Agent SDK already writes its own session
transcripts to disk unencrypted. Files are mode 0600; writes go via a temp
file + atomic rename so a crash mid-write cannot truncate an existing file.

The agent serves exactly one user, so there is no per-user namespacing and
no concurrency control beyond the atomic rename.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import paths

logger = logging.getLogger(__name__)

# Base directory holding one sub-folder per strategy. `DATAFYE_AGENT_STRATEGIES_DIR`
# is the modern knob; `DATAFYE_AGENT_CONVERSATIONS_DIR` is still honoured as the
# base for back-compat. Default lives under the single agent state root.
_BASE_DIR = Path(
    os.environ.get(
        "DATAFYE_AGENT_STRATEGIES_DIR",
        os.environ.get("DATAFYE_AGENT_CONVERSATIONS_DIR", paths.state_path("strategies")),
    )
)

# Where the previous layout kept <id>.json files. Migrated into folders on load.
_LEGACY_DIR = Path(paths.state_path("conversations"))

# Words dropped when deducing a name from the first message.
_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "and", "or", "with", "on", "in",
    "my", "i", "want", "build", "create", "make", "using", "use", "that",
    "when", "from", "do", "let", "lets", "can", "you", "help", "me", "is",
}

# --- Scaffold templates -------------------------------------------------
# Kept deliberately small: CLAUDE.md is auto-loaded into the model's context
# on every turn (project memory), so boilerplate here is paid for repeatedly.
# These are living documents the agent maintains as the strategy evolves.

_CLAUDE_MD_TEMPLATE = """# Strategy: {name}

This is your durable working memory for THIS strategy. Keep it current as the
strategy takes shape. Prefer short bullet points over prose.

- Idea: (what this strategy is trying to do)
- Data: (datasets / schemas / symbols in use)
- Mode: (backtest / paper)
- Decisions: (key choices and why)
- Status: (what works, what doesn't, what's next)
"""

_PROJECT_MD_TEMPLATE = """# {name}

A plain-language description of this trading strategy. Maintain this file as the
strategy develops: the idea in everyday words, the data it relies on, how the
logic works, the results seen so far, and lessons learned. Keep it readable and
engaging, not a dry spec.
"""

_MEMORY_INDEX_TEMPLATE = """# Strategy Memory

One line per memory file kept in this folder. (Empty for now.)
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def strategy_dir(conversation_id: str) -> Path:
    """The directory that holds everything for one strategy."""
    return _BASE_DIR / conversation_id


def _meta_path(conversation_id: str) -> Path:
    return strategy_dir(conversation_id) / "meta.json"


def deduce_name(first_message: str) -> str:
    """Derive a short kebab-case name from the user's first message."""
    words = [
        w for w in re.sub(r"[^a-z0-9\s-]", " ", (first_message or "").lower()).split()
        if w and w not in _STOPWORDS
    ]
    return "-".join(words[:4]) or "new-project"


def _read(conversation_id: str) -> Optional[dict]:
    p = _meta_path(conversation_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        logger.warning("Could not read strategy %s: %s", conversation_id, e)
        return None


def _write(record: dict) -> None:
    """Persist meta.json atomically (temp file + rename)."""
    d = strategy_dir(record["id"])
    d.mkdir(parents=True, exist_ok=True)
    p = d / "meta.json"
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)


def _scaffold(conversation_id: str, name: str) -> None:
    """Create the strategy folder's CLAUDE.md / PROJECT.md / memory / .claude/skills
    if they don't already exist. Idempotent — never clobbers an agent's edits."""
    d = strategy_dir(conversation_id)
    try:
        (d / "memory").mkdir(parents=True, exist_ok=True)
        (d / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
        files = {
            d / "CLAUDE.md": _CLAUDE_MD_TEMPLATE.format(name=name),
            d / "PROJECT.md": _PROJECT_MD_TEMPLATE.format(name=name),
            d / "memory" / "MEMORY.md": _MEMORY_INDEX_TEMPLATE,
        }
        for path, content in files.items():
            if not path.exists():
                path.write_text(content)
    except OSError as e:
        logger.warning("Could not scaffold strategy folder %s: %s", conversation_id, e)


def meta(record: dict) -> dict:
    """The listing view of a strategy — no message/commentary bodies."""
    return {
        "id": record["id"],
        "name": record["name"],
        "created_at": record.get("created_at", 0),
        "updated_at": record.get("updated_at", 0),
        "message_count": len(record.get("messages", [])),
    }


def list_conversations() -> list:
    """All strategies, metadata only, most-recently-updated first."""
    _migrate_legacy()
    if not _BASE_DIR.exists():
        return []
    out = []
    for child in _BASE_DIR.iterdir():
        if not child.is_dir():
            continue
        mp = child / "meta.json"
        if not mp.exists():
            continue
        try:
            out.append(meta(json.loads(mp.read_text())))
        except Exception as e:
            logger.warning("Skipping unreadable strategy %s: %s", child.name, e)
    out.sort(key=lambda c: c.get("updated_at", 0), reverse=True)
    return out


def _new_record(conversation_id: str, first_message: str = "") -> dict:
    """Build a fresh, empty strategy record with the given id."""
    now = _now_ms()
    return {
        "id": conversation_id,
        "name": deduce_name(first_message),
        "created_at": now,
        "updated_at": now,
        "sdk_session_id": None,
        "messages": [],
        "commentary": [],
    }


def create(first_message: str = "") -> dict:
    """Create a new strategy with a freshly-minted id; the name is deduced from
    the first message.

    LEGACY: in the current architecture the accounts service is the
    authoritative project registry and mints the id. New chat threads arrive
    with an accounts-minted conversation_id; use ensure() to materialise the
    record for such an id. This id-minting path is kept only for the now-unused
    POST /v1/conversations endpoint."""
    record = _new_record("c-" + uuid.uuid4().hex[:12], first_message)
    _write(record)
    _scaffold(record["id"], record["name"])
    logger.info("Created strategy %s (%s)", record["id"], record["name"])
    return record


def ensure(conversation_id: str) -> dict:
    """Return the strategy record for `conversation_id`, creating the folder
    (meta.json + scaffold) with that exact id if it does not yet exist.

    Unlike create(), the id is taken as given — never minted — because the
    accounts service owns the project registry and supplies the id. This is
    what makes chat against an accounts-minted id always persist:
    append_message / append_commentary no-op when the record is absent, so the
    agent calls ensure() first."""
    _migrate_legacy()
    existing = _read(conversation_id)
    if existing is not None:
        # Folder may pre-date the scaffold step; ensure it's present.
        _scaffold(conversation_id, existing.get("name", conversation_id))
        return existing
    record = _new_record(conversation_id)
    _write(record)
    _scaffold(conversation_id, record["name"])
    logger.info("Materialised strategy %s for accounts-minted id", conversation_id)
    return record


def get(conversation_id: str) -> Optional[dict]:
    """Full strategy record, or None if it does not exist."""
    return _read(conversation_id)


def rename(conversation_id: str, name: str) -> Optional[dict]:
    """Rename a strategy. Returns the updated record, or None if absent."""
    record = _read(conversation_id)
    if record is None:
        return None
    record["name"] = (name or "").strip() or record["name"]
    record["updated_at"] = _now_ms()
    _write(record)
    return record


def append_message(conversation_id: str, role: str, content: str) -> None:
    """Append a user/assistant turn. No-op if the strategy does not exist
    (e.g. a frontend running in local-only fallback mode)."""
    record = _read(conversation_id)
    if record is None:
        return
    record.setdefault("messages", []).append(
        {"role": role, "content": content, "at": _now_ms()}
    )
    record["updated_at"] = _now_ms()
    _write(record)


def append_commentary(conversation_id: str, text: str, kind: Optional[str] = None) -> dict:
    """Append one activity-log entry and return it (so the caller can also emit
    it live over SSE). Persists only if the strategy exists."""
    entry = {"text": text, "kind": kind, "at": _now_ms()}
    record = _read(conversation_id)
    if record is not None:
        record.setdefault("commentary", []).append(entry)
        record["updated_at"] = _now_ms()
        _write(record)
    return entry


def get_sdk_session(conversation_id: str) -> Optional[str]:
    """The Claude Agent SDK session id to resume, or None."""
    record = _read(conversation_id)
    return record.get("sdk_session_id") if record else None


def set_sdk_session(conversation_id: str, session_id: str) -> None:
    """Persist the SDK session id so resume survives an agent restart."""
    record = _read(conversation_id)
    if record is None or not session_id:
        return
    if record.get("sdk_session_id") != session_id:
        record["sdk_session_id"] = session_id
        _write(record)


# --- Legacy migration ---------------------------------------------------

_migrated = False


def _migrate_legacy() -> None:
    """One-shot: convert any old `<conversations>/<id>.json` files into the new
    `<strategies>/<id>/meta.json` folder layout. Best-effort and idempotent;
    runs at most once per process."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        if not _LEGACY_DIR.exists() or _LEGACY_DIR.resolve() == _BASE_DIR.resolve():
            return
        for f in _LEGACY_DIR.glob("*.json"):
            try:
                record = json.loads(f.read_text())
                cid = record.get("id") or f.stem
                if _meta_path(cid).exists():
                    continue  # already migrated
                record["id"] = cid
                _write(record)
                _scaffold(cid, record.get("name", cid))
                logger.info("Migrated legacy conversation %s into a strategy folder", cid)
            except Exception as e:
                logger.warning("Could not migrate legacy conversation %s: %s", f.name, e)
    except Exception as e:
        logger.warning("Legacy conversation migration skipped: %s", e)
