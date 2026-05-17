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
JWT validation middleware for the Datafye Agent.

The agent serves exactly one user (whose username is established by the
accounts bootstrap push — see main.py's /bootstrap handler). Every
request that touches user-scoped surfaces (chat, broker actions,
credential reads) must carry a JWT issued by the datafye-accounts
service. The agent verifies the JWT's signature against accounts' JWKS
endpoint and rejects anything whose `sub` claim doesn't match its own
bootstrapped username — so user A's JWT can't operate user B's sandbox
even if it lands at the wrong URL.

This module also verifies the accounts-signed bootstrap token itself
(verify_bootstrap_token), which carries no `sub`-vs-self check since it
is what establishes the agent's identity in the first place.

Trust chain:
  - accounts holds the RSA private key, signs tokens at /auth/login
    and signs the agent-bootstrap token at provision time
  - accounts publishes the public half at /auth/jwks
  - this module fetches that JWKS lazily (PyJWKClient caches with TTL),
    verifies every inbound JWT against it, and additionally checks
    sub == the agent's bootstrapped username
"""

from __future__ import annotations

import logging
import os

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

# Where accounts lives. The JWKS endpoint is a known suffix.
ACCOUNTS_URL = os.getenv("DATAFYE_AGENT_ACCOUNTS_URL", "https://accounts.datafye.io").rstrip("/")
JWKS_URL = f"{ACCOUNTS_URL}/datafye-accounts-api/v1/auth/jwks"
EXPECTED_ISSUER = os.getenv("DATAFYE_AGENT_ACCOUNTS_ISSUER", "https://accounts.datafye.io")

# PyJWKClient lazily fetches JWKS on the first verification, then caches
# the keys for `lifespan` seconds. On a key-id miss it refetches — handles
# accounts rotating its signing key without an agent restart.
_jwks_client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)

# Set by main.py's /bootstrap handler once the accounts bootstrap push
# establishes the agent's identity. None until then.
_AGENT_USERNAME: str | None = None


def configure(username: str) -> None:
    """Called by main.py's /bootstrap handler once the agent's identity
    is established by the accounts bootstrap push."""
    global _AGENT_USERNAME
    _AGENT_USERNAME = username
    logger.info("Auth configured: username=%s, jwks_url=%s, issuer=%s",
                username, JWKS_URL, EXPECTED_ISSUER)


def verify_bootstrap_token(token: str) -> dict:
    """Verify an accounts-signed agent-bootstrap token and return its claims.

    Unlike require_self_jwt this does NOT check `sub` against the agent's
    identity — at bootstrap time the agent has no identity yet; this token
    is what establishes it. Verifies the RSA signature against accounts'
    JWKS, the issuer, and that the token was minted for the bootstrap
    purpose. Raises ValueError on any failure.
    """
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=EXPECTED_ISSUER,
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as e:
        raise ValueError(f"invalid bootstrap token: {e}") from e
    if claims.get("purpose") != "agent-bootstrap":
        raise ValueError("token is not an agent-bootstrap token")
    return claims


async def require_self_jwt(authorization: str | None = Header(default=None)) -> dict:
    """
    FastAPI dependency: validates the inbound Bearer JWT and ensures its
    subject matches the agent's bootstrapped username. Returns the
    decoded claims on success. Raises 401 on missing/invalid token,
    403 on subject mismatch.

    Apply via:
        @app.post("/foo", dependencies=[Depends(require_self_jwt)])
    or at the router level:
        APIRouter(..., dependencies=[Depends(require_self_jwt)])
    """
    if _AGENT_USERNAME is None:
        # Misconfiguration: configure() wasn't called. Fail closed.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Agent identity not bootstrapped; auth cannot be enforced",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or malformed Authorization header")
    token = authorization[len("Bearer "):].strip()
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=EXPECTED_ISSUER,
            # We don't currently set an audience claim; skip aud verification.
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {e}")

    sub = claims.get("sub")
    if sub != _AGENT_USERNAME:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Token subject '{sub}' does not match agent identity '{_AGENT_USERNAME}'",
        )
    return claims
