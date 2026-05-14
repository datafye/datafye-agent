"""
Encrypted on-disk credentials store for the Datafye Agent.

The agent's credentials (data-provider API keys, ConnectTrade user creds,
GitHub token, etc.) used to be split between process env vars (read once
at startup) and an unencrypted broker_user.json file. This module unifies
both into a single binary blob at ~/.datafye/agent/credentials.bin that
survives restarts and is encrypted with a per-instance key.

Design notes:
- Format: msgpack (binary, not human-glanceable on `cat`).
- Encryption: cryptography.fernet (AES-128-CBC + HMAC-SHA256). Single
  key derived from sha256("datafye-agent-creds-v1::" + instance_id), so
  the key is reconstructable on every agent restart without persisting
  it to disk. Defends against casual filesystem inspection and against
  leaked EBS snapshots (snapshot doesn't carry the instance ID).
- File mode 0600 on creation.
- Dict subclass: writes auto-persist. Code that does `creds["foo"] = "bar"`
  just works; no separate save() call needed.
- Generation: a deterministic hash of the contents, exposed for the
  accounts service's poll-loop to detect cache loss / changes.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

import msgpack
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

KEY_DERIVATION_SALT = b"datafye-agent-creds-v1::"

DEFAULT_PATH = Path(
    os.environ.get(
        "DATAFYE_AGENT_CREDENTIALS_FILE",
        os.path.expanduser("~/.datafye/agent/credentials.bin"),
    )
)

# Path of the pre-existing ConnectTrade user-cred file we migrate FROM on first
# load (one-shot, then deleted). Kept as a separate constant so the migration
# is easy to remove later.
LEGACY_BROKER_FILE = Path(
    os.environ.get(
        "DATAFYE_AGENT_BROKER_STATE_FILE",
        os.path.expanduser("~/.datafye/agent/broker_user.json"),
    )
)


def _derive_key(instance_id: str) -> bytes:
    raw = hashlib.sha256(KEY_DERIVATION_SALT + instance_id.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw)


class CredentialsStore(dict):
    """
    Auto-persisting credentials dictionary.

    Behaves exactly like a dict to consumers (main.py's `credentials` global,
    broker.py's shared handle); writes transparently encrypt and flush to
    disk. Concurrent writers aren't expected — the agent serves one user.
    """

    def __init__(self, path: Path, instance_id: str, initial: dict | None = None):
        super().__init__()
        self._path = path
        self._fernet = Fernet(_derive_key(instance_id))
        if initial:
            super().update(initial)
            self._save()

    # ---- dict-protocol overrides that trigger persistence ---------------

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self._save()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self._save()

    def update(self, *args, **kwargs) -> None:  # type: ignore[override]
        super().update(*args, **kwargs)
        self._save()

    def pop(self, key: str, *args):
        result = super().pop(key, *args)
        self._save()
        return result

    def clear(self) -> None:
        super().clear()
        self._save()

    # ---- store-specific API --------------------------------------------

    def generation(self) -> str:
        """
        A short stable hash of the current contents. Two stores with the
        same contents will have the same generation regardless of when
        they were built. Accounts polls this via /health to detect cache
        loss and re-push credentials if needed.
        """
        sorted_pairs = sorted((k, v) for k, v in self.items() if v not in (None, ""))
        canonical = msgpack.packb(sorted_pairs, use_bin_type=True)
        return hashlib.sha256(canonical).hexdigest()[:16]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._fernet.encrypt(msgpack.packb(dict(self), use_bin_type=True))
        # Write to a temp file then rename so a crash mid-write doesn't truncate
        # the existing file.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_bytes(blob)
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self._path)


def load(instance_id: str, path: Path = DEFAULT_PATH, env_seed: dict | None = None) -> CredentialsStore:
    """
    Load the credentials store. If the file exists, decrypt + deserialize it
    into the store. Otherwise create an empty store seeded from `env_seed`
    (the legacy env-var-driven credentials from main.py, used for the first
    run before any /v1/credentials/update pushes have happened).

    On first load, also migrates the legacy unencrypted broker_user.json
    file in place and deletes it.
    """
    if path.exists():
        store = _decrypt(path, instance_id)
        logger.info(
            "Loaded credentials from %s (%d entries, generation=%s)",
            path, len(store), store.generation(),
        )
        return store

    # First run on this instance. Seed from env vars, migrate any legacy state.
    initial: dict = {}
    if env_seed:
        for k, v in env_seed.items():
            if v:
                initial[k] = v

    legacy = _read_legacy_broker_file()
    if legacy:
        initial.update(legacy)
        try:
            LEGACY_BROKER_FILE.unlink()
            logger.info(
                "Migrated %d field(s) from legacy %s into encrypted store",
                len(legacy), LEGACY_BROKER_FILE,
            )
        except OSError:
            logger.warning("Migrated legacy broker file but could not delete %s", LEGACY_BROKER_FILE)

    store = CredentialsStore(path=path, instance_id=instance_id, initial=initial)
    logger.info(
        "Created new credentials store at %s (%d entries seeded, generation=%s)",
        path, len(store), store.generation(),
    )
    return store


def _decrypt(path: Path, instance_id: str) -> CredentialsStore:
    blob = path.read_bytes()
    fernet = Fernet(_derive_key(instance_id))
    try:
        plaintext = fernet.decrypt(blob)
    except InvalidToken as e:
        raise RuntimeError(
            f"Credentials file {path} cannot be decrypted with this instance's "
            f"key. The file was likely written on a different instance. "
            f"Delete it (you'll lose any persisted credentials) or recover the "
            f"correct instance ID."
        ) from e
    contents = msgpack.unpackb(plaintext, raw=False)
    if not isinstance(contents, dict):
        raise RuntimeError(f"Credentials file {path} contained {type(contents).__name__}, expected dict")
    return CredentialsStore(path=path, instance_id=instance_id, initial=contents)


def _read_legacy_broker_file() -> dict:
    """Read the old plain-JSON broker_user.json if present. Returns {} on absence."""
    if not LEGACY_BROKER_FILE.exists():
        return {}
    try:
        import json
        payload = json.loads(LEGACY_BROKER_FILE.read_text())
        out = {}
        if payload.get("user_id"):
            out["connecttrade_user_id"] = payload["user_id"]
        if payload.get("user_secret"):
            out["connecttrade_user_secret"] = payload["user_secret"]
        return out
    except Exception as e:
        logger.warning("Could not read legacy broker file %s: %s", LEGACY_BROKER_FILE, e)
        return {}
