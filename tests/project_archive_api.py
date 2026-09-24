"""The export/import ENDPOINTS, against a real agent process (DAT-293/300).

`tests/project_archive.py` covers the archive logic. This covers the wiring, which is the part unit
tests cannot see: that the routes exist, that they are behind the same JWT gate as everything else,
that a zip survives the round trip through HTTP, and that the staging copy is cleaned up after the
response rather than before it (which would truncate the download).

It starts the agent with a throwaway state dir and a locally-minted RS256 key pair standing in for
accounts' JWKS, so it needs no network and no Anthropic key.

Run: python3 tests/project_archive_api.py
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    import httpx
    import jwt as pyjwt
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError as e:                                  # pragma: no cover
    print(f"SKIP: needs httpx + pyjwt + cryptography ({e})")
    sys.exit(0)

_TMP = Path(tempfile.mkdtemp(prefix="datafye-archive-api-"))
_USER = "u-test"
_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        _failures.append(name)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- a local stand-in for accounts: an RSA key pair and a JWKS file served off disk -------------
_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _key.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption()).decode()
_numbers = _key.public_key().public_numbers()


def _b64(n, length):
    import base64
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


_jwks = {"keys": [{"kty": "RSA", "kid": "test", "use": "sig", "alg": "RS256",
                   "n": _b64(_numbers.n, 256), "e": _b64(_numbers.e, 3)}]}
_jwks_dir = _TMP / "jwks"
_jwks_dir.mkdir(parents=True)
_jwks_path = _jwks_dir / "datafye-accounts-api" / "v1" / "auth"
_jwks_path.mkdir(parents=True, exist_ok=True)
(_jwks_path / "jwks").write_text(json.dumps(_jwks))
_jwks_port = _free_port()
_jwks_server = subprocess.Popen([sys.executable, "-m", "http.server", str(_jwks_port)],
                                cwd=_jwks_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _token(purpose=None, audience="datafye", sub=_USER, **extra):
    claims = {"sub": sub, "aud": audience, "iss": "nvx-accounts",
              "iat": int(time.time()), "exp": int(time.time()) + 900,
              "mfa_verified": True}
    if purpose:
        claims["purpose"] = purpose
    claims.update(extra)
    return pyjwt.encode(claims, _priv_pem, algorithm="RS256", headers={"kid": "test"})


# ---- start the agent ---------------------------------------------------------------------------
_port = _free_port()
_state = _TMP / "state"
env = dict(os.environ)
env.update({
    "DATAFYE_AGENT_STATE_DIR": str(_state),
    "DATAFYE_AGENT_PROJECTS_DIR": str(_state / "projects"),
    "DATAFYE_AGENT_PORT": str(_port),
    # ⚠️ This agent builds its JWKS URL from ACCOUNTS_URL (there is no JWKS override), so the stub
    # must answer on the real path. It also does NOT verify audience - see the sub test below.
    "DATAFYE_AGENT_ACCOUNTS_URL": f"http://127.0.0.1:{_jwks_port}",
    "DATAFYE_AGENT_ACCOUNTS_ISSUER": "nvx-accounts",
    "DATAFYE_AGENT_WORKSPACE": str(_TMP / "workspace"),
})
(_TMP / "workspace").mkdir(exist_ok=True)
_agent_tmp = _TMP / "agent-tmp"
_agent_tmp.mkdir(exist_ok=True)
env["TMPDIR"] = str(_agent_tmp)          # the agent's staging dirs land HERE, not in the shared /tmp
proc = subprocess.Popen([sys.executable, "main.py"], cwd=_ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
base = f"http://127.0.0.1:{_port}"


def _wait_up(timeout=45):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print("--- agent died at startup ---")
            print(proc.stdout.read()[-3000:])
            return False
        try:
            # ⚠️ 12s, not the 2s a lighter agent needs: this /health resolves the deployment API and
            # the MCP host, and off a box those DNS lookups do not fail fast. A probe that expires
            # BELOW the real response time never succeeds, and reads as "the agent did not start".
            if httpx.get(f"{base}/health", timeout=12).status_code == 200:
                return True
        except Exception:
            time.sleep(0.5)
    return False


try:
    if not _wait_up():
        print("FAILED: agent did not start")
        sys.exit(1)

    print("== the routes are behind the JWT gate ==")
    r = httpx.get(f"{base}/v1/conversations/x/export", timeout=10)
    check("export without a token is refused", r.status_code in (401, 403, 503), r.status_code)
    r = httpx.post(f"{base}/v1/conversations/x/import", content=b"x", timeout=10)
    check("import without a token is refused", r.status_code in (401, 403, 503), r.status_code)

    print("== bootstrap ==")
    r = httpx.post(f"{base}/bootstrap", timeout=20, headers={
        "Authorization": "Bearer " + _token(purpose="agent-bootstrap",
                                            user_id=_USER,
                                            # a REAL Fernet key: the credentials store opens with
                                            # it, and a placeholder fails bootstrap with a 500 that
                                            # then reads as every later route being broken
                                            creds_key=Fernet.generate_key().decode())})
    check("bootstrap accepted", r.status_code in (200, 204), f"{r.status_code} {r.text[:200]}")
    hdr = {"Authorization": "Bearer " + _token()}

    # ⚠️ Unlike the Sutra agent, this one does not verify AUDIENCE (single product, one box). What
    # it enforces is that the token's subject IS the bootstrapped user, so that is what is asserted:
    # a valid, correctly-signed token for somebody else must not reach another user's projects.
    print("== a token for a DIFFERENT user is refused on the new routes ==")
    r = httpx.get(f"{base}/v1/conversations/x/export", timeout=10,
                  headers={"Authorization": "Bearer " + _token(sub="someone-else")})
    check("another user's token refused on export", r.status_code in (401, 403), r.status_code)
    r = httpx.post(f"{base}/v1/conversations/x/import", timeout=10, content=b"x",
                   headers={"Authorization": "Bearer " + _token(sub="someone-else")})
    check("another user's token refused on import", r.status_code in (401, 403), r.status_code)

    print("== export a project that does not exist ==")
    r = httpx.get(f"{base}/v1/conversations/nope/export", headers=hdr, timeout=20)
    check("404 for a project with no folder", r.status_code == 404, r.status_code)

    print("== export a real project ==")
    pdir = _state / "projects" / "p-1"
    (pdir / "src").mkdir(parents=True)
    (pdir / "meta.json").write_text(json.dumps({"id": "p-1", "name": "demo", "messages": [1, 2]}))
    (pdir / "src" / "App.java").write_text("class App {}\n")
    (pdir / "node_modules").mkdir()
    (pdir / "node_modules" / "big.js").write_text("x" * 5000)
    r = httpx.get(f"{base}/v1/conversations/p-1/export", headers=hdr, timeout=60)
    check("export returns 200", r.status_code == 200, r.status_code)
    check("content type is zip", "zip" in r.headers.get("content-type", ""), r.headers)
    blob = r.content
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    check("archive holds the code", "project/src/App.java" in names, names)
    check("archive excludes node_modules", not any("node_modules" in n for n in names))
    check("archive is not truncated by cleanup", zipfile.ZipFile(io.BytesIO(blob)).testzip() is None)

    print("== the staging copy is cleaned up afterwards ==")
    # The agent runs with its own TMPDIR, so this cannot be polluted by (or blamed on) anything else
    # on the machine. The cleanup runs as a starlette BackgroundTask AFTER the body is flushed, so it
    # is genuinely asynchronous - poll briefly rather than assert on a race.
    leftovers = list(_agent_tmp.glob("datafye-export-*"))
    for _ in range(20):
        if not leftovers:
            break
        time.sleep(0.25)
        leftovers = list(_agent_tmp.glob("datafye-export-*"))
    check("no export staging dirs left behind", not leftovers, leftovers)

    print("== import into a different id ==")
    r = httpx.post(f"{base}/v1/conversations/p-2/import", headers=hdr, content=blob, timeout=60)
    check("import returns 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("import reports the source project", r.json().get("source_project_id") == "p-1", r.text)
    moved = json.loads((_state / "projects" / "p-2" / "meta.json").read_text())
    check("imported record carries the NEW id", moved["id"] == "p-2", moved.get("id"))
    check("imported project keeps its history", moved["messages"] == [1, 2])

    print("== import refuses to clobber, unless told to ==")
    r = httpx.post(f"{base}/v1/conversations/p-2/import", headers=hdr, content=blob, timeout=60)
    check("400 on an existing project", r.status_code == 400, r.status_code)
    r = httpx.post(f"{base}/v1/conversations/p-2/import?overwrite=true", headers=hdr,
                   content=blob, timeout=60)
    check("overwrite=true succeeds", r.status_code == 200, r.status_code)

    print("== a hostile archive is refused over HTTP too ==")
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("agent-manifest.json", json.dumps({"format_version": 1}))
        z.writestr("project/../../../pwned.txt", "no\n")
    r = httpx.post(f"{base}/v1/conversations/p-evil/import", headers=hdr,
                   content=evil.getvalue(), timeout=30)
    check("400 for an escaping entry", r.status_code == 400, r.status_code)
    check("the escape did not land", not (_state / "pwned.txt").exists()
          and not (_TMP / "pwned.txt").exists())

    print("== user memory round trip over HTTP ==")
    r = httpx.get(f"{base}/v1/memory/export", headers=hdr, timeout=60)
    check("memory export returns 200", r.status_code == 200, r.status_code)
    mem_blob = r.content
    check("memory archive is a valid zip",
          zipfile.ZipFile(io.BytesIO(mem_blob)).testzip() is None)
    r = httpx.post(f"{base}/v1/memory/import", headers=hdr, content=mem_blob, timeout=60)
    check("memory import returns 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    _jwks_server.terminate()
    shutil.rmtree(_TMP, ignore_errors=True)

print()
if _failures:
    print(f"FAILED: {len(_failures)} -> {_failures}")
    sys.exit(1)
print("all project_archive_api tests passed")
