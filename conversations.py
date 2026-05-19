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
Per-user conversation store for the Datafye Agent.

A "conversation" is the agent workspace's top-level entity — what the
frontend calls a "project". Each one owns:
  - a human name (deduced from the first message),
  - the message history (user + assistant turns),
  - a commentary log — the audit trail of background activity,
  - the Claude Agent SDK session id, so chat turns resume the right SDK
    session across agent restarts.

Storage: one JSON file per conversation under
~/.datafye/agent/conversations/<id>.json. Plain JSON, not encrypted —
unlike credentials.bin: conversation content is not a secret key, and the
Claude Agent SDK already writes its own session transcripts to disk
unencrypted, so encrypting an index of them would buy nothing. Files are
mode 0600; writes go via a temp file + atomic rename so a crash mid-write
cannot truncate an existing file.

The agent serves exactly one user, so there is no per-user namespacing
and no concurrency control beyond the atomic rename.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_BASE_DIR = Path(
    os.environ.get(
        "DATAFYE_AGENT_CONVERSATIONS_DIR",
        os.path.expanduser("~/.datafye/agent/conversations"),
    )
)

# Words dropped when deducing a name from the first message.
_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "and", "or", "with", "on", "in",
    "my", "i", "want", "build", "create", "make", "using", "use", "that",
    "when", "from", "do", "let", "lets", "can", "you", "help", "me", "is",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _path(conversation_id: str) -> Path:
    return _BASE_DIR / f"{conversation_id}.json"


def deduce_name(first_message: str) -> str:
    """Derive a short kebab-case name from the user's first message."""
    words = [
        w for w in re.sub(r"[^a-z0-9\s-]", " ", (first_message or "").lower()).split()
        if w and w not in _STOPWORDS
    ]
    return "-".join(words[:4]) or "new-project"


def _read(conversation_id: str) -> Optional[dict]:
    p = _path(conversation_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        logger.warning("Could not read conversation %s: %s", conversation_id, e)
        return None


def _write(record: dict) -> None:
    _BASE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(record["id"])
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)


def meta(record: dict) -> dict:
    """The listing view of a conversation — no message/commentary bodies."""
    return {
        "id": record["id"],
        "name": record["name"],
        "created_at": record.get("created_at", 0),
        "updated_at": record.get("updated_at", 0),
        "message_count": len(record.get("messages", [])),
    }


def list_conversations() -> list:
    """All conversations, metadata only, most-recently-updated first."""
    if not _BASE_DIR.exists():
        return []
    out = []
    for f in _BASE_DIR.glob("*.json"):
        try:
            out.append(meta(json.loads(f.read_text())))
        except Exception as e:
            logger.warning("Skipping unreadable conversation file %s: %s", f, e)
    out.sort(key=lambda c: c.get("updated_at", 0), reverse=True)
    return out


def create(first_message: str = "") -> dict:
    """Create a new conversation; the name is deduced from the first message."""
    now = _now_ms()
    record = {
        "id": "c-" + uuid.uuid4().hex[:12],
        "name": deduce_name(first_message),
        "created_at": now,
        "updated_at": now,
        "sdk_session_id": None,
        "messages": [],
        "commentary": [],
    }
    _write(record)
    logger.info("Created conversation %s (%s)", record["id"], record["name"])
    return record


def get(conversation_id: str) -> Optional[dict]:
    """Full conversation record, or None if it does not exist."""
    return _read(conversation_id)


def rename(conversation_id: str, name: str) -> Optional[dict]:
    """Rename a conversation. Returns the updated record, or None if absent."""
    record = _read(conversation_id)
    if record is None:
        return None
    record["name"] = (name or "").strip() or record["name"]
    record["updated_at"] = _now_ms()
    _write(record)
    return record


def append_message(conversation_id: str, role: str, content: str) -> None:
    """Append a user/assistant turn. No-op if the conversation does not exist
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
    """Append one activity-log entry and return it (so the caller can also
    emit it live over SSE). Persists only if the conversation exists."""
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
