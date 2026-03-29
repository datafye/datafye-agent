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
from typing import Optional, AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from claude_agent_sdk import (
    query, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, SystemMessage,
)

from prompt import build_system_prompt

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration from environment ────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("DATAFYE_AGENT_MODEL", "opus")
PORT = int(os.getenv("DATAFYE_AGENT_PORT", "18780"))
ALLOWED_ORIGINS = os.getenv("DATAFYE_AGENT_ALLOWED_ORIGINS", "*").split(",")

# Working directory for algo development (user's workspace)
WORKSPACE_DIR = os.getenv("DATAFYE_AGENT_WORKSPACE", "/home/datafye/workspace")

# Path to local Datafye documentation
DOCS_DIR = os.getenv("DATAFYE_DOCS_DIR", "/home/datafye/docs")

# Path to Datafye CLI binary
CLI_PATH = os.getenv("DATAFYE_CLI_PATH", "datafye")

# Path to Datafye samples (Java-based reference for API patterns)
SAMPLES_DIR = os.getenv("DATAFYE_SAMPLES_DIR", "/home/datafye/samples")

# User credentials (injected per-user at launch)
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "")
PALPHA_API_KEY = os.getenv("PALPHA_API_KEY", "")
HWAI_API_KEY = os.getenv("HWAI_API_KEY", "")
CONNECTTRADE_CLIENT_ID = os.getenv("CONNECTTRADE_CLIENT_ID", "")
CONNECTTRADE_CLIENT_SECRET = os.getenv("CONNECTTRADE_CLIENT_SECRET", "")
CONNECTTRADE_USER_ID = os.getenv("CONNECTTRADE_USER_ID", "")
CONNECTTRADE_USER_SECRET = os.getenv("CONNECTTRADE_USER_SECRET", "")

# GitHub - defaults to Datafye org, user can override
GITHUB_USER = os.getenv("GITHUB_USER", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_ORG = os.getenv("GITHUB_ORG", "datafye")

# MCP servers (optional, for additional tooling)
MCP_SERVERS_ADDITIONAL = os.getenv("DATAFYE_MCP_SERVERS_ADDITIONAL", "[]")

# ── Internal tools ────────────────────────────────────────────────
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

# ── Session storage ───────────────────────────────────────────────
# Single session per user — maps conversation_id -> agent session_id
sessions: dict[str, str] = {}


# ── Request/Response Models ───────────────────────────────────────

class ChatRequest(BaseModel):
    """Request model for chat endpoint."""
    message: str
    conversation_id: Optional[str] = None
    algo_id: Optional[str] = None


class HealthResponse(BaseModel):
    """Response model for health endpoint."""
    status: str
    configured: bool
    workspace: str
    docs_available: bool
    cli_available: bool
    credentials: dict[str, bool]


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


# ── Credential state (mutable at runtime) ─────────────────────────
credentials = {
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

    if credentials["massive_api_key"]:
        lines.append("- Massive (Polygon) API key: configured (for SIP and Crypto datasets)")
    else:
        lines.append("- Massive (Polygon) API key: NOT configured (needed for SIP and Crypto datasets)")

    if credentials["palpha_api_key"]:
        lines.append("- Precision Alpha API key: configured (for Palpha dataset)")
    else:
        lines.append("- Precision Alpha API key: NOT configured (needed for Palpha dataset)")

    if credentials["hwai_api_key"]:
        lines.append("- HWAI API key: configured (for HWAI dataset)")
    else:
        lines.append("- HWAI API key: NOT configured (needed for HWAI dataset)")

    ct_configured = all([
        credentials["connecttrade_client_id"],
        credentials["connecttrade_client_secret"],
        credentials["connecttrade_user_id"],
        credentials["connecttrade_user_secret"],
    ])
    if ct_configured:
        lines.append("- ConnectTrade broker credentials: configured (for simulated trading)")
    else:
        lines.append("- ConnectTrade broker credentials: NOT configured (needed for simulated trading)")

    if credentials["github_user"] and credentials["github_token"]:
        lines.append(f"- GitHub: using personal account ({credentials['github_user']})")
    else:
        lines.append(f"- GitHub: using Datafye org ({GITHUB_ORG})")

    return "\n".join(lines)


# ── SSE Helpers ───────────────────────────────────────────────────

def sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"data: {json.dumps({'type': event_type, **data})}\n\n"


def truncate(text: str, limit: int = 150) -> str:
    """Truncate text for logging."""
    if not text:
        return "<empty>"
    cleaned = text.replace("\n", "\\n").replace("\r", "")
    return cleaned[:limit] + "..." if len(cleaned) > limit else cleaned


# ── Agent Streaming ───────────────────────────────────────────────

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


# ── App Setup ─────────────────────────────────────────────────────

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


# ── Endpoints ─────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint."""
    import shutil
    return HealthResponse(
        status="healthy",
        configured=bool(ANTHROPIC_API_KEY),
        workspace=WORKSPACE_DIR,
        docs_available=os.path.isdir(DOCS_DIR),
        cli_available=shutil.which(CLI_PATH) is not None,
        credentials={
            "massive": bool(credentials["massive_api_key"]),
            "precision_alpha": bool(credentials["palpha_api_key"]),
            "hwai": bool(credentials["hwai_api_key"]),
            "connecttrade": all([
                credentials["connecttrade_client_id"],
                credentials["connecttrade_client_secret"],
                credentials["connecttrade_user_id"],
                credentials["connecttrade_user_secret"],
            ]),
            "github": bool(credentials["github_user"] and credentials["github_token"]),
        }
    )


@app.post("/v1/chat")
async def chat(request: ChatRequest):
    """
    Streaming chat endpoint using Server-Sent Events.

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
        stream_agent_response(
            message=request.message,
            conversation_id=request.conversation_id,
            algo_id=request.algo_id,
        ),
        media_type="text/event-stream"
    )


@app.post("/v1/credentials")
async def update_credentials(update: CredentialsUpdate):
    """Update user credentials at runtime (called from frontend settings)."""
    updated = []
    for field, value in update.model_dump(exclude_none=True).items():
        if field in credentials:
            credentials[field] = value
            updated.append(field)

    logger.info(f"Credentials updated: {updated}")
    return {"updated": updated}


@app.get("/v1/credentials/status")
async def credentials_status():
    """Check which credentials are configured."""
    return {
        "massive": bool(credentials["massive_api_key"]),
        "precision_alpha": bool(credentials["palpha_api_key"]),
        "hwai": bool(credentials["hwai_api_key"]),
        "connecttrade": all([
            credentials["connecttrade_client_id"],
            credentials["connecttrade_client_secret"],
            credentials["connecttrade_user_id"],
            credentials["connecttrade_user_secret"],
        ]),
        "github_personal": bool(credentials["github_user"] and credentials["github_token"]),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
