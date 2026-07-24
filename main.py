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
Datafye Agent Service

A dedicated per-user FastAPI backend that wraps the Claude Agent SDK for
algorithmic trading strategy development. Each user gets their own instance
with access to:

- Local Datafye documentation
- Datafye CLI (foundry provisioning, trading environments, data access)
- File system tools for building Python-based algos
- User's data provider and broker credentials

SSE streaming responses with structured events for the agent frontend,
including custom events for environment status, scorecard, and chart data.
"""

import json
import os
import logging
import socket
import time
from typing import Optional, AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from claude_agent_sdk import (
    query, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, SystemMessage,
    create_sdk_mcp_server, tool,
)

from prompt import build_system_prompt
import auth
import broker
import conversations
import credentials as credentials_module
import memory
import skills

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -- Configuration from environment --------------------------------
# All env vars use DATAFYE_AGENT_ prefix for consistency.
#
# The Anthropic API key is NOT read here — it is a credential, held in the
# encrypted credentials store and delivered by accounts (platform key) or
# entered by the user (BYO key). _apply_anthropic_key() syncs it into
# os.environ["ANTHROPIC_API_KEY"] so the Claude Agent SDK subprocess picks
# it up. DATAFYE_AGENT_ANTHROPIC_API_KEY still works as a local-dev seed
# (see _credential_env_seed()).
CLAUDE_MODEL = os.getenv("DATAFYE_AGENT_MODEL", "opus")
PORT = int(os.getenv("DATAFYE_AGENT_PORT", "18780"))
ALLOWED_ORIGINS = os.getenv("DATAFYE_AGENT_ALLOWED_ORIGINS", "*").split(",")

# Working directory for algo development (user's workspace)
WORKSPACE_DIR = os.getenv("DATAFYE_AGENT_WORKSPACE", "/home/datafye/workspace")

# Path to local Datafye documentation
DOCS_DIR = os.getenv("DATAFYE_AGENT_DOCS_DIR", "/home/datafye/docs")

# Path to Datafye CLI binary
CLI_PATH = os.getenv("DATAFYE_AGENT_CLI_PATH", "datafye")

# Path to Datafye samples (Java-based reference for API patterns)
SAMPLES_DIR = os.getenv("DATAFYE_AGENT_SAMPLES_DIR", "/home/datafye/samples")

# The foundry resource-cost cheat sheet ships with the agent app clone; the prompt
# points the agent at it to estimate memory/disk before heavy foundry operations.
CHEATSHEET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "reference", "foundry-resource-cost-cheatsheet.md")

# User credentials (injected per-user at launch)
MASSIVE_API_KEY = os.getenv("DATAFYE_AGENT_MASSIVE_API_KEY", "")
PALPHA_API_KEY = os.getenv("DATAFYE_AGENT_PALPHA_API_KEY", "")
HWAI_API_KEY = os.getenv("DATAFYE_AGENT_HWAI_API_KEY", "")
CONNECTTRADE_CLIENT_ID = os.getenv("DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID", "")
CONNECTTRADE_CLIENT_SECRET = os.getenv("DATAFYE_AGENT_CONNECTTRADE_CLIENT_SECRET", "")
CONNECTTRADE_USER_ID = os.getenv("DATAFYE_AGENT_CONNECTTRADE_USER_ID", "")
CONNECTTRADE_USER_SECRET = os.getenv("DATAFYE_AGENT_CONNECTTRADE_USER_SECRET", "")

# GitHub - defaults to Datafye org, user can override
GITHUB_USER = os.getenv("DATAFYE_AGENT_GITHUB_USER", "")
GITHUB_TOKEN = os.getenv("DATAFYE_AGENT_GITHUB_TOKEN", "")
GITHUB_ORG = os.getenv("DATAFYE_AGENT_GITHUB_ORG", "datafye")

# Datafye API MCP server — provisioned alongside every foundry/trading
# deployment by the CLI. The installer configures /etc/hosts so this URL
# resolves to 127.0.0.1 on the agent machine.
DATAFYE_API_MCP_URL = os.getenv(
    "DATAFYE_AGENT_API_MCP_URL",
    "http://local-foundry-dev-mcp-api.datafye.local:3200/mcp",
)

# Datafye deployment REST API — part of the same datafye-api service the MCP
# server fronts, but the plain HTTP REST surface (Jersey/Jetty on port 7776).
# The CLI writes a /etc/hosts entry mapping this hostname to 127.0.0.1 on the
# agent machine. Used to read the running environment's deployment descriptor
# (GET /datafye-api/v1/deployment/descriptor) and derive env_status after a
# chat turn. If no environment is up the agent simply emits nothing.
DATAFYE_DEPLOYMENT_API_URL = os.getenv(
    "DATAFYE_AGENT_DEPLOYMENT_API_URL",
    "http://local-foundry-dev-api.datafye.local:7776",
)

# MCP servers (optional, for additional tooling)
MCP_SERVERS_ADDITIONAL = os.getenv("DATAFYE_AGENT_MCP_SERVERS_ADDITIONAL", "[]")

# The agent runs a single, explicit memory model (see memory.py + conversations.py):
# global notes/index under the state root, per-strategy CLAUDE.md + memory/ in each
# strategy folder. The claude CLI that the SDK spawns has its OWN auto-memory feature,
# which is ON by default and would maintain a second, uncontrolled store. Disable it
# so there is one coherent memory system. The SDK subprocess inherits this env var.
# Overridable by pre-setting it in the environment.
os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")


def check_api_mcp_reachable(url: str, timeout: float = 2.0) -> bool:
    """Cheap TCP reachability check for the Datafye API MCP server.

    Returns True if the port is listening. Doesn't validate the MCP protocol
    itself — the installer's provision step is the load-bearing guarantee
    that the server is correctly stood up. This is for runtime monitoring
    (e.g., so the frontend can surface a useful message if the user has
    stopped the foundry environment).
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

# -- Internal tools ------------------------------------------------
INTERNAL_TOOLS = [
    # File operations
    "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS",
    # Execution
    "Bash",
    # Planning (no AskUserQuestion — it's a structured-prompt tool the
    # Claude Code harness renders interactively; the Datafye workspace has
    # no handler for it, so a model that used it would silently fail to
    # surface its question. Without the tool, the model asks inline.)
    "EnterPlanMode", "ExitPlanMode",
    # Notebook
    "NotebookEdit",
    # Discovery
    "Skill", "ToolSearch",
]

# -- Session storage -----------------------------------------------
# Single session per user -- maps conversation_id -> agent session_id
sessions: dict[str, str] = {}


# -- Activity tracking (read by /health for accounts' idle monitor) ---
# lastChatActivityAt: epoch ms of the most recent /v1/chat invocation. 0 = never.
# runningJobs: count of in-flight chat streams. Incremented on stream start,
#   decremented on stream completion (in tracked_stream_agent_response below).
# activeProxiedApps: list of agent-managed app routes currently registered with
#   the accounts service. Empty for v1 — placeholder for the future feature
#   where the agent can stand up Jupyter etc. and ask accounts to proxy them.
# Seeded to boot time (not 0) so the accounts idle-monitor — which skips
# last_chat_activity_at == 0 as "never active" — also idle-stops an agent that
# was provisioned but never chatted with: idle is measured from boot, so an
# unused agent goes Dormant after the threshold (and auto-wakes invisibly).
last_chat_activity_at: int = int(time.time() * 1000)
running_jobs: int = 0
active_proxied_apps: list[str] = []


# -- Request/Response Models ---------------------------------------

class ChatRequest(BaseModel):
    """Request model for chat endpoint."""
    message: str
    conversation_id: Optional[str] = None
    algo_id: Optional[str] = None


class HealthResponse(BaseModel):
    """Response model for health endpoint."""
    status: str
    bootstrapped: bool              # False until the accounts bootstrap push lands
    configured: bool                # an Anthropic key is set (any non-"missing" status)
    anthropic_key_status: str       # missing | ok | invalid | unvalidated
    workspace: str
    docs_available: bool
    cli_available: bool
    api_mcp_available: bool
    credentials: dict[str, bool]
    username: Optional[str] = None              # None until bootstrapped
    credentials_generation: Optional[str] = None  # None until bootstrapped
    # Idle signals consumed by accounts' poll loop (Chunk 4):
    last_chat_activity_at: int      # epoch ms; 0 if no chat yet
    running_jobs: int               # count of in-flight chat streams
    active_proxied_apps: list[str]  # always [] in v1


class CredentialsUpdate(BaseModel):
    """Update user credentials at runtime."""
    massive_api_key: Optional[str] = None
    palpha_api_key: Optional[str] = None
    hwai_api_key: Optional[str] = None
    connecttrade_client_id: Optional[str] = None
    connecttrade_client_secret: Optional[str] = None
    connecttrade_user_id: Optional[str] = None
    connecttrade_user_secret: Optional[str] = None
    github_user: Optional[str] = None
    github_token: Optional[str] = None


# -- Bootstrap state ----------------------------------------------
# The agent's identity and its credentials-store key are NOT known at
# startup — they arrive from the accounts service via the bootstrap push
# (POST /bootstrap). Until that lands the agent runs "awaiting bootstrap":
# /health and /bootstrap respond; every user-facing endpoint returns 503.
#
# AGENT_USERNAME — the agent's identity once bootstrapped (None until then).
# credentials    — the encrypted credentials store, opened with the
#                  creds_key from the push (None until then).
AGENT_USERNAME: Optional[str] = None
credentials: Optional[credentials_module.CredentialsStore] = None
_bootstrapped: bool = False

# Anthropic key status, surfaced on /health and checked by /v1/chat:
#   "missing"     — no key configured; chat unavailable
#   "ok"          — validated against the Anthropic API
#   "invalid"     — the Anthropic API rejected the key
#   "unvalidated" — a key is set but validation couldn't be confirmed
#                   (network blip); chat proceeds optimistically
anthropic_key_status: str = "missing"


def _credential_env_seed() -> dict:
    """Legacy env-var credential seed, applied only when a store is created
    fresh (local dev). In production, accounts pushes credentials."""
    return {
        "anthropic_api_key": os.getenv("DATAFYE_AGENT_ANTHROPIC_API_KEY", ""),
        "massive_api_key": MASSIVE_API_KEY,
        "palpha_api_key": PALPHA_API_KEY,
        "hwai_api_key": HWAI_API_KEY,
        "connecttrade_client_id": CONNECTTRADE_CLIENT_ID,
        "connecttrade_client_secret": CONNECTTRADE_CLIENT_SECRET,
        "connecttrade_user_id": CONNECTTRADE_USER_ID,
        "connecttrade_user_secret": CONNECTTRADE_USER_SECRET,
        "github_user": GITHUB_USER,
        "github_token": GITHUB_TOKEN,
    }


def _validate_anthropic_key(key: str) -> str:
    """Quick liveness check of an Anthropic API key against the Anthropic
    API. Returns "ok", "invalid", or "unvalidated" (the network couldn't
    confirm — the caller treats that as a soft pass)."""
    try:
        resp = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            timeout=5.0,
        )
    except httpx.HTTPError as e:
        logger.warning("Anthropic key validation could not reach the API: %s", e)
        return "unvalidated"
    if resp.status_code == 200:
        return "ok"
    if resp.status_code in (401, 403):
        return "invalid"
    logger.warning("Anthropic key validation got unexpected HTTP %s", resp.status_code)
    return "unvalidated"


def _apply_anthropic_key() -> None:
    """Sync the Anthropic key from the credentials store into the process
    environment — the Claude Agent SDK subprocess inherits os.environ — and
    validate it. Updates anthropic_key_status. Called after bootstrap and
    after any credentials push that touches the Anthropic key."""
    global anthropic_key_status
    key = credentials.get("anthropic_api_key") if credentials else None
    if not key:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        anthropic_key_status = "missing"
        logger.warning("Anthropic API key not configured — chat is unavailable until one is set")
        return
    os.environ["ANTHROPIC_API_KEY"] = key
    anthropic_key_status = _validate_anthropic_key(key)
    logger.info("Anthropic API key applied (status=%s)", anthropic_key_status)


# Maps credentials-store keys to the env-var names the Datafye CLI /
# deployment descriptors expect via ${VAR} substitution. Each store key
# maps to one or more env-var aliases — historical renames (Polygon ->
# Massive, Palpha -> Precision Alpha) are exported under both names so a
# descriptor written against either resolves. The Anthropic key is handled
# separately by _apply_anthropic_key because it also validates the key.
_CREDENTIAL_ENV_MAP = {
    "massive_api_key":             ["POLYGON_API_KEY", "MASSIVE_API_KEY"],
    "palpha_api_key":              ["PALPHA_API_KEY", "PRECISION_ALPHA_API_KEY"],
    "hwai_api_key":                ["HWAI_API_KEY"],
    "connecttrade_client_id":      ["CONNECTTRADE_CLIENT_ID"],
    "connecttrade_client_secret":  ["CONNECTTRADE_CLIENT_SECRET"],
    "connecttrade_user_id":        ["CONNECTTRADE_USER_ID"],
    "connecttrade_user_secret":    ["CONNECTTRADE_USER_SECRET"],
    "github_user":                 ["GITHUB_USER"],
    "github_token":                ["GITHUB_TOKEN"],
}


def _apply_credentials_env() -> None:
    """Sync the data-provider / broker / GitHub credentials from the
    encrypted store into the process environment so any subprocess the
    Claude Agent SDK spawns — and any datafye CLI invocation — inherits
    them. Deployment descriptors use ${VAR} substitution
    (e.g. polygon_api_key: ${POLYGON_API_KEY}); without this sync the
    values were locked inside the agent and the CLI saw blank
    substitutions. Called after bootstrap and after every
    /v1/credentials/update push."""
    if credentials is None:
        return
    for store_key, env_names in _CREDENTIAL_ENV_MAP.items():
        val = credentials.get(store_key)
        for name in env_names:
            if val:
                os.environ[name] = val
            else:
                os.environ.pop(name, None)


def build_mcp_config() -> tuple[dict, list[str]]:
    """Build MCP servers dict and allowed tools list."""
    mcp_servers = {}
    allowed_tools = list(INTERNAL_TOOLS)

    # Datafye API MCP server — primary interface to the running deployment.
    # Always registered; if the foundry environment is down the SDK will
    # surface tool-call errors on first use.
    mcp_servers["datafye-api"] = {"type": "http", "url": DATAFYE_API_MCP_URL}
    allowed_tools.append("mcp__datafye-api__*")

    # Additional MCP servers from JSON config
    try:
        additional_servers = json.loads(MCP_SERVERS_ADDITIONAL)
        for server in additional_servers:
            name = server.get("name")
            url = server.get("url")
            if name and url:
                mcp_servers[name] = {"type": "http", "url": url}
                tools = server.get("allowed_tools", [])
                if tools:
                    allowed_tools.extend(tools)
                else:
                    allowed_tools.append(f"mcp__{name}__*")
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse DATAFYE_MCP_SERVERS_ADDITIONAL: {e}")

    return mcp_servers, allowed_tools


def get_credential_summary() -> str:
    """Build a credential summary for the system prompt."""
    lines = []

    if credentials.get("massive_api_key"):
        lines.append("- Massive (Polygon) API key: configured (for SIP and Crypto datasets)")
    else:
        lines.append("- Massive (Polygon) API key: NOT configured (needed for SIP and Crypto datasets)")

    if credentials.get("palpha_api_key"):
        lines.append("- Precision Alpha API key: configured (for Palpha dataset)")
    else:
        lines.append("- Precision Alpha API key: NOT configured (needed for Palpha dataset)")

    if credentials.get("hwai_api_key"):
        lines.append("- HWAI API key: configured (for HWAI dataset)")
    else:
        lines.append("- HWAI API key: NOT configured (needed for HWAI dataset)")

    ct_configured = all([
        credentials.get("connecttrade_client_id"),
        credentials.get("connecttrade_client_secret"),
        credentials.get("connecttrade_user_id"),
        credentials.get("connecttrade_user_secret"),
    ])
    if ct_configured:
        lines.append("- ConnectTrade broker credentials: configured (for simulated trading)")
    else:
        lines.append("- ConnectTrade broker credentials: NOT configured (needed for simulated trading)")

    if credentials.get("github_user") and credentials.get("github_token"):
        lines.append(f"- GitHub: using personal account ({credentials.get('github_user')})")
    else:
        lines.append(f"- GitHub: using Datafye org ({GITHUB_ORG})")

    return "\n".join(lines)


# -- SSE Helpers ---------------------------------------------------

def sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"data: {json.dumps({'type': event_type, **data})}\n\n"


def truncate(text: str, limit: int = 150) -> str:
    """Truncate text for logging."""
    if not text:
        return "<empty>"
    cleaned = text.replace("\n", "\\n").replace("\r", "")
    return cleaned[:limit] + "..." if len(cleaned) > limit else cleaned


# A short, cheap model used only to summarize a strategy's first message into a
# title — never the main reasoning model.
TITLE_MODEL = os.getenv("DATAFYE_AGENT_TITLE_MODEL", "claude-haiku-4-5")
_TITLE_PROMPT = (
    "Generate a short, specific title (3 to 6 words, Title Case, no quotes, no "
    "trailing punctuation) summarizing this request. Reply with ONLY the title.\n\nRequest: "
)


async def generate_title(first_message: str, usage_sink: Optional[list] = None) -> Optional[str]:
    """Summarize the user's first message into a short strategy title via a
    cheap model call (direct Anthropic API, the key is already in the env).
    Returns None on any failure, in which case the caller keeps the provisional
    first-few-words name. If `usage_sink` is given, this call's token usage is
    appended (attributed to the sidecar model) so it's counted in the turn's
    per-model roll-up -- it runs outside the agent SDK session, so it does NOT
    appear in ResultMessage.model_usage."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    msg = (first_message or "").strip()
    if not key or not msg:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": TITLE_MODEL,
                    "max_tokens": 24,
                    "messages": [{"role": "user", "content": _TITLE_PROMPT + msg[:2000]}],
                },
            )
        resp.raise_for_status()
        data = resp.json()
        if usage_sink is not None and data.get("usage"):
            usage_sink.append({"model": TITLE_MODEL, "usage": data["usage"]})
        parts = data.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        title = text.strip().strip('"“”\'').rstrip(".").strip()
        return title[:60] or None
    except Exception as e:
        logger.warning("Title generation failed: %s", e)
        return None


# -- Lifecycle stage classification --------------------------------
# Cheap, best-effort, post-stream — mirrors generate_title (haiku, direct
# Anthropic call). Classifies where the strategy build is in its lifecycle so
# the workspace stepper can advance. On any failure the stepper keeps its value.
_LIFECYCLE_PROMPT = (
    "You classify what a user is doing in an AI quant workspace and where it is "
    "in its lifecycle, so the UI can show the right workflow.\n\n"
    "1) INTENT — what the user is doing. Common intents:\n"
    "   - chat: a general question or discussion (no artifact).\n"
    "   - research: one-off data analysis / exploration (a report, not a deployable artifact).\n"
    "   - signal: building a reusable trading-signal generator.\n"
    "   - algo: building a full trading strategy.\n"
    "   - dashboard: building an analytics dashboard or other non-trading tool.\n"
    "   Intents are NOT limited to this list — if the user is clearly doing\n"
    "   something else, name it in one lower-case word.\n\n"
    "2) TRACK — the ordered lifecycle stages for this intent:\n"
    "   - chat / research: [] (no build lifecycle).\n"
    "   - algo / signal: [\"Explore\",\"Design\",\"Build\",\"Backtest\",\"Validate\",\"Deploy\"].\n"
    "   - dashboard / other non-trading build: [\"Explore\",\"Design\",\"Build\",\"Ship\"].\n"
    "   - a novel build intent: compose a sensible ordered track, starting with \"Explore\".\n\n"
    "3) STAGE — the ONE stage in the track the work is at NOW (\"\" if the track is empty).\n\n"
    "Reply with ONLY a JSON object: "
    "{\"intent\":\"...\",\"track\":[...],\"stage\":\"...\"}.\n\n"
)


async def classify_lifecycle(prior: dict, user_message: str, assistant_text: str,
                             usage_sink: Optional[list] = None) -> Optional[dict]:
    """Classify the project's intent + lifecycle track + current stage, returning
    {"intent","track","stage"} or None on any failure (caller keeps prior). The
    agent owns the lifecycle: it infers the intent and the ordered track for it
    (open vocabulary), and the frontend renders whatever track it is given. If
    `usage_sink` is given, this call's token usage is appended (attributed to the
    sidecar model) -- it runs outside the agent SDK session, so it's not in
    ResultMessage.model_usage."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        convo = (f"Prior intent: {(prior or {}).get('intent') or 'chat'}\n"
                 f"Prior stage: {(prior or {}).get('stage') or ''}\n"
                 f"User: {(user_message or '')[:1500]}\n"
                 f"Assistant: {(assistant_text or '')[:1500]}")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": TITLE_MODEL,
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": _LIFECYCLE_PROMPT + convo}],
                },
            )
        resp.raise_for_status()
        payload = resp.json()
        if usage_sink is not None and payload.get("usage"):
            usage_sink.append({"model": TITLE_MODEL, "usage": payload["usage"]})
        parts = payload.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        try:
            data = json.loads(text)
        except Exception:
            i, j = text.find("{"), text.rfind("}")
            data = json.loads(text[i:j + 1]) if (i >= 0 and j > i) else None
        if not isinstance(data, dict):
            return None
        intent = str(data.get("intent") or conversations.DEFAULT_INTENT).strip().lower()
        track = data.get("track")
        if not isinstance(track, list):
            track = conversations.track_for_intent(intent)
        track = [str(s).strip() for s in track if str(s).strip()]
        stage = str(data.get("stage") or "").strip()
        if stage and stage not in track:
            stage = track[0] if track else ""
        return {"intent": intent, "track": track, "stage": stage}
    except Exception as e:
        logger.warning("Lifecycle classification failed: %s", e)
        return None


# -- Satisfaction analysis (inferred sidecar) ----------------------
# A cheap Haiku sidecar (like classify_lifecycle) that infers a 1-5 satisfaction
# rank + short reasons from the recent transcript, run post-stream and reported
# to accounts as source='inferred'. Only the DERIVED signal leaves the sandbox,
# never the raw conversation, so it is privacy-safe regardless of consent.
_SATISFACTION_PROMPT = (
    "You gauge how SATISFIED a user is with Yukti (an AI that builds trading "
    "strategies for them), from their conversation. Weigh the user's tone, whether "
    "their requests are being met, friction or rework, and any praise or "
    "complaints. Reply with ONLY a JSON object, no markdown fences and no other "
    "text:\n"
    '{"rank": <integer 1-5, where 5=delighted, 3=neutral, 1=very frustrated>, '
    '"reasons": "<one or two short plain sentences on WHY, no jargon>"}\n\n'
    "Conversation (most recent last):\n\n"
)


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a model reply, tolerant of fences/prose."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _recent_transcript(messages: list, limit: int = 12) -> str:
    """A compact plain transcript of the last few turns, for the satisfaction
    analyzer. Roles are labelled User/Yukti; each turn is length-capped."""
    lines = []
    for m in (messages or [])[-limit:]:
        who = "User" if m.get("role") == "user" else "Yukti"
        content = (m.get("content") or "").strip()
        if content:
            lines.append(f"{who}: {content[:1500]}")
    return "\n".join(lines)


async def analyze_satisfaction(transcript: str, usage_sink: Optional[list] = None) -> Optional[dict]:
    """Infer the user's satisfaction (1-5) + a short reason from the conversation,
    via a cheap direct model call (like classify_lifecycle). Best-effort: returns
    {"rank": int, "reasons": str} or None on any failure / unparseable reply."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not transcript.strip():
        return None
    content = _SATISFACTION_PROMPT + transcript[:6000]
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": TITLE_MODEL,
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": content}],
                },
            )
        resp.raise_for_status()
        data = resp.json()
        if usage_sink is not None and data.get("usage"):
            usage_sink.append({"model": TITLE_MODEL, "usage": data["usage"]})
        parts = data.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        obj = _extract_json_object(text)
        if not obj:
            return None
        rank = int(obj.get("rank") or 0)
        if rank < 1 or rank > 5:
            return None
        return {"rank": rank, "reasons": str(obj.get("reasons") or "").strip()}
    except Exception as e:
        logger.warning("Satisfaction analysis failed: %s", e)
        return None


# -- Usage tracking ------------------------------------------------
# Per (stage × model) token/cost/tool usage. Accumulated into the strategy's
# meta (drives the workspace telemetry, survives reload via /history) AND
# reported to accounts (billing + the hosted-tier quota meter). The accounts
# report forwards the user's own JWT, so it writes as self-or-admin — no new
# secret.

def _usage_get(u, key):
    """Read a token field from the SDK usage object (dict or attr-bearing)."""
    if u is None:
        return None
    if isinstance(u, dict):
        return u.get(key)
    return getattr(u, key, None)


def _usage_delta(metrics: dict, tool_calls: int) -> dict:
    """Build the turn's usage delta from the stashed ResultMessage metrics."""
    u = metrics.get("usage")

    def g(*keys):
        for k in keys:
            v = _usage_get(u, k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
        return 0

    cost = metrics.get("cost_usd") or 0.0
    try:
        cost_micros = int(round(float(cost) * 1_000_000))
    except (TypeError, ValueError):
        cost_micros = 0
    return {
        "tokens_in": g("input_tokens"),
        "tokens_out": g("output_tokens"),
        "cache_read": g("cache_read_input_tokens"),
        "cache_create": g("cache_creation_input_tokens"),
        "cost_micros": cost_micros,
        "tool_calls": int(tool_calls or 0),
        "turns": int(metrics.get("num_turns") or 0),
    }


def _usage_delta_from_model_entry(entry: dict) -> dict:
    """Build a usage delta from one model's `ResultMessage.model_usage` entry.

    `model_usage` is the CLI's `modelUsage` map (model id -> per-model totals)
    passed through verbatim, so keys are camelCase and the figures are
    CUMULATIVE for the whole turn across every internal step of the agentic
    loop -- unlike the flat `usage`, which reflects only the final model call.
    Each entry carries this model's own `costUSD` (its slice of
    total_cost_usd). tool_calls/turns are turn-level; the caller adds them to
    the primary model only.
    """
    def n(*keys):
        for k in keys:
            v = _usage_get(entry, k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
        return 0

    cost = (_usage_get(entry, "costUSD")
            or _usage_get(entry, "cost_usd")
            or _usage_get(entry, "cost") or 0.0)
    try:
        cost_micros = int(round(float(cost) * 1_000_000))
    except (TypeError, ValueError):
        cost_micros = 0
    return {
        "tokens_in": n("inputTokens", "input_tokens"),
        "tokens_out": n("outputTokens", "output_tokens"),
        "cache_read": n("cacheReadInputTokens", "cache_read_input_tokens"),
        "cache_create": n("cacheCreationInputTokens", "cache_creation_input_tokens"),
        "cost_micros": cost_micros,
        "tool_calls": 0,
        "turns": 0,
    }


# The token/cost fields folded into a turn's roll-up. tool_calls/turns are
# excluded -- they aren't token spend, and the per-turn reply badge is about
# "how much did this answer cost".
_TURN_USAGE_FIELDS = ("tokens_in", "tokens_out", "cache_read", "cache_create", "cost_micros")


def _accumulate_turn_usage(acc: dict, delta: dict) -> None:
    """Sum one (stage × model or sidecar) delta into the whole-turn roll-up that
    gets tagged onto the final reply (all work done + the reply)."""
    for k in _TURN_USAGE_FIELDS:
        acc[k] = int(acc.get(k, 0)) + int(delta.get(k, 0) or 0)


def _model_label(model: str) -> str:
    """The model id to attribute usage to (the configured DATAFYE_AGENT_MODEL —
    an alias like 'opus' or a full id); the frontend prettifies it."""
    return (model or "").strip() or "unknown"


def _turn_index(conversation_id: str) -> int:
    """A stable per-conversation turn number (count of assistant messages),
    used with the session id to form the idempotency key."""
    rec = conversations.get(conversation_id) or {}
    msgs = rec.get("messages") or []
    return sum(1 for m in msgs if (m or {}).get("role") == "assistant")


async def _report_usage_to_accounts(conversation_id: str, stage: str, model: str,
                                    delta: dict, idem: str, auth_token: Optional[str]) -> None:
    """Best-effort: POST the turn's usage delta to accounts, forwarding the
    user's JWT. Meta accumulation already happened, so a failure here only misses
    the billing roll-up — never the workspace display, never the turn."""
    if not auth_token or not AGENT_USERNAME:
        return  # no forwarded identity (e.g. a self-hosted local run) — meta-only
    # Report for any project id: accounts is the authority. Registered projects
    # (accounts-minted "proj-" AND browser-local ids that were reconciled into the
    # registry) are accepted; an unregistered id yields a 404 that this best-effort
    # call just logs. (Was gated to "proj-" only, which wrongly dropped usage for
    # reconciled projects.)
    url = (f"{auth.ACCOUNTS_URL}/datafye-accounts-api/v1/accounts/"
           f"{AGENT_USERNAME}/projects/{conversation_id}/usage")
    body = {"idempotency_key": idem, "stage": stage, "model": model}
    body.update(delta)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=body,
                                     headers={"Authorization": f"Bearer {auth_token}"})
            if resp.status_code >= 400:
                logger.warning("Usage report to accounts returned %s for %s",
                               resp.status_code, conversation_id)
    except Exception as e:
        logger.warning("Usage report to accounts failed for %s: %s", conversation_id, e)


async def _report_satisfaction_to_accounts(conversation_id: str, rank: int, reasons: str,
                                           source: str, auth_token: Optional[str]) -> None:
    """Best-effort: POST the project's satisfaction (rank 1-5 + salient reasons +
    source) to accounts, forwarding the user's JWT. Only the DERIVED signal is
    sent, never the raw conversation, so it is privacy-safe regardless of the
    analytics-consent gate. A "user" source is sticky on the accounts side."""
    if not auth_token or not AGENT_USERNAME:
        return
    # Report for any registered project (proj- OR a reconciled browser-local id);
    # accounts 404s an unregistered id and this best-effort call just logs it.
    url = (f"{auth.ACCOUNTS_URL}/datafye-accounts-api/v1/accounts/"
           f"{AGENT_USERNAME}/projects/{conversation_id}/satisfaction")
    body = {"rank": int(rank), "reasons": reasons or "", "source": source}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=body,
                                     headers={"Authorization": f"Bearer {auth_token}"})
            if resp.status_code >= 400:
                logger.warning("Satisfaction report returned %s for %s",
                               resp.status_code, conversation_id)
    except Exception as e:
        logger.warning("Satisfaction report failed for %s: %s", conversation_id, e)


def _build_reporting_mcp(auth_token: Optional[str], conversation_id: Optional[str]):
    """In-process tools the model can call to report to accounts, forwarding the
    user's own JWT (the self-host-safe channel usage reporting uses; accounts, not
    the agent, holds the Slack/Linear creds):
      - `submit_feedback`: log feedback in-conversation (vs the app's button).
      - `submit_satisfaction`: record an EXPLICIT user satisfaction rating.
    Returns None when routing isn't possible (no forwarded identity, e.g. a
    self-hosted run), so the tools simply aren't offered."""
    if not (auth_token and AGENT_USERNAME and getattr(auth, "ACCOUNTS_URL", None)):
        return None

    @tool(
        "submit_feedback",
        "Log the user's feedback about Datafye to the team. Use ONLY after the "
        "user has agreed to you logging it for them. `category` is one of "
        "'suggestion', 'bug', or 'general'. `message` is the feedback in the "
        "user's words (summarize if long). `context` is optional extra detail "
        "(what they were doing, the Datafye topic); pass an empty string if none. "
        "Bugs and suggestions also open a tracking ticket.",
        {"category": str, "message": str, "context": str},
    )
    async def submit_feedback(args):
        category = (args.get("category") or "general").strip().lower()
        message = (args.get("message") or "").strip()
        context = (args.get("context") or "").strip()
        if not message:
            return {"content": [{"type": "text", "text": "No feedback text was provided, so nothing was logged."}]}
        url = (f"{auth.ACCOUNTS_URL}/datafye-accounts-api/v1/accounts/"
               f"{AGENT_USERNAME}/feedback")
        body = {"category": category, "message": message}
        if context:
            body["context"] = context
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=body,
                                         headers={"Authorization": f"Bearer {auth_token}"})
            if resp.status_code // 100 == 2:
                data = resp.json() if resp.content else {}
                # accounts returns the created tracking-issue id under "ticket"
                # (provider-neutral; "jira" kept as a fallback for older builds).
                ticket = data.get("ticket") or data.get("jira")
                text = "Logged. Thanks for the feedback."
                if ticket:
                    text += f" A tracking ticket was opened ({ticket})."
                return {"content": [{"type": "text", "text": text}]}
            logger.warning("Feedback submit returned %s", resp.status_code)
            return {"content": [{"type": "text", "text": "Could not log the feedback right now."}]}
        except Exception as e:
            logger.warning("Feedback submit failed: %s", e)
            return {"content": [{"type": "text", "text": "Could not reach the feedback service right now."}]}

    @tool(
        "submit_satisfaction",
        "Record the user's EXPLICIT satisfaction rating for THIS project, when they "
        "actually express one (e.g. 'I love it', 'that's exactly right', 'this is "
        "frustrating'). `rank` is an integer 1-5 (5=delighted, 3=neutral, 1=very "
        "frustrated). `reasons` is a short plain note on why, in the user's words. "
        "Use ONLY for a rating the user genuinely expressed -- it takes precedence "
        "over the agent's own read. Do not fish for a rating; capture it when it's given.",
        {"rank": int, "reasons": str},
    )
    async def submit_satisfaction(args):
        try:
            rank = int(args.get("rank") or 0)
        except (TypeError, ValueError):
            rank = 0
        if rank < 1 or rank > 5:
            return {"content": [{"type": "text", "text": "A rating must be 1 to 5, so nothing was recorded."}]}
        reasons = (args.get("reasons") or "").strip()
        if conversation_id:
            conversations.set_satisfaction(conversation_id, rank, reasons, "user")
        await _report_satisfaction_to_accounts(conversation_id or "", rank, reasons, "user", auth_token)
        return {"content": [{"type": "text", "text": "Thanks, I've noted your rating."}]}

    return create_sdk_mcp_server("feedback", "1.0.0", tools=[submit_feedback, submit_satisfaction])


def _tool_commentary(tool: str, tool_input: dict):
    """A sanitized, high-level activity line for a tool call as
    (text, level), or None to skip it.

    Deliberately generic — NO file paths, commands, or source identifiers are
    surfaced. The activity panel exists to signal that the agent is working;
    it is not a debug log. Levels drive the panel's colour-coding: "muted"
    (routine, dim scrolling), "notable" (environment work, emphasised),
    "error" (a step failed). The same shape is used across the agents.
    """
    if tool in ("Read", "NotebookRead"):
        return ("Reading reference material", "muted")
    if tool in ("Grep", "Glob"):
        return ("Searching for relevant details", "muted")
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return ("Updating a file in the workspace", "muted")
    if tool == "Bash":
        cmd = ((tool_input or {}).get("command") or "").lower()
        if any(k in cmd for k in ("backtest", "paper", "validate", "test", "pytest")):
            return ("Running the backtest", "check")
        return ("Running a workspace command", "muted")
    if tool in ("WebFetch", "WebSearch"):
        return ("Looking something up online", "muted")
    if tool == "Task":
        return ("Working through a sub-task", "muted")
    if tool == "TodoWrite":
        return ("Planning the next steps", "muted")
    if tool.startswith("mcp__datafye-api__"):
        op = tool.split("mcp__datafye-api__", 1)[1].lower()
        if any(k in op for k in ("backtest", "validate", "paper", "test")):
            return ("Validating against market data", "check")
        if any(k in op for k in ("provision", "deploy", "create", "morph")):
            return ("Setting up the trading environment", "notable")
        return ("Working in the Datafye environment", "muted")
    if tool.startswith("mcp__"):
        return ("Using a connected tool", "notable")
    return None


# The raw command behind a tool call, for the persisted Tool Detail (mirrors the
# frontend's toolCommandText). Distinct from _tool_commentary: that is the
# sanitized panel line ("Reading reference material"); this is the exact command
# ("Read /path/x.py") revealed under the Tool Detail toggle. Persisted with its
# (size-capped) output so the detail survives a reload and shows in accounts.
_DETAIL_OUTPUT_CAP = 2000  # chars of tool output persisted per step

def _tool_command_text(tool: str, tool_input: dict) -> str:
    ti = tool_input or {}
    if tool == "Bash":
        return ti.get("command") or "(bash)"
    if tool in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "NotebookRead"):
        return (tool + " " + (ti.get("file_path") or ti.get("notebook_path") or "")).strip()
    if tool == "Grep":
        return "Grep " + (ti.get("pattern") or "") + (" in " + ti["path"] if ti.get("path") else "")
    if tool == "Glob":
        return "Glob " + (ti.get("pattern") or "")
    if tool in ("WebFetch", "WebSearch"):
        return tool + " " + (ti.get("url") or ti.get("query") or "")
    try:
        args = json.dumps(ti)
    except Exception:
        args = ""
    name = tool[len("mcp__"):] if tool.startswith("mcp__") else tool
    return (name + (" " + args if args and args != "{}" else "")).strip()


def _is_env_changing_tool(tool: str, tool_input: dict) -> bool:
    """True if a tool call is likely to CHANGE the deployed environment (provision,
    apply, dataset add/remove, deprovision, morph, start/stop). The streamer emits
    a transitioning env_status when one starts and re-reads the environment right
    after it finishes -- so the workspace env panel updates the moment the agent
    spins up / reconfigures an environment, instead of waiting for turn end (which
    on a long turn can be minutes later)."""
    _ENV_VERBS = ("apply", "provision", "deprovision", "dataset", "morph",
                  "upgrade", "start", "stop", "deploy", "create")
    if tool == "Bash":
        cmd = ((tool_input or {}).get("command") or "").lower()
        if ("foundry local" in cmd or "trading local" in cmd) and any(v in cmd for v in _ENV_VERBS):
            return True
        return False
    if tool.startswith("mcp__datafye-api__"):
        op = tool.split("mcp__datafye-api__", 1)[1].lower()
        return any(v in op for v in _ENV_VERBS)
    return False


async def _fetch_deployment_state() -> Optional[dict]:
    """Best-effort snapshot of the running Datafye environment.

    Hits the deployment REST API (GET .../deployment/{descriptor,datasets,
    symbols}) on the same datafye-api service the MCP server fronts. Returns
    a dict {descriptor_text, descriptor, datasets, symbols} on success, or
    None when no environment is up (connection refused / 404 / no descriptor)
    — the caller treats None as "emit nothing".

    The descriptor is the load-bearing call; datasets and symbols are
    enrichment and a failure on either is tolerated (left empty)."""
    base = DATAFYE_DEPLOYMENT_API_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{base}/datafye-api/v1/deployment/descriptor")
            if resp.status_code != 200:
                return None
            descriptor_text = (resp.json() or {}).get("descriptor", "")
            if not descriptor_text or not descriptor_text.strip():
                return None
            try:
                descriptor = yaml.safe_load(descriptor_text) or {}
            except yaml.YAMLError as e:
                logger.warning("Could not parse deployment descriptor YAML: %s", e)
                return None

            datasets: list = []
            symbols: dict = {}
            try:
                dr = await client.get(f"{base}/datafye-api/v1/deployment/datasets")
                if dr.status_code == 200:
                    datasets = (dr.json() or {}).get("datasets", []) or []
            except httpx.HTTPError:
                pass
            try:
                sr = await client.get(f"{base}/datafye-api/v1/deployment/symbols")
                if sr.status_code == 200:
                    symbols = (sr.json() or {}).get("symbols", {}) or {}
            except httpx.HTTPError:
                pass

            return {
                "descriptor_text": descriptor_text,
                "descriptor": descriptor,
                "datasets": datasets,
                "symbols": symbols,
            }
    except httpx.HTTPError:
        # connection refused / timeout — no environment is up. Emit nothing.
        return None
    except Exception as e:
        logger.warning("Could not read deployment state: %s", e)
        return None


def _derive_env_status(state: dict) -> dict:
    """Derive the frontend-facing env_status payload from a deployment state
    snapshot (the output of _fetch_deployment_state).

    Shape: {status, env_type, datasets, symbols, broker, mode}
      - mode     — the descriptor's `mode` ("backtest" | "paper")
      - env_type — "Foundry" for backtest, "Trading" for paper. Named
                   `env_type`, not `type`, so it does not collide with the
                   SSE frame's own `type` discriminator that sse_event sets.
      - datasets — dataset names (live deployment list if present, else the
                   descriptor's datasets section)
      - symbols  — union of tickers across the descriptor's datasets sections
      - broker   — the descriptor's broker.provider, or None
    """
    descriptor = state.get("descriptor") or {}
    mode = descriptor.get("mode")
    type_ = {"backtest": "Foundry", "paper": "Trading"}.get(mode, "Foundry")

    descriptor_datasets = descriptor.get("datasets") or []
    datasets = state.get("datasets") or [
        d.get("dataset") for d in descriptor_datasets if d.get("dataset")
    ]

    symbols: list = []
    for d in descriptor_datasets:
        tickers = ((d.get("symbols") or {}).get("tickers")) or []
        for t in tickers:
            if t not in symbols:
                symbols.append(t)

    broker = (descriptor.get("broker") or {}).get("provider")

    return {
        "status": "running",
        "env_type": type_,
        "datasets": datasets,
        "symbols": symbols,
        "broker": broker,
        "mode": mode,
    }


async def tracked_stream_agent_response(
    message: str,
    conversation_id: Optional[str],
    algo_id: Optional[str],
    auth_token: Optional[str] = None,
) -> AsyncIterator[str]:
    """Wraps stream_agent_response with running_jobs + lastChatActivityAt
    bookkeeping. Increments running_jobs at stream start, decrements at end
    (even on error), so /health reports an accurate live-job count for
    accounts' idle monitor."""
    global last_chat_activity_at, running_jobs
    last_chat_activity_at = int(time.time() * 1000)
    running_jobs += 1
    try:
        async for event in stream_agent_response(message, conversation_id, algo_id, auth_token):
            yield event
    finally:
        running_jobs -= 1


# -- Agent Streaming -----------------------------------------------

async def stream_agent_response(
    message: str,
    conversation_id: Optional[str],
    algo_id: Optional[str],
    auth_token: Optional[str] = None,
) -> AsyncIterator[str]:
    """Stream responses from Claude Agent SDK with structured SSE events."""
    global anthropic_key_status

    # Each strategy is its own folder, and that folder is the cwd + workspace
    # for its chat turns: the agent's files, its per-strategy CLAUDE.md memory,
    # and its per-strategy .claude/skills all live there. ensure() materialises
    # the folder for an accounts-minted id (the accounts service is the
    # authoritative project registry; it mints the id, the agent follows).
    # Conversation-less (legacy/fallback) requests use the shared workspace.
    if conversation_id:
        conversations.ensure(conversation_id)
        cwd = str(conversations.strategy_dir(conversation_id))
    else:
        cwd = WORKSPACE_DIR

    mcp_servers, allowed_tools = build_mcp_config()
    # Offer the in-conversation reporting tools (feedback + explicit satisfaction)
    # only when we can route them (platform user with a forwarded JWT); a
    # self-hosted run without accounts skips them.
    reporting_server = _build_reporting_mcp(auth_token, conversation_id)
    if reporting_server is not None:
        mcp_servers["feedback"] = reporting_server
        allowed_tools.append("mcp__feedback__submit_feedback")
        allowed_tools.append("mcp__feedback__submit_satisfaction")
    system_prompt = build_system_prompt(
        docs_dir=DOCS_DIR,
        cli_path=CLI_PATH,
        workspace_dir=cwd,
        samples_dir=SAMPLES_DIR,
        credential_summary=get_credential_summary(),
        algo_id=algo_id,
        # Cross-session memory: global notes/index + this strategy's memory index.
        # Per-strategy CLAUDE.md is auto-loaded by the SDK (project source).
        memory_context=memory.build_memory_context(cwd if conversation_id else None),
        # Where to write user-authored skills (the author-skill skill uses this).
        skills_dir=str(skills.user_global_skills_dir()),
        # Index of the user's uploaded context files (name/type/size); bodies are
        # read on demand from uploads/ — never inlined into the prompt.
        files_context=conversations.build_files_context(conversation_id),
        # On-disk path to the foundry resource-cost cheat sheet (for the resource
        # guard); empty if the bundled file is missing.
        cheatsheet_path=CHEATSHEET_PATH if os.path.exists(CHEATSHEET_PATH) else "",
    )

    options = ClaudeAgentOptions(
        model=CLAUDE_MODEL,
        cwd=cwd,
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        mcp_servers=mcp_servers if mcp_servers else None,
        allowed_tools=allowed_tools,
        # System (read-only) + user-global skills, as local plugins. Rebuilt
        # per turn so a skill the agent authors mid-session is live next turn.
        plugins=skills.build_plugins(),
        # Load the strategy folder's own context: its CLAUDE.md (per-strategy
        # memory) and its .claude/skills (per-strategy user skills). "project"
        # is the cwd's .claude; we deliberately do NOT load "user"/"local".
        setting_sources=["project"],
        include_partial_messages=True,
    )

    # Persist the user's turn and resume the strategy's SDK session.
    # get_sdk_session is read from disk so resume survives an agent restart;
    # the in-memory `sessions` map covers strategies not in the store
    # (a frontend running in local-only fallback mode).
    # Detect the first turn of a new conversation (no prior messages) BEFORE we
    # append this one — used below to summarize the first ask into a title.
    is_first_turn = False
    if conversation_id:
        _existing = conversations.get(conversation_id)
        is_first_turn = not (_existing and _existing.get("messages"))
        conversations.append_message(conversation_id, "user", message)
        resume_id = conversations.get_sdk_session(conversation_id) or sessions.get(conversation_id)
        if resume_id:
            options.resume = resume_id
            logger.info(f"Resuming session for conversation {conversation_id}")

    logger.info(f"[TRACE] === Starting Agent Query ===")
    logger.info(f"[TRACE] Model: {CLAUDE_MODEL}")
    logger.info(f"[TRACE] Algo: {algo_id}")
    logger.info(f"[TRACE] Conversation: {conversation_id}")
    logger.info(f"[TRACE] Message: {truncate(message)}")
    logger.info(f"[TRACE] MCP servers: {list(mcp_servers.keys())}")

    # Snapshot the deliverables folder so we can announce (via `artifact`) any
    # NEW or CHANGED output file the agent writes this turn. Keyed name -> (size,
    # modified_at) so a re-written same-named file still counts as fresh.
    outputs_before = {}
    if conversation_id:
        outputs_before = {f["name"]: (f["size"], f["modified_at"])
                          for f in conversations.list_outputs(conversation_id)}

    try:
        msg_count = 0
        # Text the model writes BETWEEN tool calls is work-narration (its running
        # account of what it's doing) -> routed to the Work panel as detail
        # commentary. The final trailing burst (after the last tool) is its
        # message to the user -> the Conversation. We buffer the current burst and
        # route it on flush (at the next tool, or at turn end).
        pending_blocks = []      # unrouted text blocks, kept DISTINCT (one per
                                 # narration sentence / reply paragraph) so they
                                 # never glue into a run-on line on flush
        conversation_text = ""   # the final reply -> Conversation + persisted
        tool_calls_this_turn = 0
        # Running NEW-token count (fresh input+output, cache excluded) for the
        # live status-bar ticker. Each AssistantMessage carries this round's
        # usage; we accumulate and emit a `ticker` so the client shows tokens
        # climbing in real time. Reconciled to the authoritative `usage` event
        # at turn end (this is a live estimate, not the billed figure).
        ticker_tokens = 0
        # The id of an in-flight environment-changing tool (apply/provision/...);
        # its tool_result triggers a mid-turn deployment re-read so the env panel
        # updates as soon as the environment actually changes (not at turn end).
        pending_env_tool_id = None
        result_metrics = None  # stashed from ResultMessage; reported after stage classification

        async for msg in query(prompt=message, options=options):
            msg_count += 1

            # SystemMessage
            if isinstance(msg, SystemMessage):
                subtype = getattr(msg, 'subtype', None)
                data = getattr(msg, 'data', {}) or {}

                if subtype == 'init':
                    session_id = data.get('session_id')
                    if conversation_id and session_id:
                        sessions[conversation_id] = session_id
                        conversations.set_sdk_session(conversation_id, session_id)
                    yield sse_event('init', {'session_id': session_id})
                else:
                    yield sse_event('system', {'subtype': subtype, 'data': data})

            # AssistantMessage
            elif isinstance(msg, AssistantMessage) and msg.content:
                for block in msg.content:
                    # Text
                    if hasattr(block, 'text') and not hasattr(block, 'name'):
                        text = getattr(block, 'text', '')
                        if text:
                            # Buffer each block SEPARATELY; routed on flush (Work
                            # panel vs Conversation). Keeping blocks distinct (vs
                            # string concatenation) makes each narration sentence
                            # its own line instead of one run-on blob with the
                            # sentences jammed together at the periods.
                            pending_blocks.append(text)

                    # Thinking
                    elif hasattr(block, 'thinking'):
                        thinking = getattr(block, 'thinking', '')
                        if thinking:
                            yield sse_event('thinking', {'text': thinking})

                    # Tool use
                    elif hasattr(block, 'name') and hasattr(block, 'input'):
                        # The text just before this tool is the model narrating
                        # what it's about to do -- work-narration, not a message to
                        # the user. Route each block to the Work panel as ITS OWN
                        # line (one commentary entry per narration sentence).
                        for burst in pending_blocks:
                            burst = burst.strip()
                            if not burst:
                                continue
                            # Narration is the agent's OWN voice as it works ("I'll
                            # log these now, grouped into coherent tickets ..."), a
                            # distinct kind from the machine tool-labels below so
                            # the client can render it a shade brighter in the rail.
                            if conversation_id:
                                conversations.append_commentary(conversation_id, burst, "narration")
                            yield sse_event('commentary', {'text': burst, 'kind': 'narration'})
                        pending_blocks = []
                        tool_name = getattr(block, 'name', '')
                        tool_input = getattr(block, 'input', {})
                        tool_calls_this_turn += 1
                        yield sse_event('tool_use_start', {
                            'tool': tool_name,
                            'id': getattr(block, 'id', ''),
                            'input': tool_input,
                        })
                        # Translate notable tool calls into a human activity
                        # line for the workspace's commentary panel, and
                        # persist it as the conversation's audit trail.
                        note = _tool_commentary(tool_name, tool_input)
                        if note:
                            text, level = note
                            # Persist the label WITH its tool_id + the exact command,
                            # so the Tool Detail (command + output) replays from
                            # /history and shows in the accounts Conversation view.
                            # The output is attached later (at tool_result).
                            if conversation_id:
                                conversations.append_commentary(
                                    conversation_id, text, level,
                                    tool_id=getattr(block, 'id', ''),
                                    command=_tool_command_text(tool_name, tool_input))
                            yield sse_event('commentary', {'text': text, 'kind': level})

                        # An environment-changing tool just STARTED: show the panel
                        # a transitioning state (e.g. "Applying...") right away, and
                        # remember its id so we re-read the environment when it ends.
                        if _is_env_changing_tool(tool_name, tool_input):
                            pending_env_tool_id = getattr(block, 'id', '') or True
                            yield sse_event('env_status', {'status': 'transitioning'})

                    # Tool result
                    elif hasattr(block, 'tool_use_id'):
                        is_err = bool(getattr(block, 'is_error', False))
                        result_tool_id = getattr(block, 'tool_use_id', '')
                        result_content = str(getattr(block, 'content', '') or '')
                        yield sse_event('tool_result', {
                            'tool_use_id': result_tool_id,
                            'content': result_content,
                            'is_error': getattr(block, 'is_error', False)
                        })
                        # Attach the (capped) output to the tool's commentary entry
                        # so the persisted Tool Detail carries command + output.
                        if conversation_id and result_tool_id:
                            conversations.attach_tool_output(
                                conversation_id, result_tool_id,
                                result_content[:_DETAIL_OUTPUT_CAP], is_err)
                        if is_err:
                            err_text = 'A step reported an error'
                            if conversation_id:
                                conversations.append_commentary(conversation_id, err_text, 'error')
                            yield sse_event('commentary', {'text': err_text, 'kind': 'error'})

                        # An environment-changing tool just FINISHED: re-read the
                        # deployment now and push fresh descriptor + env_status, so
                        # the panel reflects the new environment mid-turn instead of
                        # waiting for turn end. Best-effort; never breaks the turn.
                        if pending_env_tool_id and (pending_env_tool_id is True
                                                    or pending_env_tool_id == result_tool_id):
                            pending_env_tool_id = None
                            try:
                                dep = await _fetch_deployment_state()
                                if dep:
                                    yield sse_event('descriptor', {'descriptor': dep['descriptor_text']})
                                    yield sse_event('env_status', _derive_env_status(dep))
                                else:
                                    yield sse_event('env_status', {
                                        'status': 'idle', 'env_type': None,
                                        'datasets': [], 'symbols': [], 'broker': None, 'mode': None,
                                    })
                            except Exception as e:
                                logger.warning("Mid-turn env read failed: %s", e)

                # This model round's usage -> accumulate NEW tokens and push a
                # live ticker so the status bar counts up in real time. Best-
                # effort; a round without usage just doesn't advance the ticker.
                mu = getattr(msg, 'usage', None)
                if isinstance(mu, dict):
                    ticker_tokens += (int(mu.get('input_tokens') or 0)
                                      + int(mu.get('output_tokens') or 0))
                    yield sse_event('ticker', {'tokens': ticker_tokens})

            # Stream events
            elif hasattr(msg, 'event'):
                yield sse_event('stream', {'event': getattr(msg, 'event', {})})

            # Result
            elif isinstance(msg, ResultMessage):
                # The final trailing blocks (no tool followed) are the reply ->
                # the Conversation. Join as paragraphs so a multi-block reply keeps
                # its breaks and consecutive blocks never glue together.
                conversation_text = "\n\n".join(
                    b.strip() for b in pending_blocks if b.strip())
                pending_blocks = []
                # Guarantee a closing message. If the turn ended on a tool call
                # with no trailing prose, conversation_text is empty -- fall back
                # to the SDK's own final result text so the Conversation always
                # gets a reply rather than a turn that ends silently on an action.
                if not conversation_text:
                    conversation_text = (getattr(msg, 'result', '') or '').strip()
                if conversation_text:
                    yield sse_event('content', {'text': conversation_text})
                    if conversation_id:
                        conversations.append_message(conversation_id, "assistant", conversation_text)
                yield sse_event('result', {
                    'text': getattr(msg, 'result', ''),
                    'session_id': getattr(msg, 'session_id', None),
                    'duration_ms': getattr(msg, 'duration_ms', None),
                    'cost_usd': getattr(msg, 'total_cost_usd', None),
                    'usage': getattr(msg, 'usage', None),
                    'num_turns': getattr(msg, 'num_turns', None),
                })
                # Stash the turn's usage; reported AFTER stage classification so
                # the delta is attributed to the stage this turn landed in.
                result_metrics = {
                    'session_id': getattr(msg, 'session_id', None),
                    'cost_usd': getattr(msg, 'total_cost_usd', None),
                    'usage': getattr(msg, 'usage', None),
                    # Per-model, whole-turn-CUMULATIVE usage (the CLI's modelUsage
                    # map). Preferred over the flat `usage`, which reflects only
                    # the final model call and so undercounts multi-step turns.
                    'model_usage': getattr(msg, 'model_usage', None),
                    'num_turns': getattr(msg, 'num_turns', None),
                }

        logger.info(f"[TRACE] Done. Messages processed: {msg_count}")

        # Surface the running environment's state to the frontend. The chat
        # turn may have provisioned, morphed, or torn down an environment, so
        # we read the deployment descriptor after the SDK loop finishes.
        # Best-effort: if no environment is up the snapshot is None and we
        # emit nothing.
        deployment_state = await _fetch_deployment_state()
        if deployment_state:
            # Raw descriptor text so the frontend can relay it to accounts.
            yield sse_event('descriptor', {'descriptor': deployment_state['descriptor_text']})
            # Derived environment status for the frontend's env display.
            yield sse_event('env_status', _derive_env_status(deployment_state))
        else:
            # No environment is up (torn down, switched away from, or not yet
            # reachable). Emit a CLEARED status so the panel doesn't keep showing a
            # stale environment from an earlier dataset (e.g. SIP after the user has
            # moved to a Crypto foundry). A running env re-asserts itself on the
            # next turn's read.
            yield sse_event('env_status', {
                'status': 'idle', 'env_type': None,
                'datasets': [], 'symbols': [], 'broker': None, 'mode': None,
            })

        # Collect the two cheap Haiku sidecars' token usage so it's counted in
        # the turn's per-model roll-up (they run outside the SDK session, so
        # they're absent from ResultMessage.model_usage).
        sidecar_usage: list = []

        # First turn of a new conversation: replace the provisional first-few-
        # words name with an LLM-summarized title. The app adopts it (sidebar +
        # accounts registry). Best-effort — a failure keeps the provisional name.
        if conversation_id and is_first_turn:
            title = await generate_title(message, sidecar_usage)
            if title:
                conversations.rename(conversation_id, title)
                yield sse_event('title', {'conversation_id': conversation_id, 'name': title})

        # Classify what the user is doing (intent), the lifecycle track for it,
        # and the current stage — so the workspace shows the right workflow.
        # Cheap, best-effort, post-stream. The agent owns the track; the frontend
        # renders whatever it is given (empty track => no stepper).
        if conversation_id:
            rec = conversations.get(conversation_id) or {}
            life = await classify_lifecycle(rec, message, conversation_text, sidecar_usage)
            if life:
                updated = conversations.set_intent_track(
                    conversation_id, life['intent'], life['track'], life['stage']) or {}
                yield sse_event('stage', {
                    'conversation_id': conversation_id,
                    'intent': updated.get('intent', life['intent']),
                    'track': updated.get('track', life['track']),
                    'stage': updated.get('stage', life['stage']),
                    'maxStage': updated.get('maxStage', life['stage']),
                })

        # Infer the user's satisfaction (best-effort, cheap) and report only the
        # derived rank + reasons to accounts, never the raw conversation, so this
        # is privacy-safe regardless of the analytics-consent gate. An explicit
        # user rating would be sticky and win over this (accounts enforces it).
        # Gate on a forwarded identity (platform user) rather than the id prefix,
        # so it runs for any registered project (incl. reconciled ids) and is
        # skipped for a self-hosted run with no accounts to report to.
        if conversation_id and auth_token and AGENT_USERNAME:
            rec2 = conversations.get(conversation_id) or {}
            transcript = _recent_transcript(rec2.get("messages") or [])
            sat = await analyze_satisfaction(transcript, sidecar_usage)
            if sat:
                conversations.set_satisfaction(conversation_id, sat["rank"], sat["reasons"], "inferred")
                await _report_satisfaction_to_accounts(
                    conversation_id, sat["rank"], sat["reasons"], "inferred", auth_token)

        # Record + report this turn's usage, attributed to the stage it landed
        # in and the model that ran it. Drives the workspace telemetry (meta,
        # replayed by /history) and the accounts billing/quota roll-up
        # (best-effort, forwarding the user's JWT). Never breaks the turn.
        if conversation_id and result_metrics:
            try:
                _rec_now = conversations.get(conversation_id) or {}
                # Fall back to the intent (then a generic label) when there is no
                # lifecycle stage: a research/chat project has an empty track, so
                # its stage is blank -- tag usage with the intent (e.g. "research")
                # instead of leaving it empty, which the UI renders as "unknown".
                stage_now = _rec_now.get('stage') or _rec_now.get('intent') or 'general'
                base_idem = f"{result_metrics.get('session_id') or 'nosess'}:{_turn_index(conversation_id)}"
                model_usage = result_metrics.get('model_usage')
                updated_usage = None
                # Whole-turn roll-up (across every model + the Haiku sidecars),
                # tagged onto the final reply so the accounts Conversation view
                # can show tokens/cost per turn = all work done + the reply.
                turn_usage = {k: 0 for k in _TURN_USAGE_FIELDS}

                if isinstance(model_usage, dict) and model_usage:
                    # Preferred path: per-model, whole-turn-CUMULATIVE usage --
                    # every token type (incl. cache read/create) summed across
                    # the whole agentic loop, plus each model's own cost. One
                    # delta per (stage × model), idempotency-keyed per model so a
                    # multi-model turn (e.g. Opus main loop + an internal Haiku)
                    # lands in the right cells without double-counting on retry.
                    primary = _model_label(CLAUDE_MODEL)
                    attributed_extras = False
                    for model_id, entry in model_usage.items():
                        delta = _usage_delta_from_model_entry(entry if isinstance(entry, dict) else {})
                        # tool_calls + turns are turn-level, not per-model -- add
                        # them once, to the primary model (or the first model if
                        # the configured one didn't run this turn).
                        if not attributed_extras and (model_id == primary or primary not in model_usage):
                            delta['tool_calls'] = int(tool_calls_this_turn or 0)
                            delta['turns'] = int(result_metrics.get('num_turns') or 0)
                            attributed_extras = True
                        idem = f"{base_idem}:{model_id}"
                        _accumulate_turn_usage(turn_usage, delta)
                        updated_usage = conversations.add_usage(conversation_id, stage_now, model_id, delta, idem)
                        await _report_usage_to_accounts(conversation_id, stage_now, model_id, delta, idem, auth_token)
                    logger.info("[usage] %s: turn attributed across %d model(s): %s",
                                conversation_id, len(model_usage), list(model_usage.keys()))
                else:
                    # Fallback (CLI emitted no modelUsage): the flat single-usage
                    # read + total cost, attributed to the configured model. This
                    # undercounts multi-step turns -- kept only so usage tracking
                    # degrades gracefully rather than vanishing.
                    delta = _usage_delta(result_metrics, tool_calls_this_turn)
                    model_id = _model_label(CLAUDE_MODEL)
                    idem = f"{base_idem}:{model_id}"
                    _accumulate_turn_usage(turn_usage, delta)
                    updated_usage = conversations.add_usage(conversation_id, stage_now, model_id, delta, idem)
                    await _report_usage_to_accounts(conversation_id, stage_now, model_id, delta, idem, auth_token)

                # Fold in the Haiku sidecars (title + lifecycle classification).
                # They bill separately from the SDK session, so they're not in
                # model_usage. Tokens are captured; cost is 0 (the direct API
                # response carries no cost) -- negligible, derived later if wanted.
                for i, sc in enumerate(sidecar_usage):
                    sc_model = _model_label(sc.get('model') or TITLE_MODEL)
                    sc_delta = _usage_delta_from_model_entry(sc.get('usage') or {})
                    sc_idem = f"{base_idem}:sidecar:{i}:{sc_model}"
                    _accumulate_turn_usage(turn_usage, sc_delta)
                    updated_usage = conversations.add_usage(conversation_id, stage_now, sc_model, sc_delta, sc_idem)
                    await _report_usage_to_accounts(conversation_id, stage_now, sc_model, sc_delta, sc_idem, auth_token)

                # Tag the final reply with this whole turn's usage (all work +
                # the reply). Best-effort; the reply message exists only if the
                # turn produced trailing text. Additive field on the message ->
                # passes through /history for the accounts Conversation view.
                if conversation_text:
                    conversations.set_last_message_usage(conversation_id, turn_usage)

                # Hand the frontend the authoritative cumulative usage so its
                # status bar + per-(stage × model) stepper badges reconcile to
                # the agent's figures (no client-side arithmetic drift).
                if updated_usage is not None:
                    yield sse_event('usage', {
                        'conversation_id': conversation_id,
                        'usage': updated_usage,
                        'stage': stage_now,
                        'model': _model_label(CLAUDE_MODEL),
                    })
            except Exception as e:
                logger.warning("Usage tracking failed for %s: %s", conversation_id, e)

        # Announce any deliverables the agent produced this turn so the download
        # UI can offer them. Best-effort; never breaks the turn.
        if conversation_id:
            try:
                for f in conversations.list_outputs(conversation_id):
                    before = outputs_before.get(f["name"])
                    if before == (f["size"], f["modified_at"]):
                        continue   # unchanged since the turn started
                    yield sse_event("artifact", {
                        "conversation_id": conversation_id,
                        "name": f["name"],
                        "type": f["type"],
                        "size": f["size"],
                    })
            except Exception as e:
                logger.warning("Artifact announce failed for %s: %s", conversation_id, e)

        yield sse_event('done', {})

    except Exception as e:
        emsg = str(e).lower()
        if any(s in emsg for s in ("x-api-key", "authentication_error", "invalid api key", "401 unauthorized")):
            anthropic_key_status = "invalid"
            logger.warning("Anthropic call failed authentication — marking key invalid")
        logger.error(f"Agent error: {e}", exc_info=True)
        yield sse_event('error', {
            'message': str(e),
            'error_type': type(e).__name__
        })


# -- App Setup -----------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("Datafye Agent Service starting...")
    logger.info(f"  Model: {CLAUDE_MODEL}")
    logger.info(f"  Workspace: {WORKSPACE_DIR}")
    logger.info(f"  Docs dir: {DOCS_DIR}")
    logger.info(f"  CLI path: {CLI_PATH}")
    logger.info("  Awaiting accounts bootstrap (identity, credentials, Anthropic key)")

    docs_available = os.path.isdir(DOCS_DIR)
    samples_available = os.path.isdir(SAMPLES_DIR)
    logger.info(f"  Docs available: {docs_available}")
    logger.info(f"  Samples dir: {SAMPLES_DIR} (available: {samples_available})")

    # Skills: scaffold the writable user-skill plugin and report which plugin
    # dirs the SDK will load (system + user-global). Per-strategy skills are
    # wired in once the strategy folder becomes the cwd.
    skills.ensure_user_plugin()
    loaded_plugins = [p["path"] for p in skills.build_plugins()]
    logger.info(f"  Skill plugins: {loaded_plugins or 'none'}")

    # Memory: scaffold the global (cross-strategy) memory store. Per-strategy
    # memory is scaffolded per strategy folder by conversations.ensure().
    memory.ensure_global_memory()
    logger.info(f"  Global memory: {memory.GLOBAL_DIR}")

    if check_api_mcp_reachable(DATAFYE_API_MCP_URL):
        logger.info(f"  Datafye API MCP: reachable at {DATAFYE_API_MCP_URL}")
    else:
        logger.warning(
            f"  Datafye API MCP: NOT REACHABLE at {DATAFYE_API_MCP_URL}. "
            f"Agent will start, but tool calls requiring the deployment will fail. "
            f"Check the foundry environment: datafye foundry local status"
        )

    yield
    logger.info("Datafye Agent Service shutting down...")


app = FastAPI(
    title="Datafye Agent Service",
    description="AI-powered algo development assistant",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if "*" not in ALLOWED_ORIGINS else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- Bootstrap gate ------------------------------------------------

async def require_bootstrapped() -> None:
    """FastAPI dependency: 503 until the accounts bootstrap push has
    established the agent's identity + credentials store. Applied to every
    user-facing surface so nothing runs against a None identity/store."""
    if not _bootstrapped:
        raise HTTPException(
            status_code=503,
            detail="Agent is awaiting bootstrap from the accounts service",
        )


# broker router — shares the credentials store (set in /bootstrap) so pushes
# via /v1/credentials/update stay visible and lazy-provisioned ConnectTrade
# user creds flow back into it. Gated on bootstrap like all user surfaces.
app.include_router(broker.router, dependencies=[Depends(require_bootstrapped)])


# -- Endpoints -----------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check. Always available — including before bootstrap, so the
    accounts poll loop can read `bootstrapped` and decide whether to push."""
    import shutil
    creds = credentials  # None until bootstrapped
    return HealthResponse(
        status="healthy",
        bootstrapped=_bootstrapped,
        configured=(anthropic_key_status != "missing"),
        anthropic_key_status=anthropic_key_status,
        workspace=WORKSPACE_DIR,
        docs_available=os.path.isdir(DOCS_DIR),
        cli_available=shutil.which(CLI_PATH) is not None,
        api_mcp_available=check_api_mcp_reachable(DATAFYE_API_MCP_URL),
        credentials={
            "massive": bool(creds.get("massive_api_key")),
            "precision_alpha": bool(creds.get("palpha_api_key")),
            "hwai": bool(creds.get("hwai_api_key")),
            "connecttrade": all([
                creds.get("connecttrade_client_id"),
                creds.get("connecttrade_client_secret"),
                creds.get("connecttrade_user_id"),
                creds.get("connecttrade_user_secret"),
            ]),
            "github": bool(creds.get("github_user") and creds.get("github_token")),
        } if creds else {},
        username=AGENT_USERNAME,
        credentials_generation=creds.generation() if creds else None,
        last_chat_activity_at=last_chat_activity_at,
        running_jobs=running_jobs,
        active_proxied_apps=active_proxied_apps,
    )


BOM_PATH = os.getenv("DATAFYE_AGENT_BOM_PATH", "/opt/datafye/agent/bom.json")


@app.get("/v1/bom")
async def bom():
    """Dependency bill-of-materials — the Datafye version this agent is built
    against. Datafye versions all components (platform, samples, CLI, docs)
    together, so it's a single version. Unauthenticated like /health (version
    numbers aren't sensitive); rendered on the Yukti agent surface."""
    try:
        with open(BOM_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"agent_version": os.getenv("DATAFYE_AGENT_VERSION", "dev"), "dependencies": {}, "note": "bom.json not present"}
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"could not read BOM: {e}")


@app.post("/bootstrap")
async def bootstrap(authorization: Optional[str] = Header(default=None)):
    """Bootstrap the agent — called by the accounts service once the
    instance is reachable. The Authorization header carries an
    accounts-signed JWT (purpose=agent-bootstrap) whose claims are the
    agent's identity (`user_id`) and its credentials-store key
    (`creds_key`). On success the agent configures auth, opens its
    encrypted credentials store, and leaves the awaiting-bootstrap state.

    Idempotent for the same user (the reconcile loop re-pushes after a
    restart); refuses a re-bind to a different user."""
    global AGENT_USERNAME, credentials, _bootstrapped

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization[len("Bearer "):].strip()
    try:
        claims = auth.verify_bootstrap_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user_id = claims.get("user_id")
    creds_key = claims.get("creds_key")
    if not user_id or not creds_key:
        raise HTTPException(status_code=400, detail="Bootstrap token missing user_id or creds_key")

    if _bootstrapped and AGENT_USERNAME != user_id:
        raise HTTPException(
            status_code=409,
            detail=f"Agent already bootstrapped for '{AGENT_USERNAME}'; refusing rebind to '{user_id}'",
        )

    AGENT_USERNAME = user_id
    auth.configure(user_id)
    try:
        credentials = credentials_module.load(
            creds_key=creds_key,
            env_seed=_credential_env_seed(),
        )
    except Exception as e:
        logger.error("Bootstrap failed opening credentials store: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not open credentials store: {e}")
    broker.configure(credentials)
    _apply_anthropic_key()
    _apply_credentials_env()
    _bootstrapped = True
    logger.info("Bootstrapped: username=%s (credentials generation=%s, anthropic=%s)",
                user_id, credentials.generation(), anthropic_key_status)
    return {"bootstrapped": True, "username": user_id}


@app.post("/v1/chat", dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def chat(request: ChatRequest, authorization: Optional[str] = Header(default=None)):
    """
    Streaming chat endpoint using Server-Sent Events.

    Requires a valid Bearer JWT issued by accounts whose `sub` matches this
    agent's bootstrapped username. The browser sends the JWT it received
    from accounts at sign-in.

    SSE Event Types:
    - init: Session initialized {session_id}
    - content: Text content {text}
    - thinking: Agent reasoning {text}
    - tool_use_start: Tool invocation {tool, id, input}
    - tool_result: Tool result {tool_use_id, content, is_error}
    - commentary: Background activity line for the activity panel {text, kind}
      (kind: narration = the agent's own voice; muted/notable/check = machine
      tool-labels; error = a failed step)
    - ticker: Running NEW-token count for the live status ticker {tokens},
      emitted per model round; a live estimate reconciled by `usage` at turn end
    - result: Final result {text, session_id, duration_ms, cost_usd}
    - descriptor: Raw deployment-descriptor YAML text {descriptor} (relayed to accounts)
    - env_status: Environment state {status, env_type, datasets, symbols, broker, mode}
    - scorecard_update: Test results {return, winRate, trades, sharpe, drawdown, profitFactor}
    - chart_data: Chart data push {type, series, indicators}
    - error: Error {message, error_type}
    - done: Stream complete {}
    """
    if anthropic_key_status == "missing":
        raise HTTPException(
            status_code=503,
            detail="Anthropic API key is not configured for this agent",
        )
    if anthropic_key_status == "invalid":
        raise HTTPException(
            status_code=502,
            detail="The configured Anthropic API key is invalid",
        )

    # The caller's own JWT (already verified by require_self_jwt) — forwarded
    # verbatim when reporting usage to accounts, so the billing write runs as
    # the user (self-or-admin), no new credential.
    auth_token = (authorization[len("Bearer "):].strip()
                  if authorization and authorization.startswith("Bearer ") else None)
    return StreamingResponse(
        tracked_stream_agent_response(
            message=request.message,
            conversation_id=request.conversation_id,
            algo_id=request.algo_id,
            auth_token=auth_token,
        ),
        media_type="text/event-stream"
    )


@app.get("/v1/skills", dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def get_skills(conversation_id: Optional[str] = None):
    """List the skills available to the agent, across all tiers:
      - system: predefined, read-only (shipped with the agent)
      - user-global: agent-authored, reusable across strategies
      - user-strategy: specific to one strategy (included when conversation_id is given)

    The frontend uses this to show a skill list; "running" a skill is a normal
    chat turn (e.g. "use the <name> skill"), which the model services via the
    Skill tool — there is no separate execution endpoint."""
    cwd = str(conversations.strategy_dir(conversation_id)) if conversation_id else None
    return {"skills": skills.list_skills(cwd)}


@app.post("/v1/credentials")
async def update_credentials_deprecated(update: CredentialsUpdate):
    """REMOVED — direct credential writes from the frontend are no longer
    supported. The frontend now calls accounts (PUT /accounts/{username}/
    credentials/{provider}); accounts then pushes the new value to this
    agent via POST /v1/credentials/update.
    Returns 410 Gone with a pointer message so any lingering caller fails
    loudly rather than silently writing values that get clobbered by the
    next push from accounts."""
    raise HTTPException(
        status_code=410,
        detail=(
            "Direct credential writes are no longer accepted. Send credential "
            "updates to the accounts service (PUT /accounts/{username}/"
            "credentials/{provider}); accounts will push them here."
        ),
    )


class CredentialUpdate(BaseModel):
    """Single-credential push from the accounts service."""
    provider: str   # e.g. "massive_api_key", "palpha_api_key", "github_token", "connecttrade_user_secret"
    value: str


@app.post("/v1/credentials/update", status_code=204,
          dependencies=[Depends(require_bootstrapped)])
async def push_credential(update: CredentialUpdate):
    """Push a single credential value from accounts.

    No JWT required for v1 — the only effect is updating a cache value
    (no privilege escalation), and the accounts -> agent direction is
    constrained by the jump server's routing (only accounts can reach
    this URL in production). Hardenable later by requiring an
    accounts-signed JWT here too.

    The credentials store auto-persists on __setitem__, so this is a
    single dict assignment + an encrypted disk write."""
    credentials[update.provider] = update.value
    logger.info(f"Credential pushed: {update.provider} (generation={credentials.generation()})")
    # The Anthropic key drives chat availability — re-sync it into the
    # process env and re-validate so /health reflects the new value. Every
    # other credential needs to land in the process env too so the CLI's
    # ${VAR} substitution in deployment descriptors can resolve.
    if update.provider == "anthropic_api_key":
        _apply_anthropic_key()
    else:
        _apply_credentials_env()


@app.get("/v1/credentials/status",
         dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def credentials_status():
    """Check which credentials are configured. JWT-protected so a leaked
    sandbox URL can't be probed for which integrations are wired up."""
    return {
        "massive": bool(credentials.get("massive_api_key")),
        "precision_alpha": bool(credentials.get("palpha_api_key")),
        "hwai": bool(credentials.get("hwai_api_key")),
        "connecttrade": all([
            credentials.get("connecttrade_client_id"),
            credentials.get("connecttrade_client_secret"),
            credentials.get("connecttrade_user_id"),
            credentials.get("connecttrade_user_secret"),
        ]),
        "github_personal": bool(credentials.get("github_user") and credentials.get("github_token")),
    }


# -- Conversations (the agent workspace's "projects") --------------
# A conversation == a project: a named, persistent chat thread that owns
# its own message history, commentary audit trail, and SDK session.

class CreateConversationRequest(BaseModel):
    """Create a conversation; `first_message` seeds the deduced name."""
    first_message: Optional[str] = None


class RenameConversationRequest(BaseModel):
    """Rename a conversation."""
    name: str


@app.get("/v1/conversations",
         dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def list_conversations():
    """List the user's conversations (projects), most-recently-updated first.

    LEGACY / UNUSED: the accounts service is now the authoritative project
    registry — the frontend lists projects from accounts, not from the
    agent. This endpoint is left in place (harmless; it reflects only the
    agent's local chat-layer files) but is no longer called by the
    frontend."""
    return {"conversations": conversations.list_conversations()}


@app.post("/v1/conversations",
          dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def create_conversation(request: CreateConversationRequest):
    """Create a conversation. The agent deduces a name from the first message.

    LEGACY / UNUSED: the accounts service is now the authoritative project
    registry and mints project ids — the frontend creates projects against
    accounts, not the agent. This endpoint (and the agent's id-minting) is
    left in place but is no longer called by the frontend. New chat threads
    arrive with an accounts-minted conversation_id; stream_agent_response
    materialises the chat-layer record via conversations.ensure()."""
    return conversations.meta(conversations.create(request.first_message or ""))


@app.patch("/v1/conversations/{conversation_id}",
           dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def rename_conversation(conversation_id: str, request: RenameConversationRequest):
    """Rename a conversation."""
    record = conversations.rename(conversation_id, request.name)
    if record is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    return conversations.meta(record)


@app.get("/v1/conversations/{conversation_id}/history",
         dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def conversation_history(conversation_id: str):
    """Replay a conversation: its messages and its commentary audit trail."""
    record = conversations.get(conversation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    return {
        "id": record["id"],
        "name": record["name"],
        "messages": record.get("messages", []),
        "commentary": record.get("commentary", []),
        # Lifecycle (intent + track + stage) + per-(stage × model) usage, so the
        # stepper + telemetry footer survive a reload. The frontend renders the
        # agent-supplied track (empty => no stepper).
        "intent": record.get("intent", conversations.DEFAULT_INTENT),
        "track": record.get("track", []),
        "stage": record.get("stage", ""),
        "maxStage": record.get("maxStage", ""),
        "usage": conversations.usage_public(record),
    }


@app.delete("/v1/conversations/{conversation_id}",
            dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def delete_conversation(conversation_id: str):
    """Permanently delete a strategy: the agent-side folder (meta, algo code,
    per-strategy memory + skills). 404 if the agent never materialised it.
    Accounts deletes its project registry record separately, so a strategy that
    exists in accounts but never here still deletes cleanly there."""
    if not conversations.delete(conversation_id):
        raise HTTPException(status_code=404, detail="No such conversation")
    return Response(status_code=204)


# -- Uploaded project files (per-project agent context) ------------
# The user uploads files (data, specs, examples, a CSV of trades, ...) into a
# project; they are stored in the project folder's uploads/ dir, the agent is
# made aware of them via a small index injected into the prompt, and it reads
# them on demand with its existing Read/Glob/Grep tools (see conversations.py
# and prompt.build_system_prompt). Never inline whole files into the prompt.

# Per-file upload size cap, to keep one upload from filling the instance disk.
MAX_UPLOAD_MB = int(os.environ.get("DATAFYE_AGENT_MAX_UPLOAD_MB", "25"))


@app.get("/v1/conversations/{conversation_id}/files",
         dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def list_conversation_files(conversation_id: str):
    """List a project's uploaded context files (name, type, size, modified)."""
    return {"files": conversations.list_files(conversation_id)}


@app.post("/v1/conversations/{conversation_id}/files",
          dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def upload_conversation_file(conversation_id: str, file: UploadFile = File(...)):
    """Upload one file into a project as agent context. Stored in the project
    folder's uploads/ dir (durable, per-project isolated); the agent reads it on
    demand. A same-named file is overwritten."""
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (limit {MAX_UPLOAD_MB} MB)",
        )
    conversations.ensure(conversation_id)
    entry = conversations.save_file(conversation_id, file.filename, data)
    if entry is None:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return entry


@app.delete("/v1/conversations/{conversation_id}/files/{filename}",
            dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def delete_conversation_file(conversation_id: str, filename: str):
    """Remove one uploaded file from a project. 404 if absent."""
    if not conversations.delete_file(conversation_id, filename):
        raise HTTPException(status_code=404, detail="No such file")
    return Response(status_code=204)


@app.get("/v1/conversations/{conversation_id}/outputs",
         dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def list_conversation_outputs(conversation_id: str):
    """List the deliverables the agent produced for this project (the download
    list). Distinct from uploads/ (the user's context files)."""
    return {"outputs": conversations.list_outputs(conversation_id)}


@app.get("/v1/conversations/{conversation_id}/outputs/{filename}",
         dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def download_conversation_output(conversation_id: str, filename: str):
    """Download one agent-produced output file. Path-safety-guarded (never serves
    outside the project's outputs/ dir). 404 if the file does not exist."""
    path = conversations.output_file_path(conversation_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


# Fail fast at startup if a load-bearing route is missing (e.g. a mis-applied
# edit clobbered its decorator). Otherwise the agent would serve /health 200
# while silently 404'ing /bootstrap, masking a broken agent as "Running". A
# missing route now crashes startup loudly.
_REQUIRED_ROUTES = {
    ("GET", "/health"),
    ("POST", "/bootstrap"),
    ("POST", "/v1/chat"),
}
_present_routes = {
    (_m, getattr(_r, "path", None))
    for _r in app.routes
    for _m in (getattr(_r, "methods", None) or ())
}
_missing_routes = sorted(f"{_m} {_p}" for (_m, _p) in _REQUIRED_ROUTES if (_m, _p) not in _present_routes)
if _missing_routes:
    raise RuntimeError(f"Required agent routes are not registered: {_missing_routes}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
