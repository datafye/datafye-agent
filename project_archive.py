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


def _add_member(z: zipfile.ZipFile, absolute: Path, arcname: str, partial: list) -> bool:
    """Add one file to the archive. True when a COMPLETE member landed.

    ⚠️ `ZipFile.write` is not all-or-nothing: a read failure part way has already begun the member,
    and it is committed when the writing handle closes, so catching OSError around it left a
    truncated member in the archive while the counter stayed behind and the manifest under-reported
    what the file holds.

    Round 4 "fixed" that by reading each file whole and calling writestr, which introduced two
    regressions of its own, both measured: a bare ZipInfo carries no `external_attr`, so every
    member came out 0600 and an exported executable unzipped non-executable; and a single large
    file was buffered entirely in the agent process where it used to stream in chunks, which is an
    OOM on these boxes for one file under the total ceiling.

    So: metadata from `ZipInfo.from_file` (mode and mtime preserved), content STREAMED, and a
    member that fails mid-write is named in the manifest's `partial_files` rather than quietly
    miscounted. The archive declares what it could not finish, which is the property the original
    finding was about; pretending a stream can be un-committed is not available.
    """
    try:
        # strict_timestamps=False clamps BOTH ends of what a zip can represent. Clamping only the
        # pre-1980 side left a post-2107 mtime to reach the header writer as a struct.error, which
        # is not an OSError and killed the entire export.
        info = zipfile.ZipInfo.from_file(absolute, arcname, strict_timestamps=False)
    except OSError as e:
        logger.warning("Skipping %s during export: %s", absolute, e)
        return False
    # A ZipInfo carries its OWN compress_type, defaulting to STORED and overriding the one the
    # ZipFile was opened with, so this line is what keeps the archive compressed at all.
    info.compress_type = zipfile.ZIP_DEFLATED
    try:
        src = absolute.open("rb")
    except OSError as e:
        # Never opened, so no member began: this is a clean SKIP, not a partial. Reporting it as
        # partial named a member the archive does not contain, while `note` called it skipped in
        # the same manifest.
        logger.warning("Skipping %s during export: %s", absolute, e)
        return False
    try:
        with src, z.open(info, "w") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        return True
    except Exception as e:
        # Anything from here on has already begun the member and it is committed on close, so the
        # archive holds a short one and must say so. Deliberately broad: a post-2107 timestamp
        # raises struct.error and a member crossing the ZIP64 limit raises RuntimeError, and
        # neither is an OSError - both used to escape and kill the whole export.
        logger.warning("Could not finish archiving %s: %s", absolute, e)
        partial.append(arcname)
        return False


def _require_safe_project_id(conversation_id: str) -> str:
    """Refuse anything that is not a plain project id. Returns the id.

    The id is used to build a filesystem path. Called with `".."` this function archived the agent
    STATE ROOT: `credentials.bin`, `broker_user.json`, the user's memory, `plugins/user/skills` and
    every other project - reproduced at 7 entries before this guard existed. Import guarded its id
    from the start and export did not, which is why both now share one guard rather than each
    carrying its own idea of what is safe.

    ⚠️ On the REACHABILITY, because it was initially overstated: the HTTP route does NOT let this
    through. `/v1/conversations/%2e%2e/export` is normalised before routing and 404s with or without
    this guard (measured both ways). So this is defence in depth for any caller of the function, not
    a patch for a live exploit. It matters because the only thing that made the export safe was a
    property of the web framework that nothing here states or tests.

    The allow-list matches the accounts-side `isSafeProjectId`, deliberately: two components that
    disagree about which ids are legal is a gap that only shows up under attack.
    """
    if not conversation_id or len(conversation_id) > 128:
        raise ArchiveError(f"invalid project id '{conversation_id}'")
    for ch in conversation_id:
        if not (ch.isascii() and (ch.isalnum() or ch in "-_")):
            raise ArchiveError(f"invalid project id '{conversation_id}'")
    return conversation_id


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
    manifest["raw_bytes"] = raw_bytes
    written = 0
    partial = []
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for absolute, relative in entries:
            if _add_member(z, absolute, prefix + relative.as_posix(), partial):
                written += 1
        manifest["files"] = written
        if partial:
            manifest["partial_files"] = partial
        if written != len(entries):
            manifest["note"] = "some files could not be read and were skipped; see the agent log"
        # ⚠️ Written LAST, and that is the whole point. Writing it first meant the count inside the
        # ARCHIVE was the count we intended rather than the count we achieved: a skipped unreadable
        # file corrected only the dict returned to the caller, so anyone restoring from the file saw
        # a manifest claiming members it does not hold. The correction has to land where the reader
        # will look for it.
        z.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
    return manifest


def export_project(conversation_id: str, out_path: Path) -> dict:
    """Write the agent half of one project to `out_path`. Returns the manifest.

    Raises ArchiveError when the project does not exist on this box — which is a real state, not a
    fault: accounts can hold a project record for a project the user never chatted to.
    """
    _require_safe_project_id(conversation_id)
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
    manifest["raw_bytes"] = raw_bytes
    # The same ceiling the project export enforces. This measured raw_bytes and then never looked at
    # it, so a memory tree past the limit was archived anyway; on the accounts side that surfaces as
    # a bare `user_memory: unavailable` with nothing saying why.
    if raw_bytes > MAX_EXPORT_BYTES:
        raise ArchiveError(
            f"this user's memory holds {raw_bytes // (1024 * 1024)} MB, more than the "
            f"{MAX_EXPORT_BYTES // (1024 * 1024)} MB an export may carry")

    written = 0
    partial = []
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for absolute in entries:
            if _add_member(z, absolute,
                           _MEMORY_PREFIX + absolute.relative_to(state_root).as_posix(), partial):
                written += 1
        manifest["files"] = written
        if partial:
            manifest["partial_files"] = partial
        if written != len(entries):
            manifest["note"] = "some files could not be read and were skipped; see the agent log"
        z.writestr(_MANIFEST_NAME, json.dumps(manifest, indent=2))
    return manifest


def _iter_staged(root: Path):
    """Every file under `root`, with NO exclusions.

    Distinct from `_iter_files`, which prunes regenerable directories on the way OUT. On the way in,
    pruning would silently drop a staged file that has already been counted as restored.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in filenames:
            absolute = base / name
            yield absolute, absolute.relative_to(root)


def _has_real_content(path: Path, template: str) -> bool:
    """Whether `path` holds anything beyond the scaffold the agent writes on startup.

    ⚠️ An UNREADABLE file counts as real content. Returning False there would fold "I could not
    look" into "there is nothing worth keeping", and the caller acts on that by overwriting - so a
    live CLAUDE.md that merely failed to decode would be replaced outright, with no copy kept
    beside it. When in doubt the file is precious, because the cost of being wrong is asymmetric.
    """
    if not path.is_file():
        return False
    try:
        body = path.read_text()
    except (OSError, UnicodeDecodeError):
        return True
    return bool(body.strip()) and body.strip() != template.strip()


def _index_has_entries(index: Path) -> bool:
    """Whether a MEMORY.md index carries any POINTER LINE, as opposed to just its header.

    The agent already treats a header-only index as empty when it builds the prompt block, so this
    is the same reading rather than a new one.
    """
    if not index.is_file():
        return False
    try:
        return any(line.lstrip().startswith("- ") for line in index.read_text().splitlines())
    except (OSError, UnicodeDecodeError):
        # Unreadable, so treat it as having entries: that routes the restore through _merge_index,
        # which itself refuses to touch an index it cannot read and keeps the archive's copy as
        # MEMORY.imported.md. Returning False here would bypass the guard this round added and
        # overwrite the live index wholesale, stranding every topic file it pointed at.
        return True


def _index_key(line: str) -> str:
    """The identity of a pointer line: the file it points at, not the prose describing it.

    Deduping on the whole line re-adds an entry whose description was edited after the export, so
    the index grows a second, stale pointer to the same topic file on every single restore.
    """
    start = line.find("](")
    if start < 0:
        return line.strip()
    end = line.find(")", start + 2)
    return line[start + 2:end].strip() if end > start else line.strip()


def _last_section(body: str) -> str:
    """The final `##` heading in a markdown body, or "" when there is none."""
    last = ""
    for line in body.splitlines():
        if line.startswith("## "):
            last = line.strip()
    return last


def _keep_beside(source: Path, preferred: Path) -> Path:
    """Move `source` to `preferred`, or to the next free numbered name if that is taken.

    Every caller is holding something UNRECONCILED - a copy the user still has to look at - so
    overwriting one with another is the loss these paths exist to prevent, merely postponed.
    """
    preferred.parent.mkdir(parents=True, exist_ok=True)
    target = preferred
    n = 2
    while target.exists():
        target = preferred.with_name(f"{preferred.stem}.{n}{preferred.suffix}")
        n += 1
        if n > 100:                       # absurd; stop rather than spin
            break
    shutil.move(str(source), str(target))
    return target


def _merge_index(live: Path, incoming: Path) -> bool:
    """Union the pointer lines of two MEMORY.md indexes, keeping the live file's own order.

    The index is the always-on block injected into every prompt, and it is a list of pointers to
    topic files. A plain overwrite (which is what shutil.move does) drops every line added since the
    export, and each dropped line makes its topic file INVISIBLE rather than merely unlisted - the
    body is only ever read on demand, via the index. So the lines are unioned: everything the live
    file has, plus anything the archive carries that it does not.
    """
    try:
        body = live.read_text()
        new_lines = incoming.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        # The caller must know this did not happen: it used to discard the staged copy anyway and
        # count it as restored, so the archive's index vanished while the import reported success.
        return False
    seen = {_index_key(line) for line in body.splitlines() if line.strip()}
    added = [line for line in new_lines
             if line.lstrip().startswith("- ") and _index_key(line) not in seen]
    if added:
        if body and not body.endswith("\n"):
            body += "\n"
        # ⚠️ Under a heading of their own. Appending bare pointer lines files them under whatever
        # `##` section the live file happens to end with, so an archive's Trading entries arrive
        # under Local - and this block is injected into every prompt, so a wrong heading is a
        # standing false statement rather than untidiness. Carrying the archive's own headings
        # across would mean merging two section structures; saying where these came from is
        # honest, and the user can file them properly.
        # ⚠️ Whether the tail is already that section, not whether the file mentions it anywhere.
        # Checking the whole body suppressed the heading on every import after the first while
        # still appending at EOF, so once the user added a section below it the entries filed under
        # THAT - the exact misfiling this is here to stop, returning on the second import.
        if _last_section(body) != "## Imported":
            body += "\n## Imported\n"
        live.write_text(body + "\n".join(added) + "\n")
    return True


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
    # `Path(".").parts` is EMPTY, so the filter above does not see it and the containment check
    # below is satisfied by base == base. `_extract` would then open a DIRECTORY for writing and
    # raise IsADirectoryError, which is a 500 where the caller means to answer 400.
    if not pure.parts:
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
    _require_safe_project_id(conversation_id)

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
    # ⚠️ The question is whether the live file holds REAL CONTENT, not whether it exists.
    #
    # Capturing existence before ensure_user_memory() was the previous attempt and it was wrong on
    # every running agent: main.py's lifespan already scaffolds both files at STARTUP, long before
    # an import arrives, so the file always existed, the preserve-the-live-file rule always fired,
    # and the user's real notes were always diverted to CLAUDE.imported.md - which nothing reads
    # and export_user_memory does not archive, so a second migration hop lost them outright. The
    # unit test passed only because it unlinked the file first, which is a state no running agent
    # ever presents. Verified by replaying the real sequence: scaffold, then import.
    memory.ensure_user_memory()
    had_claude = _has_real_content(Path(memory.USER_CLAUDE_MD), memory._USER_CLAUDE_TEMPLATE)
    had_index = _index_has_entries(Path(memory.USER_DIR, "MEMORY.md"))
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
        if written == 0:
            # import_project refuses the analogous case. Without this, posting a PROJECT archive to
            # /v1/memory/import reads as a completed restore of nothing.
            raise ArchiveError("archive holds no user-memory content")
        # Only now that the whole archive has been accepted, merge it into the live tree.
        #
        # ⚠️ _iter_files PRUNES excluded dirs and names, so iterating it here could drop a staged
        # file that was already counted in `written` - reported as restored and then deleted with
        # the staging dir. The merge walks everything that was staged.
        merged = 0
        for absolute, relative in _iter_staged(staging):
            final = state_root / relative
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.name == "MEMORY.md" and final.is_file() and had_index:
                if _merge_index(final, absolute):
                    absolute.unlink(missing_ok=True)
                else:
                    # Could not read the live index, so nothing was merged. Keep the archive's copy
                    # rather than dropping it and reporting a restore that did not happen.
                    _keep_beside(absolute, Path(memory.USER_DIR) / "MEMORY.imported.md")
            elif final == Path(memory.USER_CLAUDE_MD) and final.is_file() and had_claude:
                # Prose, so there is no safe line-wise merge. The live file wins and the archive's
                # copy is kept for the user to reconcile, because "merge, not replace" has to be
                # true of the file the prompt reads on every single turn.
                #
                # ⚠️ The comparison must not RAISE. had_claude is deliberately true for a live file
                # we could not read, and read_bytes() on that file then threw straight out of the
                # import: a 500, staging destroyed, and the files already moved this pass left
                # live - a partial restore that failed identically on every retry, which is worse
                # than the overwrite this branch was added to prevent. Unreadable means we cannot
                # compare, so it is treated as a conflict and both copies survive.
                same = False
                try:
                    same = final.read_bytes() == absolute.read_bytes()
                except OSError as e:
                    logger.warning("Could not compare %s with the archive's copy: %s", final, e)
                if same:
                    absolute.unlink(missing_ok=True)
                else:
                    # Inside memory/, NOT beside it at the state root: export_user_memory archives
                    # memory/** plus the user CLAUDE.md and nothing else, so a conflict copy at the
                    # root is invisible to the next export and the notes die on the following hop.
                    _keep_beside(absolute, Path(memory.USER_DIR) / "CLAUDE.imported.md")
            elif ".imported" in final.name and final.is_file():
                # ⚠️ Now that the conflict copy lives in memory/, it is EXPORTED - so a restore
                # carries box A's unreconciled notes into box B and the generic move below would
                # overwrite box B's own, which is the same loss one level removed. Neither is
                # reconciled yet, so neither may be discarded.
                _keep_beside(absolute, final)
            else:
                shutil.move(str(absolute), str(final))
            merged += 1
        written = merged
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"files": written}
