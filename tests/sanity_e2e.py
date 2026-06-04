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
End-to-end SANITY check for the Datafye Agent (manual / integration — NOT CI).

This is a sanity suite, not a full regression suite: it runs ONE meaty flow
against a REAL agent process making REAL model calls, and asserts the major
surfaces work together end to end:

  health -> bootstrap (accounts-signed JWT) -> health
    -> chat: write GLOBAL + PER-STRATEGY memory
    -> chat: author a USER-GLOBAL skill (author-skill -> Skill tool)
    -> GET /v1/skills lists system + the new user skill
    -> chat in a NEW strategy: recall the GLOBAL memory, WITHOUT leaking the
       first strategy's private fact
    -> strategy folders are scaffolded (CLAUDE.md / PROJECT.md / memory / skills)

All agent state is redirected into a throwaway temp dir and a clean temp HOME,
so the run never touches ~/.datafye or the developer's ~/.claude.

PREREQUISITES (the suite SKIPS gracefully, exit 2, if missing):
  - DATAFYE_AGENT_ANTHROPIC_API_KEY in the environment (seeds the agent's key).
  - An accounts JWT signing key JSON (RSA, private). The suite stands in for the
    accounts service: it serves a local JWKS from this key and mints the
    bootstrap + per-request tokens. Default path:
    ~/.datafye/accounts/jwt-keys/jwt-signing-key.json

CONFIG via env (sensible defaults):
  SANITY_ACCOUNTS_KEY   path to the accounts signing-key JSON
  SANITY_PYTHON         python to run the agent (default: <repo>/.venv/bin/python)
  SANITY_AGENT_PORT     default 18799
  SANITY_ACCOUNTS_PORT  default 18790
  SANITY_MODEL          agent model alias (default: sonnet)

RUN:   ./.venv/bin/python tests/sanity_e2e.py
EXIT:  0 = all checks passed | 1 = a check failed | 2 = skipped (missing prereqs)
"""

import json
import os
import sys
import time
import threading
import subprocess
import tempfile
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.environ.get("SANITY_PYTHON", os.path.join(REPO, ".venv", "bin", "python"))
ACCOUNTS_KEY = os.environ.get(
    "SANITY_ACCOUNTS_KEY",
    os.path.expanduser("~/.datafye/accounts/jwt-keys/jwt-signing-key.json"),
)
AGENT_PORT = int(os.environ.get("SANITY_AGENT_PORT", "18799"))
ACCOUNTS_PORT = int(os.environ.get("SANITY_ACCOUNTS_PORT", "18790"))
MODEL = os.environ.get("SANITY_MODEL", "sonnet")

ISSUER = f"http://127.0.0.1:{ACCOUNTS_PORT}"
AGENT = f"http://127.0.0.1:{AGENT_PORT}"
USER = "sanityuser"
STRAT_A = "c-sanity-a"
STRAT_B = "c-sanity-b"


def skip(msg):
    print(f"[sanity] SKIPPED — {msg}")
    sys.exit(2)


# --- Prereq checks ------------------------------------------------------
if not os.environ.get("DATAFYE_AGENT_ANTHROPIC_API_KEY"):
    skip("DATAFYE_AGENT_ANTHROPIC_API_KEY not set (needed to seed the agent's key)")
if not os.path.isfile(ACCOUNTS_KEY):
    skip(f"accounts signing key not found at {ACCOUNTS_KEY} (set SANITY_ACCOUNTS_KEY)")
if not os.path.isfile(PYTHON):
    skip(f"python not found at {PYTHON} (set SANITY_PYTHON)")

try:
    import jwt
    from cryptography.fernet import Fernet
    import httpx
except Exception as e:  # pragma: no cover
    skip(f"missing test dependency: {e}")

JWK = json.load(open(ACCOUNTS_KEY))
KID = JWK["kid"]
PRIV = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(JWK))
PUBJWK = {k: JWK[k] for k in ("kty", "kid", "use", "alg", "n", "e")}

WORKDIR = tempfile.mkdtemp(prefix="datafye-agent-sanity-")
CHECKS = []  # (label, passed)


def check(label, passed):
    CHECKS.append((label, bool(passed)))
    print(f"[sanity] {'PASS' if passed else 'FAIL'}  {label}")


class JwksHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/datafye-accounts-api/v1/auth/jwks":
            body = json.dumps({"keys": [PUBJWK]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def start_jwks():
    HTTPServer.allow_reuse_address = True
    srv = HTTPServer(("127.0.0.1", ACCOUNTS_PORT), JwksHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def mint(claims, ttl=3600):
    now = int(time.time())
    return jwt.encode({**claims, "iss": ISSUER, "iat": now, "exp": now + ttl},
                      PRIV, algorithm="RS256", headers={"kid": KID})


def start_agent():
    env = {
        **os.environ,
        "HOME": os.path.join(WORKDIR, "home"),
        "DATAFYE_AGENT_STATE_DIR": os.path.join(WORKDIR, "state"),
        "DATAFYE_AGENT_WORKSPACE": os.path.join(WORKDIR, "workspace"),
        "DATAFYE_AGENT_DOCS_DIR": os.path.join(WORKDIR, "docs"),
        "DATAFYE_AGENT_SAMPLES_DIR": os.path.join(WORKDIR, "samples"),
        "DATAFYE_AGENT_PORT": str(AGENT_PORT),
        "DATAFYE_AGENT_ACCOUNTS_URL": ISSUER,
        "DATAFYE_AGENT_ACCOUNTS_ISSUER": ISSUER,
        "DATAFYE_AGENT_MODEL": MODEL,
        # Loopback so the /health MCP probe fails fast (avoids slow .local mDNS).
        "DATAFYE_AGENT_API_MCP_URL": "http://127.0.0.1:3200/mcp",
        "DATAFYE_AGENT_DEPLOYMENT_API_URL": "http://127.0.0.1:7776",
    }
    for d in ("home", "workspace", "docs", "samples"):
        os.makedirs(os.path.join(WORKDIR, d), exist_ok=True)
    log = open(os.path.join(WORKDIR, "agent.log"), "w")
    return subprocess.Popen([PYTHON, "main.py"], cwd=REPO, env=env,
                            stdout=log, stderr=subprocess.STDOUT)


def wait_health():
    for _ in range(60):
        try:
            r = httpx.get(f"{AGENT}/health", timeout=3)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("[sanity] agent never became healthy — see agent.log")


def chat_turn(self_jwt, conv_id, message, retries=1):
    """One /v1/chat turn -> (answer, tools). Retries once on a cold-start
    'initialize' timeout (the CLI subprocess can be slow on first spawn)."""
    for attempt in range(retries + 1):
        content, tools, errored = [], [], False
        with httpx.stream("POST", f"{AGENT}/v1/chat",
                          headers={"Authorization": f"Bearer {self_jwt}"},
                          json={"message": message, "conversation_id": conv_id},
                          timeout=180) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = ev.get("type")
                if t == "content":
                    content.append(ev.get("text", ""))
                elif t == "tool_use_start":
                    tools.append(ev.get("tool"))
                elif t == "error":
                    errored = True
                    print(f"[sanity] SSE error: {ev.get('message')}")
        answer = "".join(content)
        if answer or not errored or attempt == retries:
            return answer, tools
        print("[sanity] retrying turn after cold-start error...")
    return "", []


def get_skills(self_jwt, conv=None):
    params = {"conversation_id": conv} if conv else {}
    r = httpx.get(f"{AGENT}/v1/skills",
                  headers={"Authorization": f"Bearer {self_jwt}"},
                  params=params, timeout=10)
    r.raise_for_status()
    return {s["name"]: s["scope"] for s in r.json()["skills"]}


def main():
    start_jwks()
    proc = start_agent()
    try:
        h = wait_health()
        check("health responds, awaiting bootstrap", h.get("bootstrapped") is False)

        boot = mint({"purpose": "agent-bootstrap", "user_id": USER,
                     "creds_key": Fernet.generate_key().decode()})
        r = httpx.post(f"{AGENT}/bootstrap",
                       headers={"Authorization": f"Bearer {boot}"}, timeout=30)
        check("bootstrap accepts accounts-signed token", r.status_code == 200)

        h = httpx.get(f"{AGENT}/health", timeout=5).json()
        check("post-bootstrap: identity bound", h.get("username") == USER)
        check("post-bootstrap: anthropic key validated",
              h.get("anthropic_key_status") in ("ok", "unvalidated"))

        self_jwt = mint({"sub": USER})

        # --- system skills are listed -----------------------------------
        sysk = get_skills(self_jwt, STRAT_A)
        for s in ("provision-environment", "backtest-strategy", "author-skill"):
            check(f"system skill listed: {s}", sysk.get(s) == "system")

        # --- memory: write global + per-strategy ------------------------
        _, mtools = chat_turn(self_jwt, STRAT_A,
            "Remember for the future: (1) GLOBAL preference for all my strategies — "
            "default to a 2 percent stop-loss; (2) for THIS strategy only — trade only "
            "AAPL and MSFT. Save both to memory in the right scope now.")
        sdir = os.path.join(WORKDIR, "state")
        gmem = os.path.join(sdir, "memory")
        smem = os.path.join(sdir, "strategies", STRAT_A, "memory")

        def wrote_nonindex(d):
            return os.path.isdir(d) and any(f != "MEMORY.md" for f in os.listdir(d))
        check("memory: Write tool used", any((t or "").lower() == "write" for t in mtools))
        check("memory: GLOBAL memory file written", wrote_nonindex(gmem))
        check("memory: PER-STRATEGY memory file written", wrote_nonindex(smem))

        # --- skills: author a user-global skill -------------------------
        _, atools = chat_turn(self_jwt, STRAT_A,
            "Use your author-skill skill to create a reusable skill for ALL my "
            "strategies (user-global) named 'risk-checklist' that lists my pre-trade "
            "risk checks. Create the SKILL.md now.")
        check("author-skill: Skill tool invoked",
              any((t or "").lower() == "skill" for t in atools))
        authored = os.path.join(sdir, "plugins", "user", "skills", "risk-checklist", "SKILL.md")
        check("author-skill: user-global SKILL.md written", os.path.isfile(authored))
        check("author-skill: new skill listed as user-global",
              get_skills(self_jwt, STRAT_A).get("risk-checklist") == "user-global")

        # --- memory recall in a DIFFERENT strategy, no leak -------------
        rans, _ = chat_turn(self_jwt, STRAT_B,
            "What is my default stop-loss preference? One short sentence. No tools.")
        low = rans.lower()
        check("recall: GLOBAL memory recalled in new strategy", "2" in rans and "stop" in low)
        check("recall: per-strategy fact did NOT leak",
              "aapl" not in low and "msft" not in low)

        # --- strategy folder scaffold -----------------------------------
        sa = os.path.join(sdir, "strategies", STRAT_A)
        for rel in ("meta.json", "CLAUDE.md", "PROJECT.md", "memory/MEMORY.md", ".claude/skills"):
            check(f"scaffold: {rel}", os.path.exists(os.path.join(sa, rel)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(WORKDIR, ignore_errors=True)

    passed = sum(1 for _, ok in CHECKS if ok)
    total = len(CHECKS)
    print(f"\n[sanity] {passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
