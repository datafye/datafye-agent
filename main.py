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
from typing import Optional, AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from claude_agent_sdk import (
    query, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, SystemMessage,
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
    # Task management
    "Task", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskStop", "TaskOutput",
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
last_chat_activity_at: int = 0
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


async def generate_title(first_message: str) -> Optional[str]:
    """Summarize the user's first message into a short strategy title via a
    cheap model call (direct Anthropic API, the key is already in the env).
    Returns None on any failure, in which case the caller keeps the provisional
    first-few-words name."""
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
        parts = resp.json().get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        title = text.strip().strip('"“”\'').rstrip(".").strip()
        return title[:60] or None
    except Exception as e:
        logger.warning("Title generation failed: %s", e)
        return None


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
        return ("Running a workspace command", "muted")
    if tool in ("WebFetch", "WebSearch"):
        return ("Looking something up online", "muted")
    if tool == "Task":
        return ("Working through a sub-task", "muted")
    if tool == "TodoWrite":
        return ("Planning the next steps", "muted")
    if tool.startswith("mcp__datafye-api__"):
        return ("Working in the Datafye environment", "notable")
    if tool.startswith("mcp__"):
        return ("Using a connected tool", "notable")
    return None


import time


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
) -> AsyncIterator[str]:
    """Wraps stream_agent_response with running_jobs + lastChatActivityAt
    bookkeeping. Increments running_jobs at stream start, decrements at end
    (even on error), so /health reports an accurate live-job count for
    accounts' idle monitor."""
    global last_chat_activity_at, running_jobs
    last_chat_activity_at = int(time.time() * 1000)
    running_jobs += 1
    try:
        async for event in stream_agent_response(message, conversation_id, algo_id):
            yield event
    finally:
        running_jobs -= 1


# -- Agent Streaming -----------------------------------------------

async def stream_agent_response(
    message: str,
    conversation_id: Optional[str],
    algo_id: Optional[str],
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

    try:
        msg_count = 0
        assistant_text = ""

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
                            assistant_text += text
                            yield sse_event('content', {'text': text})

                    # Thinking
                    elif hasattr(block, 'thinking'):
                        thinking = getattr(block, 'thinking', '')
                        if thinking:
                            yield sse_event('thinking', {'text': thinking})

                    # Tool use
                    elif hasattr(block, 'name') and hasattr(block, 'input'):
                        tool_name = getattr(block, 'name', '')
                        tool_input = getattr(block, 'input', {})
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
                            # Persist only the meaningful trail (env/error),
                            # not every routine read/search.
                            if conversation_id and level != 'muted':
                                conversations.append_commentary(conversation_id, text)
                            yield sse_event('commentary', {'text': text, 'kind': level})

                    # Tool result
                    elif hasattr(block, 'tool_use_id'):
                        is_err = bool(getattr(block, 'is_error', False))
                        yield sse_event('tool_result', {
                            'tool_use_id': getattr(block, 'tool_use_id', ''),
                            'content': str(getattr(block, 'content', '') or ''),
                            'is_error': getattr(block, 'is_error', False)
                        })
                        if is_err:
                            err_text = 'A step reported an error'
                            if conversation_id:
                                conversations.append_commentary(conversation_id, err_text)
                            yield sse_event('commentary', {'text': err_text, 'kind': 'error'})

            # Stream events
            elif hasattr(msg, 'event'):
                yield sse_event('stream', {'event': getattr(msg, 'event', {})})

            # Result
            elif isinstance(msg, ResultMessage):
                yield sse_event('result', {
                    'text': getattr(msg, 'result', ''),
                    'session_id': getattr(msg, 'session_id', None),
                    'duration_ms': getattr(msg, 'duration_ms', None),
                    'cost_usd': getattr(msg, 'total_cost_usd', None),
                    'usage': getattr(msg, 'usage', None),
                    'num_turns': getattr(msg, 'num_turns', None),
                })

        logger.info(f"[TRACE] Done. Messages processed: {msg_count}")
        if conversation_id and assistant_text:
            conversations.append_message(conversation_id, "assistant", assistant_text)

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

        # First turn of a new conversation: replace the provisional first-few-
        # words name with an LLM-summarized title. The app adopts it (sidebar +
        # accounts registry). Best-effort — a failure keeps the provisional name.
        if conversation_id and is_first_turn:
            title = await generate_title(message)
            if title:
                conversations.rename(conversation_id, title)
                yield sse_event('title', {'conversation_id': conversation_id, 'name': title})

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
async def chat(request: ChatRequest):
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
    - commentary: Background activity line for the activity panel {text}
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

    return StreamingResponse(
        tracked_stream_agent_response(
            message=request.message,
            conversation_id=request.conversation_id,
            algo_id=request.algo_id,
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
