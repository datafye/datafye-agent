"""Export and import a project (DAT-293/300), and the user-level memory that spans projects.

A project lives in two halves: the record accounts keeps (name, usage, satisfaction, conversation
log) and everything on this box — the app's code, the chat history in `meta.json`, the project's
memory, its uploads and its outputs. This module owns the agent half. Accounts fetches it over HTTP
and bundles it with its own record, so the two halves travel as one file.

Three things are worth knowing before changing anything here.

**Regenerable directories are left out.** A project that has been built once carries a
`node_modules` or a `target` far larger than the work itself, and restoring it proves nothing a
rebuild would not. The exclusions are named in the archive's manifest rather than applied silently,
so whoever restores it can see what was dropped and why.

**An archive is untrusted input.** It may have come from another box, another account, or a text
editor. Every entry is checked before it is written: no absolute paths, no `..`, no symlinks, no
escape from the target directory, and a size ceiling. This is the zip-slip guard, and it is the
reason import builds its own paths rather than handing the archive's names to `extractall`.

**The id is the caller's to choose.** Importing into a different account means the target project
id is decided by accounts, not by whatever was in the archive, so `import_project` takes the id it
must write and rewrites the record's own `id` field to match. Nothing downstream has to know the
project moved.
"""

import io
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

import conversations
import memory
import paths

logger = logging.getLogger(__name__)

# The archive layout, versioned so a future reader can tell what it is holding. Bump the MAJOR part
# for a change that an older reader could not make sense of.
FORMAT_VERSION = 1

# Built artefacts and dependency trees: large, regenerable, and meaningless on another box. Matched
# on the directory NAME at any depth.
EXCLUDED_DIRS = frozenset({
    "node_modules", "target", "__pycache__", ".git", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", ".gradle", ".m2", ".mvn", ".pytest_cache",
    # Datafye gives each project its own Python env (conversations.py scaffolds .venv). It is large,
    # machine-specific and rebuilt on demand, so it is the last thing an export should carry.
    ".mypy_cache", ".ruff_cache", ".tox", ".idea",
})

# Excluded by FILE name. Kept separate from EXCLUDED_DIRS because the walk prunes directories and
# filters files in different places - a name in the wrong set is advertised in the manifest as
# dropped while still being archived.
EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db"})

# Ceilings. An import writes to the box's disk, so it gets a hard limit on both the archive and what
# it expands to; a zip bomb otherwise fills the agent's disk and takes the box down.
MAX_IMPORT_BYTES = int(os.environ.get("DATAFYE_AGENT_MAX_IMPORT_MB", "512")) * 1024 * 1024
MAX_EXPANDED_BYTES = int(os.environ.get("DATAFYE_AGENT_MAX_EXPANDED_MB", "2048")) * 1024 * 1024
# An export builds the archive on this box's disk and accounts holds the result in heap, so a project
# carrying a multi-GB upload must fail here with a clear message rather than take one of them down.
MAX_EXPORT_BYTES = int(os.environ.get("DATAFYE_AGENT_MAX_EXPORT_MB", "1024")) * 1024 * 1024

_MANIFEST_NAME = "agent-manifest.json"
_PROJECT_PREFIX = "project/"
_MEMORY_PREFIX = "memory/"


class ArchiveError(Exception):
    """A bad archive, or a target that cannot be written. Carries a message meant for an operator."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iter_files(root: Path):
    """Every file under `root` worth archiving, as (absolute path, path relative to root).

    Directories in EXCLUDED_DIRS are pruned whole. Symlinks are skipped rather than followed: a link
    pointing outside the project would otherwise copy content the project does not own, and a link
    cycle would never terminate.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS
                             and not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            if name in EXCLUDED_NAMES:
                continue                      # a FILE on the exclusion list, not a directory
            p = Path(dirpath) / name
            if p.is_symlink() or not p.is_file():
                continue
            yield p, p.relative_to(root)


def _write_zip(out_path: Path, root: Path, prefix: str, manifest: dict) -> dict:
    """Zip everything under `root` into `out_path` beneath `prefix`, manifest first.

    The tree is measured BEFORE anything is written, so the manifest can carry the real counts and be
    written once. The previous version wrote a placeholder and then rebuilt the whole archive to
    correct it - which re-read and re-compressed every member, doubling the work and the disk for an
    archive of any size, while its comment claimed it touched "a small file only".
    """
    entries = list(_iter_files(root))
    raw_bytes = 0
    for absolute, _ in entries:
        try:
            raw_bytes += absolute.stat().st_size
        except OSError:
            pass
    if raw_bytes > MAX_EXPORT_BYTES:
        raise ArchiveError(
            f"this project holds {raw_bytes // (1024 * 1024)} MB, more than the "
            f"{MAX_EXPORT_BYTES // (1024 * 1024)} MB an export may carry")

    manifest = dict(manifest)
    manifest["files"] = len(entries)
    manifest["raw_bytes"] = raw_bytes
    written = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
        for absolute, relative in entries:
            try:
                z.write(absolute, prefix + relative.as_posix())
                written += 1
            except OSError as e:          # unreadable file: skip it, do not fail the whole export
                logger.warning("Skipping %s during export: %s", absolute, e)
    if written != manifest["files"]:
        # the manifest must not claim files the archive does not hold
        manifest["files"] = written
        manifest["note"] = "some files could not be read and were skipped; see the agent log"
    return manifest


def export_project(conversation_id: str, out_path: Path) -> dict:
    """Write the agent half of one project to `out_path`. Returns the manifest.

    Raises ArchiveError when the project does not exist on this box — which is a real state, not a
    fault: accounts can hold a project record for a project the user never chatted to.
    """
    root = conversations.project_dir(conversation_id)
    if not root.is_dir():
        raise ArchiveError(f"no project '{conversation_id}' on this agent")
    record = conversations.get(conversation_id) or {}
    manifest = {
        "format_version": FORMAT_VERSION,
        "kind": "datafye-project",
        "project_id": conversation_id,
        "project_name": record.get("name"),
        "exported_at": _now_ms(),
        "excluded_dirs": sorted(EXCLUDED_DIRS),
        "excluded_files": sorted(EXCLUDED_NAMES),
    }
    return _write_zip(out_path, root, _PROJECT_PREFIX, manifest)


def export_user_memory(out_path: Path) -> dict:
    """Write the user-level memory that spans projects.

    Two things make this up, and they are NOT in the same directory: the memory dir
    (`<state>/memory/`) and the user's own `CLAUDE.md`, which sits beside it at `<state>/CLAUDE.md`.
    Both are archived relative to the state dir under a single `user/` prefix, so an import can put
    each back where it belongs. Archiving the memory dir alone would restore `CLAUDE.md` one level
    too deep, where nothing reads it.

    Per-project memory travels inside each project's own archive; this is only the cross-project
    half, which is what would otherwise be lost when a user's projects move to another box.

    ⚠️ FLEET memory is deliberately NOT here. It is read-only, ships with the build, and is replaced
    wholesale on upgrade, so exporting it would carry a copy that the next upgrade silently diverges
    from - and importing it would write into a directory the agent does not own.
    """
    memory.ensure_user_memory()
    state_root = Path(memory.USER_DIR).parent
    manifest = {
        "format_version": FORMAT_VERSION,
        "kind": "datafye-user-memory",
        "exported_at": _now_ms(),
        "excluded_dirs": sorted(EXCLUDED_DIRS),
        "excluded_files": sorted(EXCLUDED_NAMES),
    }
    # Measured first, so the manifest is written once with the real counts (see _write_zip).
    entries = []
    memory_dir = Path(memory.USER_DIR)
    if memory_dir.is_dir():
        entries.extend(absolute for absolute, _ in _iter_files(memory_dir))
    user_claude = Path(memory.USER_CLAUDE_MD)
    if user_claude.is_file():
        entries.append(user_claude)
    raw_bytes = 0
    for absolute in entries:
        try:
            raw_bytes += absolute.stat().st_size
        except OSError:
            pass
    manifest["files"] = len(entries)
    manifest["raw_bytes"] = raw_bytes

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
        for absolute in entries:
            z.write(absolute, _MEMORY_PREFIX + absolute.relative_to(state_root).as_posix())
    return manifest


def _safe_target(base: Path, name: str) -> Optional[Path]:
    """Resolve an archive entry to a path inside `base`, or None if it tries to escape.

    Rejects absolute paths, drive letters, `..` segments and anything that resolves outside `base`.
    The final resolve() check is what catches the cases the string checks miss.
    """
    if not name or name.endswith("/"):
        return None
    pure = Path(name)
    if pure.is_absolute() or (len(name) > 1 and name[1] == ":"):
        return None
    if any(part in ("..", "") for part in pure.parts):
        return None
    target = (base / pure).resolve()
    if target != base.resolve() and base.resolve() not in target.parents:
        return None
    return target


def _extract(archive_bytes: bytes, prefix: str, dest: Path) -> int:
    """Extract entries under `prefix` into `dest`, refusing anything that escapes it."""
    written = 0
    expanded = 0
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.startswith(prefix):
                continue
            relative = info.filename[len(prefix):]
            target = _safe_target(dest, relative)
            if target is None:
                raise ArchiveError(f"refusing unsafe archive entry '{info.filename}'")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise ArchiveError(
                    f"archive expands to more than {MAX_EXPANDED_BYTES // (1024 * 1024)} MB")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            written += 1
    return written


def read_manifest(archive_bytes: bytes) -> dict:
    """The archive's own manifest, or {} when it has none (an archive from elsewhere)."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
            return json.loads(z.read(_MANIFEST_NAME))
    except (KeyError, ValueError, zipfile.BadZipFile):
        return {}


def import_project(conversation_id: str, archive_bytes: bytes, overwrite: bool = False) -> dict:
    """Write the agent half of a project into `conversation_id` on this box.

    The id is the caller's choice, not the archive's: importing into another account means accounts
    picks the id, so the record's own `id` is rewritten to match. Refuses an existing project unless
    `overwrite`, and writes to a staging directory first so a failure leaves nothing half-written.
    """
    if len(archive_bytes) > MAX_IMPORT_BYTES:
        raise ArchiveError(f"archive is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
    if not conversation_id or "/" in conversation_id or conversation_id in (".", ".."):
        raise ArchiveError(f"invalid project id '{conversation_id}'")

    dest = conversations.project_dir(conversation_id)
    if dest.exists() and not overwrite:
        raise ArchiveError(f"project '{conversation_id}' already exists on this agent")

    manifest = read_manifest(archive_bytes)
    staging = dest.parent / f".import-{conversation_id}.tmp"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        files = _extract(archive_bytes, _PROJECT_PREFIX, staging)
        if files == 0:
            raise ArchiveError("archive holds no project content")
        # Without meta.json the folder is INVISIBLE (list_conversations skips folders that have no
        # record), so a 200 here would report success for a project the user can never see - and with
        # overwrite=True the existing project's name, history and usage have already been deleted to
        # make room for it. Refuse before anything is swapped in.
        if not (staging / "meta.json").is_file():
            raise ArchiveError("archive has no project/meta.json, so it is not a project export")
        _retarget_record(staging / "meta.json", conversation_id)
        if dest.exists():
            shutil.rmtree(dest)
        staging.replace(dest)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    logger.info("Imported project %s (%d files) from archive %s",
                conversation_id, files, manifest.get("project_id", "<unknown>"))
    return {
        "project_id": conversation_id,
        "files": files,
        "source_project_id": manifest.get("project_id"),
        "source_format_version": manifest.get("format_version"),
    }


def _retarget_record(meta_path: Path, conversation_id: str) -> None:
    """Point an imported record at the id it now lives under.

    A project imported into another account keeps its history and its name, but its id is the target
    id. Leaving the archive's id in `meta.json` would make the record disagree with its own folder,
    and every lookup goes through the folder.
    """
    if not meta_path.is_file():
        return
    try:
        record = json.loads(meta_path.read_text())
    except ValueError as e:
        raise ArchiveError(f"archive's meta.json is not readable: {e}")
    record["id"] = conversation_id
    record["imported_at"] = _now_ms()
    meta_path.write_text(json.dumps(record, indent=2))


def import_user_memory(archive_bytes: bytes) -> dict:
    """Merge the cross-project memory from an archive into this box.

    ⚠️ Only TWO destinations are accepted: the memory directory and the user's own `CLAUDE.md`.
    Extracting relative to the state root instead - which is where these entries are archived from -
    would let an archive write anywhere under it, and the state root holds the encrypted credential
    store, every other project, and `plugins/user/skills`, where a planted file is EXECUTABLE by the
    agent. Staying inside the base directory is not a sufficient check when the base is the state
    root; the destination has to be named.

    Merge, not replace: the target box may already carry memory for this user, and a restore that
    silently dropped it would lose what the box learned since the export. Staged first, so an archive
    that is refused half way through has not already written part of itself.
    """
    if len(archive_bytes) > MAX_IMPORT_BYTES:
        raise ArchiveError(f"archive is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
    memory.ensure_user_memory()
    state_root = Path(memory.USER_DIR).parent
    memory_rel = Path(memory.USER_DIR).name                      # "memory"
    claude_rel = Path(memory.USER_CLAUDE_MD).name                # "CLAUDE.md"

    staging = Path(tempfile.mkdtemp(prefix=".import-memory-", dir=str(state_root)))
    written = 0
    expanded = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as z:
            for info in z.infolist():
                if info.is_dir() or not info.filename.startswith(_MEMORY_PREFIX):
                    continue
                relative = info.filename[len(_MEMORY_PREFIX):]
                parts = Path(relative).parts
                # the allow-list: <memory dir>/** and the user CLAUDE.md, nothing else
                if not parts or (parts[0] != memory_rel and relative != claude_rel):
                    raise ArchiveError(
                        f"refusing '{info.filename}': a memory archive may only carry "
                        f"{memory_rel}/ and {claude_rel}")
                target = _safe_target(staging, relative)
                if target is None:
                    raise ArchiveError(f"refusing unsafe archive entry '{info.filename}'")
                expanded += info.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise ArchiveError(
                        f"archive expands to more than {MAX_EXPANDED_BYTES // (1024 * 1024)} MB")
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                written += 1
        # Only now that the whole archive has been accepted, merge it into the live tree.
        for absolute, relative in _iter_files(staging):
            final = state_root / relative
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(absolute), str(final))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"files": written}
