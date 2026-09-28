"""Export/import of ONE project (DAT-293/300).

Export/import of a whole user - every project plus the cross-project memory - was removed in
DAT-325, so there is nothing here about user-level memory. A project's own memory travels inside
its archive and is covered by the round-trip tests.

The interesting cases are not the happy path. An archive is untrusted input, so most of what is
asserted here is what import REFUSES: an entry that escapes the project directory, an id that is a
path, an archive that expands past the ceiling. The round-trip tests exist to prove the escape
guards did not also break the ordinary case.

Run: python3 tests/project_archive.py    (no pytest dependency, like the other suites here)
"""

import ast
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="datafye-archive-test-"))
os.environ["DATAFYE_AGENT_STATE_DIR"] = str(_TMP / "state")
os.environ["DATAFYE_AGENT_PROJECTS_DIR"] = str(_TMP / "state" / "projects")

import conversations          # noqa: E402  (must follow the env setup above)
import memory                 # noqa: E402
import project_archive as pa  # noqa: E402

_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        _failures.append(name)


def _make_project(pid: str, name: str = "demo") -> Path:
    d = conversations.project_dir(pid)
    (d / "src").mkdir(parents=True, exist_ok=True)
    (d / "memory").mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(
        {"id": pid, "name": name, "messages": [{"role": "user", "content": "hello"}]}))
    (d / "src" / "App.java").write_text("class App {}\n")
    (d / "CLAUDE.md").write_text("project memory\n")
    (d / "memory" / "MEMORY.md").write_text("- a note\n")
    # a regenerable tree that must NOT be carried
    (d / "node_modules" / "left-pad").mkdir(parents=True, exist_ok=True)
    (d / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1\n")
    return d


print("== export ==")
_make_project("p-src")
out = _TMP / "p-src.zip"
manifest = pa.export_project("p-src", out)
names = zipfile.ZipFile(out).namelist()
check("archive carries the app code", "project/src/App.java" in names)
check("archive carries the chat history", "project/meta.json" in names)
check("archive carries the project memory", "project/memory/MEMORY.md" in names)
check("node_modules is excluded", not any("node_modules" in n for n in names), names)
check("manifest is versioned", manifest["format_version"] == pa.FORMAT_VERSION)
check("manifest names the exclusions", "node_modules" in manifest["excluded_dirs"])
check("manifest counts the files", manifest["files"] == 4, manifest.get("files"))
check("manifest is readable back", pa.read_manifest(out.read_bytes())["project_id"] == "p-src")

print("== EXPORT refuses a project id that is a path (the hole import already guarded) ==")
# ⚠️ This is the finding that mattered most in review. uvicorn percent-decodes the URL path before
# Starlette routes it, so `GET /v1/conversations/%2e%2e/export` arrived here with id "..".
# project_dir("..") is the agent STATE ROOT, is_dir() is true, and the export returned a zip of
# credentials.bin, broker_user.json, the user's memory, plugins/user/skills and every other
# project. Reproduced at 7 entries before the guard existed. Import was guarded from the start;
# export was not, and every traversal test below this line was an IMPORT test.
# Named as a decoy rather than `credentials.bin`: a later test asserts that the memory-import
# allow-list never CREATES credentials.bin, and planting one here would satisfy that by accident.
Path(_TMP, "state", "credstore-decoy.bin").write_bytes(b"ENCRYPTED-KEY-MATERIAL")
(Path(_TMP, "state", "plugins", "user", "skills")).mkdir(parents=True, exist_ok=True)
Path(_TMP, "state", "plugins", "user", "skills", "mine.md").write_text("executable by the agent\n")
for bad in ("..", ".", "../..", "a/b", "p-ok/../..", "", "proj-../x", "%2e%2e"):
    try:
        pa.export_project(bad, _TMP / "leak.zip")
        check(f"export refuses id {bad!r}", False, "IT EXPORTED - this is a credential leak")
    except pa.ArchiveError as e:
        check(f"export refuses id {bad!r}", "invalid project id" in str(e), str(e))
check("no archive was produced by any of those", not (_TMP / "leak.zip").exists())

print("== a legitimate id still exports (the guard must not be too tight) ==")
_make_project("proj-abc123")
check("a minted-shape id is accepted",
      pa.export_project("proj-abc123", _TMP / "ok.zip")["project_id"] == "proj-abc123")

print("== export of a project this agent does not have ==")
try:
    pa.export_project("never-chatted", _TMP / "x.zip")
    check("refuses a missing project", False)
except pa.ArchiveError as e:
    check("refuses a missing project", "no project" in str(e))

print("== round trip into a DIFFERENT id (import into another account) ==")
blob = out.read_bytes()
result = pa.import_project("p-dest", blob)
dest = conversations.project_dir("p-dest")
check("code restored", (dest / "src" / "App.java").read_text() == "class App {}\n")
check("history restored", json.loads((dest / "meta.json").read_text())["messages"][0]["content"] == "hello")
check("record id retargeted to the new id",
      json.loads((dest / "meta.json").read_text())["id"] == "p-dest")
check("name survives the move", json.loads((dest / "meta.json").read_text())["name"] == "demo")
check("result names the source project", result["source_project_id"] == "p-src")

print("== import refuses to clobber ==")
try:
    pa.import_project("p-dest", blob)
    check("refuses an existing project", False)
except pa.ArchiveError as e:
    check("refuses an existing project", "already exists" in str(e))
before = json.loads((dest / "meta.json").read_text())["id"]
pa.import_project("p-dest", blob, overwrite=True)
check("overwrite=True is allowed explicitly",
      json.loads((dest / "meta.json").read_text())["id"] == before)

print("== zip-slip: entries that try to escape ==")


def _evil(entry_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(pa._MANIFEST_NAME, json.dumps({"format_version": 1}))
        z.writestr(entry_name, "pwned\n")
    return buf.getvalue()


# Each evil archive gets its OWN target id. Sharing one id hides the real result: the first import
# creates the project, and every later one is then refused for "already exists" rather than for being
# unsafe — which reads as a pass while proving nothing. A mutation run (guards deleted) is what
# surfaced this; two of the three cases had never actually been exercised.
_evil_cases = [
    ("project/../../../../etc/passwd", "escapes several levels up"),
    ("project/../escaped.txt", "escapes one level, into the projects dir"),
    ("project//../../escaped2.txt", "escapes via a doubled separator"),
]
for _i, (evil_name, _why) in enumerate(_evil_cases):
    pid = f"p-evil{_i}"
    staging = Path(os.environ["DATAFYE_AGENT_PROJECTS_DIR"]) / f".import-{pid}.tmp"
    # where the entry WOULD land with no guard, which is the only place worth checking
    naive_target = Path(os.path.normpath(str(staging / evil_name[len("project/"):])))
    # Compare against what was there BEFORE, not against absence: a case that escapes above the
    # test's own temp dir can land on a path some earlier run already created, and asserting plain
    # non-existence would then fail for a reason that has nothing to do with this import.
    existed_before = naive_target.exists()
    try:
        pa.import_project(pid, _evil(evil_name))
        check(f"refuses {evil_name} ({_why})", False, "import succeeded")
    except pa.ArchiveError as e:
        check(f"refuses {evil_name} ({_why})", "unsafe" in str(e), str(e))
    check(f"nothing written at {naive_target.name}",
          naive_target.exists() == existed_before, naive_target)
    check(f"{pid} was not created", not conversations.project_dir(pid).exists())
# Where an escape would actually LAND: staging sits inside the projects dir, so `project/../x`
# resolves into the projects dir itself and `project/../../x` into the state dir. Asserting on the
# test's own temp root instead would pass even with the guards removed — it did, until a mutation
# run (guards deleted) showed this check staying green while three others went red.
_projects_dir = Path(os.environ["DATAFYE_AGENT_PROJECTS_DIR"])
_state_dir = Path(os.environ["DATAFYE_AGENT_STATE_DIR"])
_escapees = ([p for p in _projects_dir.glob("escaped*.txt")]
             + [p for p in _state_dir.glob("escaped*.txt")]
             + [p for p in _TMP.glob("escaped*.txt")])
check("nothing escaped the projects dir", not _escapees, _escapees)
check("a refused import leaves no staging dir",
      not list(Path(os.environ["DATAFYE_AGENT_PROJECTS_DIR"]).glob(".import-*")))

print("== an absolute path entry ==")
try:
    pa.import_project("p-abs", _evil("project//etc/cron.d/x"))
    check("refuses an absolute-looking entry", not Path("/etc/cron.d/x").exists())
except pa.ArchiveError:
    check("refuses an absolute-looking entry", True)

print("== a project id that is itself a path ==")
for bad_id in ("../../etc", "a/b", "..", ""):
    try:
        pa.import_project(bad_id, blob)
        check(f"refuses id '{bad_id}'", False)
    except pa.ArchiveError as e:
        check(f"refuses id '{bad_id}'", "invalid project id" in str(e))

print("== ceilings ==")
_saved = pa.MAX_IMPORT_BYTES
pa.MAX_IMPORT_BYTES = 10
try:
    pa.import_project("p-big", blob)
    check("refuses an oversized archive", False)
except pa.ArchiveError as e:
    check("refuses an oversized archive", "larger than" in str(e))
finally:
    pa.MAX_IMPORT_BYTES = _saved

_saved_expanded = pa.MAX_EXPANDED_BYTES
pa.MAX_EXPANDED_BYTES = 5
try:
    pa.import_project("p-bomb", blob)
    check("refuses an archive that expands past the ceiling", False)
except pa.ArchiveError as e:
    check("refuses an archive that expands past the ceiling", "expands to more than" in str(e))
finally:
    pa.MAX_EXPANDED_BYTES = _saved_expanded

print("== an archive with no project content ==")
empty = io.BytesIO()
with zipfile.ZipFile(empty, "w") as z:
    z.writestr(pa._MANIFEST_NAME, json.dumps({"format_version": 1}))
try:
    pa.import_project("p-empty", empty.getvalue())
    check("refuses an archive with no content", False)
except pa.ArchiveError as e:
    check("refuses an archive with no content", "no project content" in str(e))

print("== an import with no record is refused, and does not destroy what is there ==")
_make_project("p-keep", name="precious")
no_meta = io.BytesIO()
with zipfile.ZipFile(no_meta, "w") as z:
    z.writestr(pa._MANIFEST_NAME, json.dumps({"format_version": 1}))
    z.writestr("project/src/Thing.java", "class Thing {}\n")
try:
    pa.import_project("p-keep", no_meta.getvalue(), overwrite=True)
    check("refuses an archive with no meta.json", False, "import succeeded")
except pa.ArchiveError as e:
    check("refuses an archive with no meta.json", "meta.json" in str(e), str(e))
check("the project it would have replaced is untouched",
      json.loads((conversations.project_dir("p-keep") / "meta.json").read_text())["name"] == "precious")

print("== excluded FILES are excluded, not just directories ==")
_p = conversations.project_dir("p-src")
(_p / ".DS_Store").write_text("junk\n")
_out2 = _TMP / "p-src-2.zip"
_m2 = pa.export_project("p-src", _out2)
_n2 = zipfile.ZipFile(_out2).namelist()
check(".DS_Store is excluded", not any(".DS_Store" in n for n in _n2), _n2)
check("the manifest names the excluded files", ".DS_Store" in _m2["excluded_files"])

print("== an export larger than the ceiling is refused ==")
_saved_export = pa.MAX_EXPORT_BYTES
pa.MAX_EXPORT_BYTES = 10
try:
    pa.export_project("p-src", _TMP / "too-big.zip")
    check("refuses an oversized export", False)
except pa.ArchiveError as e:
    check("refuses an oversized export", "more than" in str(e), str(e))
finally:
    pa.MAX_EXPORT_BYTES = _saved_export

print("== the manifest INSIDE the archive counts what the archive holds ==")
_make_project("p-manifest")
zp = _TMP / "manifest.zip"
man = pa.export_project("p-manifest", zp)
inner = json.loads(zipfile.ZipFile(zp).read(pa._MANIFEST_NAME))
members = [n for n in zipfile.ZipFile(zp).namelist() if n != pa._MANIFEST_NAME]
check("the archived manifest matches the real member count",
      inner["files"] == len(members), (inner["files"], len(members)))
check("and agrees with what the caller was told", inner["files"] == man["files"])

print("== every refusal carries a status somebody CHOSE ==")
# The point of the taxonomy is that a refusal added later reports itself correctly without anyone
# touching a route. That only holds while each raise site picks the class its condition deserves,
# and picking wrong is silent: the base class is a perfectly valid 400. So this walks the module.
_src = ast.parse(Path(pa.__file__).read_text())


def _raise_sites():
    for node in ast.walk(_src):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        name = getattr(node.exc.func, "id", "")
        if not name.startswith("Archive"):
            continue
        arg = node.exc.args[0] if node.exc.args else None
        if isinstance(arg, ast.Constant):
            text = str(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            text = "".join(v.value for v in arg.values if isinstance(v, ast.Constant))
        else:
            text = ""
        yield node.lineno, name, " ".join(text.split())


sites = list(_raise_sites())
check("the module still raises something", len(sites) >= 10, len(sites))

# Statuses are a property of the class, so a route never decides one.
for cls, want in ((pa.ArchiveError, 400), (pa.ArchiveNotFound, 404),
                  (pa.ArchiveConflict, 409), (pa.ArchiveTooLarge, 413)):
    check(f"{cls.__name__} is {want}", cls.status == want, cls.status)
check("every subclass is catchable as ArchiveError",
      all(issubclass(c, pa.ArchiveError)
          for c in (pa.ArchiveNotFound, pa.ArchiveConflict, pa.ArchiveTooLarge)))

# ⚠️ THE REGRESSION GUARD. A condition that is plainly a size, a collision or an absence must not
# be raised as the bare base class - that is exactly how "too large" came to be reported as "does
# not exist". Keyed on the wording of the message, because that is what a new raise site writes
# first and it is the only signal available before anyone has thought about the status.
RULES = (
    (("larger than", "expands to more than", "more than the"), pa.ArchiveTooLarge, "a size ceiling"),
    (("already exists",), pa.ArchiveConflict, "a collision"),
    (("no project '",), pa.ArchiveNotFound, "an absence"),
)
for lineno, name, text in sites:
    for needles, want, what in RULES:
        if any(n in text for n in needles):
            check(f"line {lineno} ({what}) is {want.__name__}", name == want.__name__,
                  f"raised {name}: {text[:60]}")

# And the other direction: a site NOT covered by a rule is the base class on purpose, not by
# omission. If this fires, either the raise deserves a subclass or the inventory below needs the
# new wording - both are decisions, which is the point.
GENERIC = ("invalid project id", "refusing unsafe archive entry", "archive holds no project content",
           "archive has no project/meta.json", "archive's meta.json is not readable")
for lineno, name, text in sites:
    if name == "ArchiveError":
        check(f"line {lineno} is a deliberate 400", any(g in text for g in GENERIC),
              f"unclassified refusal: {text[:60]}")

print("== and the refusals report those statuses in practice ==")
_make_project("p-status")
try:
    pa.export_project("p-nonexistent-xyz", _TMP / "s.zip")
    check("a missing project raises ArchiveNotFound", False, "it did not raise")
except pa.ArchiveNotFound as e:
    check("a missing project raises ArchiveNotFound", e.status == 404, e.status)
except pa.ArchiveError as e:
    check("a missing project raises ArchiveNotFound", False, f"{type(e).__name__}: {e}")

_saved = pa.MAX_EXPORT_BYTES
pa.MAX_EXPORT_BYTES = 1
try:
    pa.export_project("p-status", _TMP / "s.zip")
    check("an oversized project raises ArchiveTooLarge", False, "it did not raise")
except pa.ArchiveTooLarge as e:
    check("an oversized project raises ArchiveTooLarge", e.status == 413, e.status)
except pa.ArchiveError as e:
    check("an oversized project raises ArchiveTooLarge", False, f"{type(e).__name__}: {e}")
finally:
    pa.MAX_EXPORT_BYTES = _saved

_zip = _TMP / "conflict.zip"
pa.export_project("p-status", _zip)
_blob = _zip.read_bytes()
try:
    pa.import_project("p-status", _blob)
    check("importing onto a live project raises ArchiveConflict", False, "it did not raise")
except pa.ArchiveConflict as e:
    check("importing onto a live project raises ArchiveConflict", e.status == 409, e.status)
except pa.ArchiveError as e:
    check("importing onto a live project raises ArchiveConflict", False, f"{type(e).__name__}: {e}")
check("and overwrite still works", pa.import_project("p-status", _blob, overwrite=True) is not None)

shutil.rmtree(_TMP, ignore_errors=True)
print()
if _failures:
    print(f"FAILED: {len(_failures)} -> {_failures}")
    sys.exit(1)
print("all project_archive tests passed")
