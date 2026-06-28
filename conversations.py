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

A plain-language story of this trading strategy, written for the user to read.
Maintain it as the strategy develops. Cover, in everyday words:
- The idea: what this strategy is trying to capture, and the intuition behind it.
- The data: which datasets, schemas, and symbols it relies on, and why.
- How it works: the logic, in plain terms (use an analogy where it helps).
- Results: what testing has shown so far, honestly.
- Lessons: what worked, what didn't, and the pitfalls to avoid next time.

Keep it engaging and readable, not a dry spec.
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


# A project's lifecycle is a *track* — an ordered list of stages — that the agent
# chooses from the project's inferred intent (see main.classify_lifecycle). Chat
# and research have no build track (empty => the workspace shows no stepper).
# "Explore" is the common opening stage: a project starts from intent/discovery,
# not necessarily a pre-formed idea. Trading vocabulary on the tail: Backtest =
# refine-on-historical-data, Validate = paper-trade on live data, Deploy = live
# (real-money) trading; a non-trading build ends at Ship instead.
TRACK_TRADING = ["Explore", "Design", "Build", "Backtest", "Validate", "Deploy"]
TRACK_BUILD = ["Explore", "Design", "Build", "Ship"]   # non-trading build (dashboard / tool)

# Standard tracks per known intent. Open vocabulary: an intent not listed here
# resolves via track_for_intent()'s fallback ([]), and the agent may override
# with a track it composes for a novel build intent.
TRACKS = {
    "algo": TRACK_TRADING,
    "signal": TRACK_TRADING,    # same lifecycle; the artifact is a signal generator
    "dashboard": TRACK_BUILD,
    "app": TRACK_BUILD,
    "tool": TRACK_BUILD,
    "analysis": [],
    "research": [],
    "chat": [],
}
DEFAULT_INTENT = "chat"

# Back-compat alias: the full trading track is what "STAGES" used to be.
STAGES = TRACK_TRADING


def track_for_intent(intent: str) -> list:
    """The ordered stage track for an intent. Known intents map to a standard
    track; an unknown intent yields no track ([]) unless the agent supplies a
    composed one. Chat/research are intentionally trackless (no stepper)."""
    return list(TRACKS.get((intent or "").strip().lower(), []))

# The per-turn usage fields we accumulate, kept as a tuple so the meta record,
# the totals roll-up, and the accounts report all stay in lockstep.
_USAGE_FIELDS = ("tokens_in", "tokens_out", "cache_read", "cache_create",
                 "cost_micros", "tool_calls", "turns")

# How many recent idempotency keys to remember per strategy (bounds the meta).
_MAX_USAGE_KEYS = 200


def _empty_usage() -> dict:
    return {
        "totals": {f: 0 for f in _USAGE_FIELDS},
        "by_stage_model": {},
        "updated_at": 0,
        "applied_keys": [],
    }


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
        # Lifecycle: the agent infers `intent` and the `track` (ordered stages)
        # for this project. `stage` = where it is now; `maxStage` = furthest
        # reached (never regresses). An empty track (chat/research) => no stepper.
        "intent": DEFAULT_INTENT,
        "track": [],
        "stage": "",
        "maxStage": "",
        # Token/cost/tool usage, accumulated one turn-delta at a time, tracked
        # per (stage × model). Drives the workspace telemetry (survives reload
        # via /history) and mirrors what the agent reports to accounts (billing +
        # the hosted-tier quota meter). `applied_keys` is a capped idempotency
        # ledger so a re-applied turn can't double-count.
        "usage": _empty_usage(),
    }


def _stage_index(stage: str, track: list) -> int:
    try:
        return (track or STAGES).index(stage)
    except ValueError:
        return 0


def set_stage(conversation_id: str, stage: str) -> Optional[dict]:
    """Set the project's current lifecycle stage *within its track*, promoting
    the `maxStage` high-water mark if this advances past it. Ignores a stage not
    in the project's track. Returns the updated record (or None)."""
    record = _read(conversation_id)
    if record is None:
        return None
    track = record.get("track") or []
    if stage and stage in track:
        record["stage"] = stage
        prev_max = record.get("maxStage") or record.get("stage") or (track[0] if track else "")
        if _stage_index(stage, track) > _stage_index(prev_max, track):
            record["maxStage"] = stage
        elif not record.get("maxStage"):
            record["maxStage"] = prev_max
        record["updated_at"] = _now_ms()
        _write(record)
    return record


def set_intent_track(conversation_id: str, intent: str, track: list,
                     stage: Optional[str] = None) -> Optional[dict]:
    """Set the project's inferred `intent` and lifecycle `track` (and optionally
    the current `stage`), keeping `stage`/`maxStage` consistent with the new
    track. Returns the updated record (or None)."""
    record = _read(conversation_id)
    if record is None:
        return None
    record["intent"] = intent or DEFAULT_INTENT
    record["track"] = list(track or [])
    cur = stage if stage is not None else record.get("stage", "")
    if cur and cur in record["track"]:
        record["stage"] = cur
    elif record["track"]:
        record["stage"] = record["track"][0]
    else:
        record["stage"] = ""
    mx = record.get("maxStage", "")
    if not (mx and mx in record["track"]) or \
            _stage_index(record["stage"], record["track"]) > _stage_index(mx, record["track"]):
        record["maxStage"] = record["stage"]
    record["updated_at"] = _now_ms()
    _write(record)
    return record


def usage_public(record: dict) -> dict:
    """The display-facing view of a record's usage: totals + the per-(stage ×
    model) breakdown, with the internal idempotency ledger stripped."""
    usage = (record or {}).get("usage") or _empty_usage()
    return {
        "totals": dict(usage.get("totals") or {}),
        "by_stage_model": dict(usage.get("by_stage_model") or {}),
        "updated_at": usage.get("updated_at", 0),
    }


def add_usage(conversation_id: str, stage: str, model: str, delta: dict,
              idempotency_key: Optional[str] = None) -> Optional[dict]:
    """Add one turn's usage delta into the (stage × model) breakdown and the
    roll-up totals — at most once per `idempotency_key`. Returns the updated
    usage view (or None if the strategy does not exist)."""
    record = _read(conversation_id)
    if record is None:
        return None
    usage = record.get("usage") or _empty_usage()
    keys = usage.setdefault("applied_keys", [])
    if idempotency_key and idempotency_key in keys:
        return usage_public(record)

    stage = stage or "unknown"
    model = model or "unknown"
    cell_key = stage + "|" + model
    cells = usage.setdefault("by_stage_model", {})
    cell = cells.get(cell_key) or {"stage": stage, "model": model}
    totals = usage.setdefault("totals", {f: 0 for f in _USAGE_FIELDS})
    now = _now_ms()
    for f in _USAGE_FIELDS:
        d = int(delta.get(f, 0) or 0)
        cell[f] = int(cell.get(f, 0)) + d
        totals[f] = int(totals.get(f, 0)) + d
    cell["updated_at"] = now
    cells[cell_key] = cell
    usage["updated_at"] = now
    if idempotency_key:
        keys.append(idempotency_key)
        if len(keys) > _MAX_USAGE_KEYS:
            del keys[: len(keys) - _MAX_USAGE_KEYS]
    record["usage"] = usage
    record["updated_at"] = now
    _write(record)
    return usage_public(record)


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


def delete(conversation_id: str) -> bool:
    """Permanently remove a strategy: its folder and everything in it (meta,
    algo code, per-strategy memory + skills). Returns False if there was no such
    strategy. nvx-accounts stays the authoritative registry and deletes its own
    project record separately."""
    base = _BASE_DIR.resolve()
    try:
        resolved = strategy_dir(conversation_id).resolve()
    except Exception:
        return False
    # safety: never rmtree anything outside the strategies base (guards a
    # malformed/hostile id like ".." even though accounts mints clean ids).
    if resolved == base or base not in resolved.parents:
        logger.warning("Refusing to delete strategy outside base: %s", conversation_id)
        return False
    if not resolved.exists():
        return False
    shutil.rmtree(resolved, ignore_errors=True)
    logger.info("Deleted strategy %s", conversation_id)
    return True


# --- Uploaded files (per-project agent context) -------------------------
# Files the user uploads to a project live in <strategy>/uploads/. That folder
# is inside the strategy folder, so it is durable (survives restart) and
# per-project isolated, and it sits under the chat turn's cwd — so the agent's
# existing Read/Glob/Grep tools read them with no new tool. We never inline a
# file's contents into the prompt: only a small INDEX (name/type/size) is
# injected (build_files_context), and the agent reads bodies on demand.

_UPLOADS_DIRNAME = "uploads"


def uploads_dir(conversation_id: str) -> Path:
    """The directory holding a project's uploaded context files."""
    return strategy_dir(conversation_id) / _UPLOADS_DIRNAME


def safe_upload_name(filename: str) -> Optional[str]:
    """Reduce a client-supplied filename to a safe basename, or None if it is
    not a usable name. Guards against path traversal (`..`, absolute paths,
    separators) — accounts mints clean ids, but uploads come straight from the
    browser, so the name is untrusted."""
    name = os.path.basename((filename or "").strip().replace("\\", "/"))
    if not name or name in (".", "..") or name.startswith(".."):
        return None
    return name[:255]


def _ext_type(name: str) -> str:
    """A short lowercase type token for the index (file extension, no dot)."""
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return ext or "file"


def _file_entry(p: Path) -> dict:
    """The listing/index view of one uploaded file."""
    st = p.stat()
    return {
        "name": p.name,
        "type": _ext_type(p.name),
        "size": st.st_size,
        "modified_at": int(st.st_mtime * 1000),
    }


def list_files(conversation_id: str) -> list:
    """The project's uploaded files, name-sorted. Empty if none/absent."""
    d = uploads_dir(conversation_id)
    if not d.exists():
        return []
    out = []
    for child in sorted(d.iterdir()):
        if child.is_file():
            try:
                out.append(_file_entry(child))
            except OSError as e:
                logger.warning("Skipping unreadable upload %s: %s", child.name, e)
    return out


def save_file(conversation_id: str, filename: str, data: bytes) -> Optional[dict]:
    """Write an uploaded file into the project's uploads/ folder (creating it),
    overwriting a same-named file. Returns the file's listing entry, or None if
    the filename is unusable. The caller ensures the strategy folder exists."""
    name = safe_upload_name(filename)
    if name is None:
        return None
    d = uploads_dir(conversation_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)
    logger.info("Stored upload %s (%d bytes) for strategy %s", name, len(data), conversation_id)
    return _file_entry(p)


def delete_file(conversation_id: str, filename: str) -> bool:
    """Remove one uploaded file. Path-safety-guarded (never escapes uploads/).
    Returns False if the name is unusable or the file does not exist."""
    name = safe_upload_name(filename)
    if name is None:
        return False
    base = uploads_dir(conversation_id).resolve()
    try:
        resolved = (uploads_dir(conversation_id) / name).resolve()
    except Exception:
        return False
    if base not in resolved.parents:
        logger.warning("Refusing to delete upload outside uploads dir: %s", filename)
        return False
    if not resolved.is_file():
        return False
    resolved.unlink()
    logger.info("Deleted upload %s for strategy %s", name, conversation_id)
    return True


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return (f"{int(size)} {u}" if u == "B" else f"{size:.1f} {u}")
        size /= 1024
    return f"{n} B"


def build_files_context(conversation_id: Optional[str]) -> str:
    """Render the always-on UPLOADED FILES block for the system prompt: a small
    index (name / type / size) the agent reads on demand. Empty string when the
    project has no uploads (so no block is added). Never inlines file bodies."""
    if not conversation_id:
        return ""
    files = list_files(conversation_id)
    if not files:
        return ""
    d = uploads_dir(conversation_id)
    lines = "\n".join(
        f"- {f['name']} ({f['type']}, {_human_size(f['size'])})" for f in files
    )
    return f"""UPLOADED FILES:
The user has uploaded files to this project as context. They live in {d}
(inside your working directory). The index below lists what is available — when
a file is relevant to the user's request, Read it (or Glob/Grep it) on demand;
do not assume its contents from the name. For a large file, read ranges or grep
rather than the whole file, to keep context small. Do not inline a whole file
unless the user asks. When the user refers to "the file I uploaded" or "this
CSV/spec/data", look here first.

{lines}"""


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


_MAX_COMMENTARY = 400


def append_commentary(conversation_id: str, text: str, kind: Optional[str] = None) -> dict:
    """Append one activity-log entry and return it (so the caller can also emit
    it live over SSE). Persists only if the strategy exists. The trail is the
    Work panel's auditable side-mirror, so it keeps every level (not just
    milestones); capped to the most recent entries to bound growth."""
    entry = {"text": text, "kind": kind, "at": _now_ms()}
    record = _read(conversation_id)
    if record is not None:
        trail = record.setdefault("commentary", [])
        trail.append(entry)
        if len(trail) > _MAX_COMMENTARY:
            del trail[: len(trail) - _MAX_COMMENTARY]
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
