"""
Identity bootstrap for the Datafye Agent.

The agent serves exactly one user. Its identity is the username it was
provisioned for; that single string drives everything downstream
(credentials lookups, JWT subject matching, etc.).

Production path (on EC2): username comes from the instance's `Name` tag,
which the Rumi AwsProvisioner sets at launch time to the instance's
short name (e.g. `agents-u123456`). We strip the network prefix and
keep the trailing identifier.

Development path (local): both username and instance ID can be supplied
via env vars (DATAFYE_AGENT_USERNAME, DATAFYE_AGENT_INSTANCE_ID) so the
agent runs cleanly on a laptop with no IMDS available.

If neither IMDS nor env vars are available, this module refuses to
return — better to fail loud at startup than to run with an undefined
identity and serve the wrong user.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

IMDS_BASE = "http://169.254.169.254"
IMDS_TIMEOUT_SECONDS = 2.0
IMDS_TOKEN_TTL_SECONDS = 21600


@dataclass(frozen=True)
class Identity:
    """The agent's bootstrapped identity."""
    username: str
    instance_id: str
    source: str  # "imds" or "env"


def bootstrap() -> Identity:
    """
    Bootstrap the agent's identity. Tries IMDSv2 first, falls back to
    env vars, raises if neither works.
    """
    identity = _try_imds() or _try_env()
    if identity is None:
        raise RuntimeError(
            "Datafye Agent identity could not be bootstrapped. "
            "Either run on an EC2 instance with a 'Name' tag like 'agents-u123456' "
            "(production) or set DATAFYE_AGENT_USERNAME + DATAFYE_AGENT_INSTANCE_ID "
            "(local dev)."
        )
    logger.info(
        "Bootstrapped identity: username=%s instance_id=%s source=%s",
        identity.username, identity.instance_id, identity.source,
    )
    return identity


def _try_imds() -> Identity | None:
    """Try IMDSv2. Returns None on any failure (no IMDS, missing tag, etc.)."""
    try:
        with httpx.Client(base_url=IMDS_BASE, timeout=IMDS_TIMEOUT_SECONDS) as http:
            token_resp = http.put(
                "/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": str(IMDS_TOKEN_TTL_SECONDS)},
            )
            if token_resp.status_code != 200:
                return None
            token = token_resp.text.strip()
            headers = {"X-aws-ec2-metadata-token": token}

            instance_id_resp = http.get("/latest/meta-data/instance-id", headers=headers)
            if instance_id_resp.status_code != 200:
                return None
            instance_id = instance_id_resp.text.strip()

            name_tag_resp = http.get("/latest/meta-data/tags/instance/Name", headers=headers)
            if name_tag_resp.status_code != 200:
                # IMDS reachable but no Name tag (or instance tags not enabled). Fall
                # back to env so an operator can recover without re-provisioning.
                logger.warning(
                    "IMDS reachable but 'Name' tag unavailable (HTTP %s). "
                    "Falling back to env-var override if set.",
                    name_tag_resp.status_code,
                )
                return None
            name_tag = name_tag_resp.text.strip()

        username = _username_from_name_tag(name_tag)
        return Identity(username=username, instance_id=instance_id, source="imds")
    except httpx.HTTPError:
        return None  # not on EC2 / IMDS unreachable
    except Exception as e:
        logger.warning("Unexpected error reading IMDS: %s", e)
        return None


def _try_env() -> Identity | None:
    username = os.environ.get("DATAFYE_AGENT_USERNAME", "").strip()
    instance_id = os.environ.get("DATAFYE_AGENT_INSTANCE_ID", "").strip()
    if username and instance_id:
        return Identity(username=username, instance_id=instance_id, source="env")
    return None


def _username_from_name_tag(name_tag: str) -> str:
    """
    The AwsProvisioner Name tag has the form '{networkName}-{shortName}',
    e.g. 'agents-u123456'. The trailing segment is the username.
    """
    if "-" not in name_tag:
        return name_tag
    return name_tag.split("-", 1)[1]
