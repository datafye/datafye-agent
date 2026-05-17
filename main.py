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

from fastapi import Depends, FastAPI, Header, HTTPException
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
import credentials as credentials_module

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -- Configuration from environment --------------------------------
# All env vars use DATAFYE_AGENT_ prefix for consistency
ANTHROPIC_API_KEY = os.getenv("DATAFYE_AGENT_ANTHROPIC_API_KEY")
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

# MCP servers (optional, for additional tooling)
MCP_SERVERS_ADDITIONAL = os.getenv("DATAFYE_AGENT_MCP_SERVERS_ADDITIONAL", "[]")


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
    # Planning
    "EnterPlanMode", "ExitPlanMode", "AskUserQuestion",
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
    configured: bool
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


def _credential_env_seed() -> dict:
    """Legacy env-var credential seed, applied only when a store is created
    fresh (local dev). In production, accounts pushes credentials."""
    return {
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


import time

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

    mcp_servers, allowed_tools = build_mcp_config()
    system_prompt = build_system_prompt(
        docs_dir=DOCS_DIR,
        cli_path=CLI_PATH,
        workspace_dir=WORKSPACE_DIR,
        samples_dir=SAMPLES_DIR,
        credential_summary=get_credential_summary(),
        algo_id=algo_id,
    )

    options = ClaudeAgentOptions(
        model=CLAUDE_MODEL,
        cwd=WORKSPACE_DIR,
        system_prompt=system_prompt,
        permission_mode="bypassPermissions",
        mcp_servers=mcp_servers if mcp_servers else None,
        allowed_tools=allowed_tools,
        include_partial_messages=True,
    )

    # Resume existing session if available
    if conversation_id and conversation_id in sessions:
        options.resume = sessions[conversation_id]
        logger.info(f"Resuming session for conversation {conversation_id}")

    logger.info(f"[TRACE] === Starting Agent Query ===")
    logger.info(f"[TRACE] Model: {CLAUDE_MODEL}")
    logger.info(f"[TRACE] Algo: {algo_id}")
    logger.info(f"[TRACE] Conversation: {conversation_id}")
    logger.info(f"[TRACE] Message: {truncate(message)}")
    logger.info(f"[TRACE] MCP servers: {list(mcp_servers.keys())}")

    try:
        msg_count = 0

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
                            yield sse_event('content', {'text': text})

                    # Thinking
                    elif hasattr(block, 'thinking'):
                        thinking = getattr(block, 'thinking', '')
                        if thinking:
                            yield sse_event('thinking', {'text': thinking})

                    # Tool use
                    elif hasattr(block, 'name') and hasattr(block, 'input'):
                        yield sse_event('tool_use_start', {
                            'tool': getattr(block, 'name', ''),
                            'id': getattr(block, 'id', ''),
                            'input': getattr(block, 'input', {})
                        })

                    # Tool result
                    elif hasattr(block, 'tool_use_id'):
                        yield sse_event('tool_result', {
                            'tool_use_id': getattr(block, 'tool_use_id', ''),
                            'content': str(getattr(block, 'content', '') or ''),
                            'is_error': getattr(block, 'is_error', False)
                        })

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
                })

        logger.info(f"[TRACE] Done. Messages processed: {msg_count}")
        yield sse_event('done', {})

    except Exception as e:
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
    logger.info(f"  API key configured: {'yes' if ANTHROPIC_API_KEY else 'no'}")

    docs_available = os.path.isdir(DOCS_DIR)
    samples_available = os.path.isdir(SAMPLES_DIR)
    logger.info(f"  Docs available: {docs_available}")
    logger.info(f"  Samples dir: {SAMPLES_DIR} (available: {samples_available})")

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
        configured=bool(ANTHROPIC_API_KEY),
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
    _bootstrapped = True
    logger.info("Bootstrapped: username=%s (credentials generation=%s)",
                user_id, credentials.generation())
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
    - result: Final result {text, session_id, duration_ms, cost_usd}
    - env_status: Environment state change {status, datasets, symbols, broker, mode}
    - scorecard_update: Test results {return, winRate, trades, sharpe, drawdown, profitFactor}
    - chart_data: Chart data push {type, series, indicators}
    - error: Error {message, error_type}
    - done: Stream complete {}
    """
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    return StreamingResponse(
        tracked_stream_agent_response(
            message=request.message,
            conversation_id=request.conversation_id,
            algo_id=request.algo_id,
        ),
        media_type="text/event-stream"
    )


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
