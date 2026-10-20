#!/usr/bin/env python3
"""The deployment token the CLI needs in its environment (DAT-404). Offline and deterministic.

A deployment carrying a broker or the Precision Alpha dataset cannot be provisioned without
`DATAFYE_ACCOUNTS_URL` + `DATAFYE_DEPLOYMENT_TOKEN`: the deploy engine refuses up front. Nothing in
this agent set them, so Yukti could not stand either kind up. These checks pin the parts that must
never be wrong, each of which fails SILENTLY if it is:

  - a turn never fails because of this (it runs on every one, and most turns do not need a token)
  - a self-hosted run with no forwarded identity is skipped, not half-configured
  - a usable token is not re-minted on every turn, and an expiring one IS replaced
  - the accounts URL and the token are set together, never one without the other

    python tests/deployment_token.py

Exits non-zero on any failure.
"""
import asyncio
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ.setdefault("DATAFYE_AGENT_SKIP_STORE", "1")

import main  # noqa: E402
import auth  # noqa: E402

FAILED = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILED.append(what)


def jwt_expiring_in(seconds):
    """A JWT-shaped string whose exp claim is `seconds` from now. Unsigned: nothing verifies it."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u1", "exp": int(time.time()) + seconds}).encode()
    ).decode().rstrip("=")
    return "hdr." + payload + ".sig"


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class _Client:
    """Stands in for httpx.AsyncClient. Records what was asked of it."""

    calls = []
    resp = _Resp(200, {"token": "minted-token"})

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None):
        _Client.calls.append((url, (headers or {}).get("Authorization")))
        if isinstance(_Client.resp, Exception):
            raise _Client.resp
        return _Client.resp


def reset(*, username="u1", accounts_url="https://accounts.example.com",
          resp=_Resp(200, {"token": "minted-token"})):
    os.environ.pop("DATAFYE_ACCOUNTS_URL", None)
    os.environ.pop("DATAFYE_DEPLOYMENT_TOKEN", None)
    _Client.calls = []
    _Client.resp = resp
    auth._AGENT_USERNAME = username
    auth.ACCOUNTS_URL = accounts_url


def run(auth_token="user-jwt"):
    asyncio.run(main._ensure_deployment_token(auth_token))


def main_checks():
    main.httpx.AsyncClient = _Client

    # -- the happy path, and that it addresses the right account --
    reset()
    run()
    check(os.environ.get("DATAFYE_DEPLOYMENT_TOKEN") == "minted-token", "a token is minted and exported")
    check(os.environ.get("DATAFYE_ACCOUNTS_URL") == "https://accounts.example.com",
          "the accounts URL is exported alongside it")
    check(len(_Client.calls) == 1, "one mint")
    url, authz = _Client.calls[0]
    check(url == "https://accounts.example.com/datafye-accounts-api/v1/accounts/u1/deployment-token",
          "the mint endpoint is addressed for this account")
    check(authz == "Bearer user-jwt",
          "minted with the USER's forwarded session, not a credential of the agent's own")

    # -- never re-minted while the one in hand is good --
    # It runs on every chat turn. A mint per turn is a round trip per turn, and 90-day tokens.
    reset()
    os.environ["DATAFYE_DEPLOYMENT_TOKEN"] = jwt_expiring_in(60 * 24 * 3600)
    run()
    check(len(_Client.calls) == 0, "a token with months left is not re-minted")

    # -- but an expiring one IS replaced: a box can outlive a 90-day token --
    reset()
    os.environ["DATAFYE_DEPLOYMENT_TOKEN"] = jwt_expiring_in(3600)
    run()
    check(len(_Client.calls) == 1, "a token inside the refresh margin is replaced")
    check(os.environ.get("DATAFYE_DEPLOYMENT_TOKEN") == "minted-token", "...with the new one")

    # -- an unparseable token is replaced rather than trusted forever --
    reset()
    os.environ["DATAFYE_DEPLOYMENT_TOKEN"] = "not-a-jwt"
    run()
    check(len(_Client.calls) == 1, "a token whose expiry cannot be read is replaced")

    # -- self-hosted: no identity forwarded, so nothing to mint against --
    # Skipped, NOT half-configured: palpha and broker are simply unavailable there.
    reset()
    run(auth_token=None)
    check(len(_Client.calls) == 0 and "DATAFYE_DEPLOYMENT_TOKEN" not in os.environ,
          "no forwarded identity: skipped, and nothing exported")
    reset(username=None)
    run()
    check(len(_Client.calls) == 0, "no bootstrapped username: skipped")
    reset(accounts_url=None)
    run()
    check(len(_Client.calls) == 0, "no accounts URL: skipped")

    # -- a failure must never fail the turn, and must never half-configure --
    for label, resp in (("an HTTP error", _Resp(403)),
                        ("a transport failure", RuntimeError("connection refused")),
                        ("a reply with no token", _Resp(200, {}))):
        reset(resp=resp)
        try:
            run()
            raised = False
        except Exception:
            raised = True
        check(not raised, f"{label} does not raise into the chat turn")
        check("DATAFYE_DEPLOYMENT_TOKEN" not in os.environ,
              f"{label} exports no token")
        check("DATAFYE_ACCOUNTS_URL" not in os.environ,
              f"{label} exports no accounts URL either (never one without the other)")

    # -- the dead exports are gone (DAT-404) --
    # Nothing read these after DAT-76; the deploy engine reads none of them and the platform's
    # connecttrade_* strings are provider names in the accounts fetch, not env vars.
    exported = {name for names in main._CREDENTIAL_ENV_MAP.values() for name in names}
    check(not any(n.startswith("CONNECTTRADE_") for n in exported),
          "no vestigial CONNECTTRADE_* is exported to the CLI")
    check("POLYGON_API_KEY" in exported, "the Polygon key is still exported (descriptors substitute it)")


if __name__ == "__main__":
    main_checks()
    print("")
    if FAILED:
        print(f"{len(FAILED)} check(s) failed:")
        for f in FAILED:
            print(f"  - {f}")
        sys.exit(1)
    print("all deployment-token checks passed")
