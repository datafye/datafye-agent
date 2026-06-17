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
Broker (ConnectTrade) integration for the Datafye Agent.

Exposes /v1/broker/* endpoints used by the Datafye App to manage brokerage
connections through ConnectTrade. All requests to ConnectTrade are made
server-side with Datafye's client_id / client_secret and the per-user
user_id / user_secret — none of these credentials ever leave the agent.

Flow:
  browser -> agent POST /v1/broker/connections {type, broker}
  agent   -> ConnectTrade POST /connections with broker preselected + redirect_url
  agent   -> browser { authorization_url }
  browser -> opens popup to authorization_url
  popup   -> ConnectTrade OAuth for the chosen broker
  popup   -> redirect to BROKER_REDIRECT_URL?connection_id=...
  popup   -> postMessage to opener, closes

Credential sourcing — accounts is the system of record for all four ConnectTrade pieces:
  - client_id / client_secret (system-wide): delivered by the accounts service into the
    encrypted credentials store via the credential push channel (see datafye-accounts
    pushPlatformConnecttradeClient). The DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID/_SECRET env
    vars are a local-dev seed only.
  - user_id / user_secret (per-user): lazily minted with ConnectTrade on first connect,
    persisted in the encrypted store, AND written back to accounts (_write_back_user_creds)
    so accounts holds them too — survives a sandbox rebuild.
"""

import logging
import os
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

import auth

logger = logging.getLogger(__name__)

# -- Configuration -------------------------------------------------
CONNECTTRADE_API_URL = os.getenv(
    "DATAFYE_AGENT_CONNECTTRADE_API_URL",
    "https://api.connecttrade.com",
)

# Redirect target hosted on the Datafye App domain (broker-callback.html in datafye-app).
# ConnectTrade appends ?connection_id=... on success or ?error=... on failure.
BROKER_REDIRECT_URL = os.getenv(
    "DATAFYE_AGENT_BROKER_REDIRECT_URL",
    "https://developer.datafye.io/broker-callback.html",
)

# Datafye's supported brokers — mirrors StocksBroker enum in
# datafye-roe/src/main/models/com/datafye/roe/messages.xml. Keep in sync:
# a broker missing here can't be picked in the Datafye App.
SUPPORTED_BROKERS: list[dict[str, str]] = [
    {"code": "ALPACA",       "name": "Alpaca"},
    {"code": "LIGHTSPEED",   "name": "Lightspeed"},
    {"code": "TASTYTRADE",   "name": "Tastytrade"},
    {"code": "TRADESTATION", "name": "TradeStation"},
    {"code": "TRADEZERO",    "name": "TradeZero"},
    {"code": "WEBULL",       "name": "Webull"},
]
SUPPORTED_BROKER_CODES = {b["code"] for b in SUPPORTED_BROKERS}

# -- Shared credentials handle (set by main.py) --------------------
# main.py owns the mutable credentials dict; we bind to the same object so that
# runtime updates via POST /v1/credentials and our lazy provisioning stay in sync.
_credentials: Optional[dict] = None


def configure(credentials: dict) -> None:
    """Bind the shared credentials dict from main.py."""
    global _credentials
    _credentials = credentials


def _creds() -> dict:
    if _credentials is None:
        raise RuntimeError("broker.configure(credentials) was not called")
    return _credentials


# -- ConnectTrade credential helpers -------------------------------

def _client_headers() -> dict[str, str]:
    creds = _creds()
    client_id = creds.get("connecttrade_client_id") or ""
    client_secret = creds.get("connecttrade_client_secret") or ""
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "ConnectTrade client credentials not configured. They are "
                "delivered by the accounts service; for local dev set "
                "DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID and _SECRET."
            ),
        )
    return {"client-id": client_id, "client-secret": client_secret}


async def _ensure_user(auth_token: Optional[str] = None) -> tuple[str, str]:
    """
    Return (user_id, user_secret), provisioning with ConnectTrade if needed.

    Persistence is handled by the encrypted credentials store — writing to the
    shared `creds` dict (which is a CredentialsStore instance) auto-flushes to
    disk via __setitem__. A freshly-minted pair is also written back to accounts
    (the system of record) via _write_back_user_creds, forwarding `auth_token`.
    """
    creds = _creds()
    user_id = creds.get("connecttrade_user_id") or ""
    user_secret = creds.get("connecttrade_user_secret") or ""
    if user_id and user_secret:
        return user_id, user_secret

    # provision a fresh user with ConnectTrade
    client_headers = _client_headers()
    new_user_id = str(uuid.uuid4())
    async with httpx.AsyncClient(base_url=CONNECTTRADE_API_URL, timeout=30.0) as http:
        resp = await http.post(
            "/users",
            headers={**client_headers, "Content-Type": "application/json"},
            json={"user_id": new_user_id},
        )
    if resp.status_code >= 400:
        logger.error("ConnectTrade POST /users failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=_extract_error(resp))
    body = resp.json()
    user_id = body.get("user_id") or new_user_id
    user_secret = body.get("user_secret") or ""
    if not user_secret:
        raise HTTPException(status_code=502, detail="ConnectTrade did not return a user_secret")

    creds["connecttrade_user_id"] = user_id
    creds["connecttrade_user_secret"] = user_secret
    logger.info("Provisioned new ConnectTrade user: %s", user_id)
    # Land the freshly-minted per-user creds in accounts (the system of record
    # for all four ConnectTrade pieces). Best-effort; the creds already work
    # locally from the encrypted store regardless of whether this lands.
    await _write_back_user_creds(user_id, user_secret, auth_token)
    return user_id, user_secret


async def _user_headers(auth_token: Optional[str] = None) -> dict[str, str]:
    user_id, user_secret = await _ensure_user(auth_token)
    return {**_client_headers(), "user-id": user_id, "user-secret": user_secret}


def _bearer(authorization: Optional[str]) -> Optional[str]:
    """Extract the raw bearer token from an Authorization header, or None."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[len("Bearer "):].strip()
    return None


async def _write_back_user_creds(user_id: str, user_secret: str,
                                 auth_token: Optional[str]) -> None:
    """Best-effort: write the freshly-minted ConnectTrade user creds back to
    accounts so accounts is the system of record for all four ConnectTrade
    pieces. Forwards the user's own JWT (a self-scoped write) over the same
    agent->accounts channel the usage reporter uses — no new credential.

    Non-fatal on failure: the creds already work locally (encrypted store),
    accounts' drift-reconcile re-pushes on the next sync, and the next mint
    after a sandbox rebuild rewrites them. Skipped on a self-hosted / local run
    with no forwarded identity (store-only)."""
    user = auth.username()
    if not auth_token or not user:
        return
    base = f"{auth.ACCOUNTS_URL}/datafye-accounts-api/v1/accounts/{user}/credentials"
    headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            for provider, value in (("connecttrade_user_id", user_id),
                                    ("connecttrade_user_secret", user_secret)):
                resp = await http.put(f"{base}/{provider}", headers=headers,
                                      json={"value": value})
                if resp.status_code >= 400:
                    logger.warning("ConnectTrade user-cred write-back to accounts "
                                   "returned %s for %s", resp.status_code, provider)
    except Exception as e:
        logger.warning("ConnectTrade user-cred write-back to accounts failed: %s", e)


def _extract_error(resp: httpx.Response) -> str:
    try:
        return resp.json().get("message") or resp.text or f"HTTP {resp.status_code}"
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


# -- Request models ------------------------------------------------

class ConnectRequest(BaseModel):
    type: str = "trade"     # "trade" | "readonly"
    broker: str             # StocksBroker code, e.g. "ALPACA"


# -- Routes --------------------------------------------------------
# All /v1/broker/* routes require a valid Bearer JWT issued by accounts
# whose `sub` claim matches this agent's bootstrapped username.
router = APIRouter(prefix="/v1/broker", tags=["broker"], dependencies=[Depends(auth.require_self_jwt)])


@router.get("/brokers")
async def list_brokers():
    """List brokers Datafye supports for connection (StocksBroker enum)."""
    return {"brokers": SUPPORTED_BROKERS}


@router.get("/connections")
async def list_connections(authorization: Optional[str] = Header(default=None)):
    """Return the user's current brokerage connections with linked accounts."""
    headers = await _user_headers(_bearer(authorization))
    async with httpx.AsyncClient(base_url=CONNECTTRADE_API_URL, timeout=30.0) as http:
        resp = await http.get("/connections", headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=_extract_error(resp))
    raw = resp.json() or []
    connections = []
    for c in raw:
        accounts = [
            {
                "account_id": a.get("account_id"),
                "account_number": a.get("account_number"),
                "institution_name": a.get("institution_name"),
            }
            for a in (c.get("accounts") or [])
        ]
        connections.append({
            "connection_id": c.get("connection_id"),
            "broker": c.get("broker"),
            # ConnectTrade's ConnectionResponse has no explicit "status"; the presence of
            # a connection implies "active". Exposed as status so the UI can render a pill.
            "status": "active",
            "type": c.get("connection_type"),
            "accounts": accounts,
        })
    return {"connections": connections}


@router.post("/connections")
async def create_connection(body: ConnectRequest, authorization: Optional[str] = Header(default=None)):
    """Generate a ConnectTrade OAuth URL for the chosen broker. Browser opens it in a popup."""
    broker = (body.broker or "").upper()
    if broker not in SUPPORTED_BROKER_CODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported broker '{broker}'. Supported: {sorted(SUPPORTED_BROKER_CODES)}",
        )
    connection_type = body.type or "trade"
    if connection_type not in ("trade", "readonly"):
        raise HTTPException(status_code=400, detail="type must be 'trade' or 'readonly'")

    headers = await _user_headers(_bearer(authorization))
    async with httpx.AsyncClient(base_url=CONNECTTRADE_API_URL, timeout=30.0) as http:
        resp = await http.post(
            "/connections",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "connection_type": connection_type,
                "broker": broker,
                "redirect_url": BROKER_REDIRECT_URL,
            },
        )
    if resp.status_code >= 400:
        logger.error("ConnectTrade POST /connections failed: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=_extract_error(resp))
    data = resp.json()
    auth_url = data.get("connection_request_url")
    if not auth_url:
        raise HTTPException(status_code=502, detail="ConnectTrade did not return a connection URL")
    return {
        "authorization_url": auth_url,
        "expires_at": data.get("expires_at"),
    }


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str, authorization: Optional[str] = Header(default=None)):
    """Revoke a brokerage connection."""
    headers = await _user_headers(_bearer(authorization))
    async with httpx.AsyncClient(base_url=CONNECTTRADE_API_URL, timeout=30.0) as http:
        resp = await http.delete(f"/connections/{connection_id}", headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=_extract_error(resp))
    return {"ok": True}
