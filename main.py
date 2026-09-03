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
algorithmic trading project development. Each user gets their own instance
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
import re
import logging
import socket
import asyncio
import time
import unicodedata
import uuid
from typing import Optional, AsyncIterator, Dict, List, Any
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from claude_agent_sdk import (
    query, ClaudeAgentOptions, HookMatcher,
    AssistantMessage, ResultMessage, SystemMessage,
    create_sdk_mcp_server, tool,
)

from prompt import build_system_prompt
import auth
import broker
import conversations
import credentials as credentials_module
import memory
import foundry
import harness
import warmth
from foundry import read_foundry_state, describe_for_model, graceful_stop
import skills

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# One-off diagnostic: dump the raw per-round usage object from the SDK. OFF by
# default -- it is one line per model round, which is hundreds per build turn.
# Set DATAFYE_AGENT_LOG_USAGE=1 in agent.env and restart to read which fields
# the bundled CLI actually passes through (notably any 5m/1h cache-creation
# split), which is only knowable from real traffic. Logged at INFO deliberately:
# the service runs at INFO, so a debug-level line would be silently swallowed
# and the diagnostic would look broken rather than disabled.
_LOG_RAW_USAGE = os.getenv("DATAFYE_AGENT_LOG_USAGE", "").strip().lower() in ("1", "true", "yes")

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
# agent machine. Read by foundry.py's background observation (readiness,
# datasets, type, and the descriptor's symbols/broker/mode), and after a chat
# turn to report the deployed descriptor to accounts. If no environment is up,
# nothing is reported and the readiness block says so.
DATAFYE_DEPLOYMENT_API_URL = os.getenv(
    "DATAFYE_AGENT_DEPLOYMENT_API_URL",
    "http://local-foundry-dev-api.datafye.local:7776",
)

# MCP servers (optional, for additional tooling)
MCP_SERVERS_ADDITIONAL = os.getenv("DATAFYE_AGENT_MCP_SERVERS_ADDITIONAL", "[]")

# The hostname the jump server routes to this box (DAT-202). An app the model
# builds is reachable at https://<username>.<this>:<port> for a port in the
# reserved band, matching the wildcard block that already routes
# <username>.app.datafye.io to the agent. CLEAR IT on a self-hosted agent: with
# no jump server there is no external route, and the prompt then tells the model
# its app is local-only rather than handing the user a link that cannot resolve.
APP_PREVIEW_HOST = os.getenv("DATAFYE_AGENT_APP_PREVIEW_HOST", "app.datafye.io")

# The agent runs a single, explicit memory model (see memory.py + conversations.py):
# global notes/index under the state root, per-project CLAUDE.md + memory/ in each
# project folder. The claude CLI that the SDK spawns has its OWN auto-memory feature,
# which is ON by default and would maintain a second, uncontrolled store. Disable it
# so there is one coherent memory system. The SDK subprocess inherits this env var.
# Overridable by pre-setting it in the environment.
os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")

# How long a foreground Bash command may run before the harness gives up on it
# (DAT-203). The harness does not KILL a command that outlives its timeout -- it
# moves it to the BACKGROUND, hands back a task id and an output file, and lets
# the turn continue. That is the failure this raises the ceiling to avoid: the
# model cannot tell a still-running operation from a finished one, so on u1 it
# fired `apply` on top of a `start` that was still going and destroyed the
# environment.
#
# ⚠️ The fix is the CEILING, not a rule telling the model to wait. `prompt.py`
# forbids backgrounding outright (DAT-185) and cannot enforce it -- the harness
# backgrounds on its own, whatever the prompt says. Raising the ceiling above the
# longest operation removes the harness's REASON to background rather than
# leaving a prohibition the surface ignores. Same lesson as `AskUserQuestion` and
# the `Task` family: never forbid what you do not control.
#
# 30 minutes clears the ~17-minute measured cold provision with real headroom for
# a loaded box. The box stays awake for it: a provision in flight is reported as
# warm through the DAT-183 marker (see warmth.py), so dormancy cannot stop the
# instance underneath a long command.
#
# ⚠️ Only the MAX is raised. BASH_DEFAULT_TIMEOUT_MS is deliberately left at the
# harness default of 2 minutes, because it applies to EVERY command -- an
# ordinary one that hangs (a curl to a dead host) would otherwise block the turn
# for half an hour. The prompt tells the model to pass an explicit generous
# timeout for environment operations, and this ceiling is what makes that
# request honored rather than silently clamped to 600000.
#
# Verified against a claude CLI: a 40s command requested with timeout=600000
# backgrounds at the cap when the cap is below it, completes in the foreground
# when the cap is above it, and a 660s command clears the old 600s default.
#
# ⚠️ That was PATH CLI 2.1.226, but a turn actually runs the SDK's BUNDLED CLI
# (`_find_cli` prefers `claude_agent_sdk/_bundled/claude` over `which claude`),
# which for the current pin is 2.1.85. The bundled binary would not run on the
# dev Mac, so this is UNVERIFIED on the version that ships -- re-check on a box
# if long commands still get backgrounded.
os.environ.setdefault("BASH_MAX_TIMEOUT_MS", os.getenv("DATAFYE_AGENT_BASH_MAX_TIMEOUT_MS", "1800000"))

# The largest single message the SDK will accept off the CLI's stdout (DAT-204).
#
# The SDK frames the CLI's NDJSON output and raises `CLIJSONDecodeError` on any
# one message bigger than this. That error comes out of the READ LOOP, so it does
# not fail the tool call -- it ends the whole turn. A user lost a 37-minute
# analysis to it after the model read back a chart it had just drawn, which is a
# reasonable thing to do and had already caught two real layout bugs.
#
# The SDK default is 1 MB, which a single chart can exceed. 16 MB is far above any
# legitimate tool result while still bounding a runaway one: the cap has to stay
# finite, or a pathological result trades a lost turn for a lost process.
#
# ⚠️ Raising this is necessary and NOT sufficient, which is why the read guard
# below exists too. Verified against the pinned SDK by driving the real transport:
# a 2 MB message dies at the 1 MB default and parses at 16 MB, and a 20 MB message
# still dies at 16 MB. There is always a payload big enough.
MAX_BUFFER_SIZE = int(os.getenv("DATAFYE_AGENT_MAX_BUFFER_SIZE", str(16 * 1024 * 1024)))

# Anything at or above this is refused before it is read. Half the buffer, because
# the framer bounds the ENCODED message -- a JSON-escaped payload plus the
# surrounding envelope is bigger than the file on disk, and base64 for an image
# is about a third bigger again. Half leaves room for both without arithmetic
# that would go stale the moment the envelope changes.
READ_REFUSE_BYTES = MAX_BUFFER_SIZE // 2

# Where npm puts globally-installed CLIs for the datafye user (DAT-201). The
# installer sets this prefix in ~datafye/.npmrc so `npm install -g` does not try
# to write into the root-owned Node tree in /opt, and adds this bin dir to PATH
# in /etc/profile.d -- which reaches an operator's login shell but NOT the model,
# whose Bash commands do not run in a login shell. Adding it here is what makes a
# tool the model just installed globally actually runnable by the next command;
# the SDK subprocess inherits os.environ. Harmless when the directory does not
# exist (a box without Node, or before the first global install).
_NPM_GLOBAL_BIN = os.path.expanduser("~/.npm-global/bin")

# The sbin directories, for the same reason and with a sharper symptom. systemd
# hands a service a minimal PATH, and on this box it does not include
# /usr/sbin -- so `ss`, which prompt.py tells the model to run before binding an
# app and again to verify the bind, is simply not found. Observed on a live box:
# the model's FIRST command of its FIRST app was `ss -ltn`, it failed, and the
# next command fell back to `/usr/sbin/ss`. It recovered every time, which is
# exactly why this would have gone unnoticed -- a wasted round trip on every
# single app, forever, costing tokens and latency and nothing else.
#
# Fixed here rather than by hardcoding /usr/sbin/ss into the prompt: the path is
# a property of the box, not of the instruction, and the next sbin tool the
# model reaches for would hit the same wall. Adding sbin to a non-root PATH is
# safe -- the binaries are world-executable and simply do less without
# privileges (`ss -p` shows only this user's own pids, which is all it needs).
_EXTRA_BIN_DIRS = [_NPM_GLOBAL_BIN, "/usr/sbin", "/sbin"]
_current_path = os.environ.get("PATH", "").split(os.pathsep)
for _bin_dir in _EXTRA_BIN_DIRS:
    if _bin_dir and _bin_dir not in _current_path:
        _current_path.append(_bin_dir)
os.environ["PATH"] = os.pathsep.join(p for p in _current_path if p)


# Every tool that can run work outside this turn's governance (DAT-272).
#
# ⚠️ This is the ENFORCEMENT. Leaving a tool out of `allowed_tools` is not a
# denial: `allowed_tools` becomes the CLI's `--allowedTools`, a PERMISSION
# allowlist, and `permission_mode="bypassPermissions"` makes permission checks
# moot. The comment on INTERNAL_TOOLS was right about WHY Task should be absent
# and wrong that absence achieved it. Sutra ran the identical arrangement:
# SUT-36 removed Task on 2026-08-04, and on 2026-08-31 that agent launched six
# subagents. This agent's clean runs have been down to its PROMPT, which does
# tell the model there is no Task tool, not to its allowlist.
#
# `Task` is only the alias; `Agent` is the tool's real name. `Workflow`
# orchestrates subagents by another route -- verified against claude 2.1.259,
# where `--disallowedTools Task` leaves it available. `RemoteTrigger` is the
# worst and least obvious: it creates and RUNS claude.ai routines, separate
# Claude Code sessions in the cloud under the user's own OAuth, inheriting
# nothing from prompt.py and not running on this box at all.
#
# `CronCreate` is deliberately NOT here: it enqueues a prompt into THIS session,
# so the scheduled turn re-enters this agent's own loop and does inherit the
# prompt. Banning it would imply a threat that is not there.
DELEGATION_TOOLS = ["Task", "Agent", "Workflow", "RunWorkflow", "RemoteTrigger"]

# What goes to --disallowedTools, which is NOT the same list. The CLI warns
# "Permission deny rule X matches no known tool" once per turn for a name it
# does not know, and a warning that is always wrong is how people learn to
# ignore warnings. RunWorkflow is an alias the CLI does not accept as a deny
# target, so it is hooked -- free, and defends a future rename -- but not named.
#
# ⚠️ Sharper here than on Sutra: this installer runs `claude.ai/install.sh`
# UNPINNED, so a box has whatever was current when it baked. The tool surface
# therefore differs per box, and a name valid on one may warn on another. The
# HOOK is version-independent and is what actually enforces; the deny list is
# belt and braces. This is also why the deny list cannot be the durable answer
# -- see SUT-62 for the inversion that makes the allowlist enforceable.
DISALLOWED_DELEGATION_TOOLS = ["Task", "Agent", "Workflow", "RemoteTrigger"]


async def deny_delegation(
    input_data: dict[str, Any], tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    """Refuse a tool that would run work outside this turn (DAT-272).

    A subagent does not inherit prompt.py, so nothing this agent is told about
    audience, plain language or voice governs delegated work, and its output
    lands unfiltered in a user-facing surface. It also spends its own
    containment benefit: a subagent exists so its exploration stays OUT of the
    main context, and a large report lands there anyway.

    ⚠️ FAIL-CLOSED, and deliberately the opposite of guard_oversized_read below.
    This one exists to stop something, so an input shape it does not recognise
    is refused rather than allowed.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Delegating or orchestrating work outside this turn is not available "
                "here, and the refusal is deliberate rather than a limit to work "
                "around.\n\n"
                "A subagent does not inherit your instructions, so what it writes is "
                "not governed by anything you have been told about how to talk to this "
                "user -- and its report lands in your context anyway, which is the one "
                "thing delegating was supposed to avoid.\n\n"
                "Do the work in this turn. If it is genuinely too large for one turn, "
                "say so plainly and let the user send you back in."
            ),
        }
    }


async def guard_shell_delegation(
    input_data: dict[str, Any], tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    """Refuse a shell command that invokes the Claude CLI (DAT-272).

    Denying the Task/Agent/Workflow TOOLS closes the tool routes and leaves the
    shell wide open: the installer puts `claude` on this user's PATH, `Bash` is
    allowlisted and unhooked, and permission_mode is bypassPermissions -- so
    `claude -p "..."` spawns an ungoverned session on the same credentials. That
    is the credit-exhaustion scenario this ticket exists to prevent, by a route
    a tool deny list cannot reach.

    Matches on the PROGRAM, not a substring: it splits on shell separators,
    steps over leading environment assignments, and looks at the command word.
    So `grep claude foo.log` and `cat claude.log` are fine, while `claude -p`,
    `./claude` and `/home/datafye/.local/bin/claude` are not.

    ⚠️ FAIL-OPEN on a command it cannot parse. This runs before EVERY Bash call,
    which is the agent's main working tool, and a guard that misreads a quoting
    edge case into a refusal would break far more than it protects. The narrow
    check is deliberate: it is worth having for the obvious invocation without
    pretending to be a sandbox.
    """
    try:
        command = (input_data.get("tool_input") or {}).get("command")
        if not command or not isinstance(command, str):
            return {}
        for segment in re.split(r"[;&|]+|\n|&&|\|\|", command):
            words = segment.strip().split()
            # Step over leading VAR=value assignments and common prefixes.
            while words and ("=" in words[0].split("/")[0] or words[0] in ("sudo", "env", "nohup", "setsid", "time")):
                words = words[1:]
            if not words:
                continue
            program = words[0].strip("'\"")
            if program == "claude" or program.endswith("/claude"):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Running the Claude CLI from the shell is not available here.\n\n"
                            "It would start a session that inherits none of your "
                            "instructions and runs on the same credentials as this one, "
                            "which is the thing delegating to a subagent was already "
                            "refused for.\n\n"
                            "Do the work in this turn. If it is genuinely too large for "
                            "one turn, say so plainly and let the user send you back in."
                        ),
                    }
                }
    except Exception:
        return {}   # fail open: this runs before every Bash call
    return {}


async def guard_oversized_read(
    input_data: dict[str, Any], tool_use_id: str | None, context: Any
) -> dict[str, Any]:
    """Refuse a Read whose result could not survive the SDK framer (DAT-204).

    A denial costs the model one tool call and tells it what to do instead. The
    alternative is `CLIJSONDecodeError` out of the read loop, which ends the turn
    and everything in it -- so this trades a recoverable refusal for an
    unrecoverable one.

    ⚠️ FAIL-OPEN, deliberately. This runs before every Read, and a guard that
    breaks reading would be far worse than the bug it prevents. Anything
    unexpected -- a path we cannot stat, an input shape we do not recognise, a
    permission error -- allows the read and lets the CLI decide, which is what
    happened before this existed.

    ⚠️ It does NOT replace the CLI's own limits. A current CLI already refuses
    huge text files, truncates bash output and downsizes images; this is the
    backstop for the cases it does not cover -- an older CLI (they are unpinned
    and never upgrade, DAT-215) and anything reaching the framer another way.
    """
    try:
        path = (input_data.get("tool_input") or {}).get("file_path")
        if not path:
            return {}
        size = os.path.getsize(path)
        if size < READ_REFUSE_BYTES:
            return {}
    except Exception:
        return {}   # fail open: never let this guard be the reason a read fails

    mb = size / (1024 * 1024)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"That file is {mb:.1f} MB, which is too large to read in one go -- "
                f"reading it would end this turn and lose the work so far, so the read "
                f"was refused instead.\n\n"
                f"Get at the content another way:\n"
                f"  - text or a log: `head -c 100000 <path>`, `tail -n 500 <path>`, or "
                f"`grep` for what you actually need\n"
                f"  - a data file: load it in Python and print a summary, not the file\n"
                f"  - an image or chart: check it with Python (PIL/matplotlib) and print "
                f"the dimensions or the finding, rather than reading the image itself\n\n"
                f"Tell the user what you did instead if it changes what they get."
            ),
        }
    }


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
    #
    # "Task" (delegate to a subagent) is deliberately ABSENT -- but ⚠️ ABSENCE
    # HERE IS NOT WHAT STOPS IT. This list becomes the CLI's --allowedTools, a
    # PERMISSION allowlist, and permission_mode is bypassPermissions, so a tool
    # left out of it is still callable. DELEGATION_TOOLS and the deny_delegation
    # hook are the enforcement (DAT-272); this line is documentation of intent.
    # It was first dropped as harness-only; the stronger reason is that a
    # subagent does NOT inherit prompt.py, so nothing this agent says about
    # audience, plain language or voice governs delegated work, and its output
    # lands unfiltered in a user-facing surface. It also spends its own
    # containment benefit: a subagent exists so its exploration stays OUT of the
    # main context, and a large report lands there anyway.
    "EnterPlanMode", "ExitPlanMode",
    # Notebook
    "NotebookEdit",
    # Discovery
    "Skill", "ToolSearch",
]


def guard_options() -> dict:
    """The guard-relevant SDK options, as ONE definition (DAT-272).

    Returned as kwargs and spread into the real ClaudeAgentOptions, so the turn
    and tests/test_tool_guards.py are looking at the same object rather than two
    that happen to agree.

    ⚠️ The first cut of this got it wrong in a way worth recording: it built a
    parallel ClaudeAgentOptions for the test with its own disallowed_tools
    literal, so every assertion checked the helper against itself -- deleting
    the real line left the suite green. A copy with a comment warning about
    drift is not a guard, it is the drift with a note attached, and moving the
    copy one level up does not fix it.
    """
    return {
        "disallowed_tools": list(DISALLOWED_DELEGATION_TOOLS),
        "hooks": {"PreToolUse": [
            *[HookMatcher(matcher=t, hooks=[deny_delegation]) for t in DELEGATION_TOOLS],
            HookMatcher(matcher="Bash", hooks=[guard_shell_delegation]),
            HookMatcher(matcher="Read", hooks=[guard_oversized_read]),
        ]},
        # A single oversized tool result must not destroy the turn (DAT-204).
        # The SDK frames the CLI's NDJSON stdout and refuses any one message
        # larger than this, by raising out of the read loop -- which ends the
        # turn, not just the tool call. A user lost 37 minutes of analysis to it
        # because the model read back a chart it had drawn.
        "max_buffer_size": MAX_BUFFER_SIZE,
    }


# -- Session storage -----------------------------------------------
# Single session per user -- maps conversation_id -> agent session_id
sessions: dict[str, str] = {}


# -- Activity tracking (read by /health for accounts' idle monitor) ---
# lastChatActivityAt: epoch ms of the most recent /v1/chat invocation. 0 = never.
# runningJobs: count of in-flight chat streams. Incremented on stream start,
#   decremented on stream completion (in tracked_stream_agent_response below).
# activeProxiedApps: work in flight that must not be interrupted, computed by
#   warmth.active_work() (DAT-184). Was a hardcoded [] placeholder until then.
# Advanced by a chat turn AND by the presence heartbeat (POST /v1/activity),
# so a user who is reading rather than typing still counts as present.
# Seeded to boot time (not 0) so the accounts idle-monitor — which skips
# last_chat_activity_at == 0 as "never active" — also idle-stops an agent that
# was provisioned but never chatted with: idle is measured from boot, so an
# unused agent goes Dormant after the threshold (and auto-wakes invisibly).
last_chat_activity_at: int = int(time.time() * 1000)
running_jobs: int = 0


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
    # The installed agent version, so an operator can see which build a box is
    # actually running without SSH. Read once at import (see AGENT_VERSION) --
    # /health may only ever report things that cost no more than reading a
    # variable. None on a build that predates this field, which is itself the
    # answer: that box has not taken an upgrade since it shipped.
    agent_version: Optional[str] = None
    credentials: dict[str, bool]
    username: Optional[str] = None              # None until bootstrapped
    credentials_generation: Optional[str] = None  # None until bootstrapped
    # Idle signals consumed by accounts' poll loop (Chunk 4):
    last_chat_activity_at: int      # epoch ms; 0 if no chat yet
    running_jobs: int               # count of in-flight chat streams
    # Work in flight that must not be interrupted, as self-describing labels
    # (e.g. env:data-flowing, env:provision). Accounts treats a NON-EMPTY list
    # as busy in both the pre-stop re-check and the admin countdown, so this is
    # what stops a box being stopped mid-provision. Empty means idle. The field
    # name predates the meaning (DAT-184 filled a hook that had always sent []).
    active_proxied_apps: list[str]
    # Foundry readiness, read from the state file the CLI and the boot service
    # write: {state, since, operation, intended, error, reason}. "Running" (this
    # process answering) says nothing about whether the box can do work, which is
    # how a request once landed on a box three minutes into its first provision.
    # state is starting|provisioning|restoring|ready|failed|absent, or "unknown"
    # when no state has been recorded yet.
    foundry: dict


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


# A short, cheap model used only to summarize a project's first message into a
# title — never the main reasoning model.
TITLE_MODEL = os.getenv("DATAFYE_AGENT_TITLE_MODEL", "claude-haiku-4-5")
_TITLE_PROMPT = (
    "Generate a short, specific title (3 to 6 words, Title Case, no quotes, no "
    "trailing punctuation) summarizing this request. Reply with ONLY the title.\n\nRequest: "
)


async def generate_title(first_message: str, usage_sink: Optional[list] = None) -> Optional[str]:
    """Summarize the user's first message into a short project title via a
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
# Anthropic call). Classifies where the project build is in its lifecycle so
# the workspace stepper can advance. On any failure the stepper keeps its value.
_LIFECYCLE_PROMPT = (
    "You classify what a user is doing in an AI quant workspace and where it is "
    "in its lifecycle, so the UI can show the right workflow.\n\n"
    "1) INTENT — what the user is doing. Common intents:\n"
    "   - chat: a general question or discussion (no artifact).\n"
    "   - research: one-off data analysis / exploration (a report, not a deployable artifact).\n"
    "   - signal: building a reusable trading-signal generator.\n"
    "   - algo: building a full trading project.\n"
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
    "projects for them), from their conversation. Weigh the user's tone, whether "
    "their requests are being met, friction or rework, and any praise or "
    "complaints. Reply with ONLY a JSON object, no markdown fences and no other "
    "text:\n"
    '{"rank": <integer 1-5, where 5=delighted, 3=neutral, 1=very frustrated>, '
    '"reasons": "<one or two short plain sentences on WHY, no jargon>"}\n\n'
    "Conversation (most recent last):\n\n"
)


_ENVIRONMENT_INTENT_PROMPT = (
    "You read one turn of a conversation between a user and Yukti (an AI that "
    "builds trading projects) and decide whether the USER stated a STANDING "
    "DECISION about their Datafye environment -- the cloud data platform Yukti "
    "runs their work on.\n\n"
    "A standing decision is the user saying what should be true of their "
    "environment from now on: 'shut it down, I'm done for the month', 'tear it "
    "down', 'stop it until I'm back', 'bring my environment back up', 'set one up "
    "for me'.\n\n"
    "It is NOT a standing decision when the environment is stopped, restarted or "
    "rebuilt as a STEP IN DOING WORK -- fixing something broken, switching "
    "datasets, freeing memory, retrying a failed build. Yukti does that routinely "
    "and the user has decided nothing.\n\n"
    "CRITICAL -- 'THE APP' IS NOT THE ENVIRONMENT. Yukti also builds and runs "
    "small web APPS for the user: dashboards, pages, previews, tools, served on a "
    "URL. Starting and stopping those is routine and says NOTHING about the "
    "environment. 'kill the app', 'stop the app', 'shut the page down', 'take the "
    "dashboard down', 'stop the second one' are all about an APP and must be "
    "answered none. Only count it when the user is plainly talking about their "
    "ENVIRONMENT, foundry, platform, data or datasets -- not a page they are "
    "looking at.\n\n"
    "If you are not sure WHICH of the two the user meant, answer none. If you are "
    "not sure at all, answer none: a wrong 'stopped' leaves the user without an "
    "environment, while a missed one only means it keeps running.\n\n"
    "Reply with ONLY a JSON object, no markdown fences and no other text:\n"
    '{"intended": "running" | "stopped" | "none", '
    '"reason": "<one short plain sentence, or empty for none>"}\n\n'
    "Turn:\n\n"
)


async def classify_environment_intent(transcript: str,
                                      usage_sink: Optional[list] = None) -> Optional[str]:
    """Infer a standing decision about the environment the model did not report.

    The safety net under `set_environment_intent` (DAT-214). A prompt rule asks
    the model to classify a request as policy AND to remember a second action
    after it has already performed the first -- and this codebase has paid for
    trusting that before, when the prompt's guidance on long commands did not
    stop the agent backgrounding a provision that was then orphaned (DAT-185).
    This runs post-stream on every turn, so it does not depend on the model
    choosing anything.

    Returns "running" / "stopped", or None for no decision. Deliberately biased
    towards None: a wrong "stopped" leaves someone without an environment, while
    a missed one only means theirs keeps running, which is the default anyway.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not transcript.strip():
        return None
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
                    "max_tokens": 150,
                    "messages": [{"role": "user",
                                  "content": _ENVIRONMENT_INTENT_PROMPT + transcript[:6000]}],
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
        intended = str(obj.get("intended") or "none").strip().lower()
        if intended not in ("running", "stopped"):
            return None
        # Log the classifier's OWN reason whenever it decides. The reason was
        # always asked for and always discarded, so the first misfire -- "kill
        # the app" read as a decision to stop the environment -- left nothing
        # behind saying why, and the only evidence was a changed field in
        # accounts. A decision this consequential (intent 'stopped' makes the
        # boot reconciler leave the foundry down) must say what it thought it
        # heard.
        logger.info("Environment intent inferred as %s: %s",
                    intended, str(obj.get("reason") or "(no reason given)"))
        return intended
    except Exception as e:
        logger.warning("Environment-intent classification failed: %s", e)
        return None


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
# Per (stage × model) token/cost/tool usage. Accumulated into the project's
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


async def _report_descriptor_to_accounts(conversation_id: str, descriptor_text: str,
                                        auth_token: Optional[str]) -> None:
    """Best-effort: PATCH the deployed descriptor onto the accounts project record.

    This used to travel through the BROWSER -- the agent emitted a `descriptor`
    SSE frame and the SPA relayed it onward -- so a server-to-server fact was
    recorded only if a tab happened to be open, stayed open past the end of the
    turn, and the PATCH succeeded. A headless or CLI-driven change never reached
    accounts at all. The agent already talks to accounts directly for usage,
    satisfaction, feedback and foundry intent; this is the same channel, with
    the user's own JWT, self-scoped.

    Never raises and never fails the turn, in line with every other reporter. A
    self-hosted run with no accounts routing has no token and simply does not
    report.
    """
    if not auth_token or not AGENT_USERNAME or not conversation_id:
        return
    if not descriptor_text or not descriptor_text.strip():
        return
    url = (f"{auth.ACCOUNTS_URL}/datafye-accounts-api/v1/accounts/"
           f"{AGENT_USERNAME}/projects/{conversation_id}")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(url, json={"deployment_descriptor": descriptor_text},
                                      headers={"Authorization": f"Bearer {auth_token}"})
            if resp.status_code >= 400:
                logger.warning("Descriptor report returned %s for %s",
                               resp.status_code, conversation_id)
    except Exception as e:
        logger.warning("Descriptor report failed for %s: %s", conversation_id, e)


# Which turns already recorded an intent explicitly. The post-stream sidecar
# skips those, so the model's own statement is never second-guessed by an
# inference drawn from the same conversation. Keyed by turn id and cleared as
# the turn ends -- this is a within-turn interlock, not a store.
_intent_recorded_this_turn: set[str] = set()


async def _report_foundry_intent_to_accounts(intended: str, source: str,
                                             auth_token: Optional[str]) -> bool:
    """Best-effort: record a foundry INTENT change with accounts (DAT-214).

    Accounts owns intent and pushes it back to this box as a replica, so this is
    the agent asking the owner to change the record -- not writing it locally.
    Writing it here would be the reverted design: the box is the most ephemeral
    component in the system, and a rebuild destroys anything held only on it.

    Forwards the user's own JWT, the same self-scoped channel the usage,
    satisfaction and feedback reporters use. That is what makes the agent's
    request legitimate: it is acting for the person who asked, not on its own
    authority, and a self-hosted run with no accounts simply skips it.
    """
    if not auth_token or not AGENT_USERNAME or not getattr(auth, "ACCOUNTS_URL", None):
        return False
    if intended not in ("running", "stopped"):
        return False

    url = (f"{auth.ACCOUNTS_URL}/datafye-accounts-api/v1/accounts/"
           f"{AGENT_USERNAME}/sandbox/foundry-intent")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json={"intended": intended, "source": source},
                                     headers={"Authorization": f"Bearer {auth_token}"})
        if resp.status_code // 100 == 2:
            logger.info("Foundry intent recorded with accounts: %s (%s)", intended, source)
            return True
        logger.warning("Foundry intent report returned %s", resp.status_code)
    except Exception as e:
        logger.warning("Foundry intent report failed: %s", e)
    return False


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

    @tool(
        "set_environment_intent",
        "Record that the USER has decided what should happen to their Datafye "
        "environment for the foreseeable future -- not what you are about to do "
        "to it. `intended` is 'stopped' when they ask you to shut their "
        "environment down, tear it down, or stop paying for it until they come "
        "back, and 'running' when they ask for it back or ask you to build one. "
        "`reason` is a short plain note in the user's words. "
        "Call this ONLY for a standing decision the user actually expressed. Do "
        "NOT call it when you stop, restart or rebuild the environment as part of "
        "doing work -- that is mechanics, and recording it would leave their "
        "environment switched off long after the task is done.",
        {"intended": str, "reason": str},
    )
    async def set_environment_intent(args):
        intended = (args.get("intended") or "").strip().lower()
        if intended not in ("running", "stopped"):
            return {"content": [{"type": "text",
                                 "text": "Intent must be 'running' or 'stopped', so nothing was recorded."}]}
        recorded = await _report_foundry_intent_to_accounts(intended, "user", auth_token)
        if not recorded:
            return {"content": [{"type": "text",
                                 "text": "Could not record that decision right now. The environment itself is "
                                         "unaffected; say so rather than claiming it was saved."}]}
        _intent_recorded_this_turn.add(_current_turn_key(conversation_id))
        return {"content": [{"type": "text",
                             "text": f"Recorded: their environment should be {intended} from now on."}]}

    return create_sdk_mcp_server("feedback", "1.0.0",
                                 tools=[submit_feedback, submit_satisfaction, set_environment_intent])


def _current_turn_key(conversation_id: Optional[str]) -> str:
    """The interlock key for "an intent was already recorded in this turn".

    Per-conversation rather than per-turn-id because the tool runs inside the
    SDK subprocess and does not see the turn id the streamer holds. A
    conversation is single-turn at a time, so it is the same guarantee with a
    handle both sides can name.
    """
    return conversation_id or "-"


# --- Bash command classification (feeds _tool_commentary) ---
#
# Matched on command TOKENS, never on substrings of the whole command line. A
# substring net is wrong in ways that showed up in real traffic: "latest" and
# "src/test/" contain "test" but run no test, and a bare "test"/"validate"
# keyword fires on any path or URL that happens to contain the word.
_TEST_PROGRAMS = ("pytest", "ctest")
# Trading-run words. Matched against command-line TOKENS split on the usual
# filename separators, so `python run_backtest.py` counts but `.../latest/` does
# not (nothing here is a substring of "latest").
_RUN_TOKENS = ("backtest", "paper", "replay")
# Datafye CLI subcommands that stand up or reshape the trading environment.
_ENV_TOKENS = ("foundry", "dataset", "provision", "deprovision", "apply", "morph", "deploy")
_TOKEN_SPLIT = re.compile(r"[/_\-.]+")


def _bash_activity(cmd: str):
    """Classify a Bash command as (text, level) for the activity rail.

    Splits on shell separators so `cd x && pytest` is judged on the pytest
    segment, then keys on the program and its arguments rather than scanning
    the whole line for keywords.
    """
    raw = (cmd or "").lower()
    cli_name = os.path.basename(CLI_PATH or "datafye").lower()
    saw_env = False

    for segment in re.split(r"&&|\|\||[;|]", raw):
        tokens = segment.split()
        # step over leading env assignments, e.g. `FOO=bar pytest`
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens.pop(0)
        if not tokens:
            continue
        program = tokens[0].rsplit("/", 1)[-1]
        args = [t for t in tokens[1:] if not t.startswith("-")]
        # every word in the command, split on filename separators
        pieces = {p for t in tokens[1:] for p in _TOKEN_SPLIT.split(t) if p}

        if program in _TEST_PROGRAMS or (program.startswith("python") and "pytest" in pieces):
            return ("Running the tests", "check")
        if pieces & set(_RUN_TOKENS):
            return ("Running the backtest", "check")
        if program == cli_name or program == "datafye":
            if pieces & set(_ENV_TOKENS) or (args and args[0] in _ENV_TOKENS):
                saw_env = True
                continue
            return ("Running a Datafye command", "muted")

    if saw_env:
        return ("Setting up the trading environment", "notable")
    return ("Running a workspace command", "muted")


def _is_memory_path(path: str) -> bool:
    """True if this path is one of the agent's memory files, in ANY scope.

    Fleet, user and per-project memory all read as simply "memory" in the rail:
    the SCOPE is recoverable from Tool Detail, which carries the exact path, so
    the sanitized line does not need to distinguish them.

    Deliberately does NOT treat a bare CLAUDE.md as memory. The user-scope one is
    matched by its exact path below, and a project's CLAUDE.md is auto-loaded by
    the SDK rather than Read, so a loose basename match would mostly catch a
    user's own project CLAUDE.md and mislabel it.
    """
    if not path:
        return False
    p = path.replace("\\", "/").lower()
    for scope_dir in (str(memory.FLEET_DIR), str(memory.USER_DIR)):
        if scope_dir and scope_dir.replace("\\", "/").lower() in p:
            return True
    if p == str(memory.USER_CLAUDE_MD).replace("\\", "/").lower():
        return True
    # Per-project memory lives at <project>/memory/...; every scope's index is
    # MEMORY.md, which the agent may read by name.
    return "/memory/" in p or os.path.basename(p) == "memory.md"


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
        # A Read spans docs, samples, and the user's own project files — classify
        # by where the file lives (inspect the path, never print it) so reading a
        # project source file doesn't read as "Reading reference material".
        path = ((tool_input or {}).get("file_path")
                or (tool_input or {}).get("notebook_path") or "").lower()
        # Memory first: it is the most specific location, and a memory read
        # silently labelled "Reviewing the project files" makes it impossible to
        # see from the rail that the agent consulted what it had learned.
        if _is_memory_path(path):
            return ("Recalling from memory", "muted")
        if DOCS_DIR and DOCS_DIR.lower() in path:
            return ("Consulting the Datafye documentation", "muted")
        if SAMPLES_DIR and SAMPLES_DIR.lower() in path:
            return ("Studying a reference sample", "muted")
        return ("Reviewing the project files", "muted")
    if tool in ("Grep", "Glob"):
        return ("Searching for relevant details", "muted")
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        # Same split on the write side: recording a lesson is not editing the
        # user's project, and saying so is what makes memory writes visible.
        if _is_memory_path((tool_input or {}).get("file_path") or ""):
            return ("Saving to memory", "muted")
        return ("Updating a file in the workspace", "muted")
    if tool == "Bash":
        return _bash_activity((tool_input or {}).get("command") or "")
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

# Rough token weight of a tool result, for the activity rail's per-tool figure.
# ~4 chars/token is close enough for the code, logs and listings tools return;
# an exact count would mean a count_tokens API call per tool call, which is far
# too expensive for a display badge. Deliberately measured on the FULL result,
# before _DETAIL_OUTPUT_CAP truncates it for display: the whole result is what
# lands in the prompt and gets re-read on every remaining round of the turn.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


# Common non-ASCII punctuation, mapped to what it means rather than dropped, so
# a folded sentence still reads as one. Anything not listed falls through to the
# NFKD pass below (which turns o-umlaut into o) and then to removal.
_ASCII_PUNCT = {
    "—": " - ", "–": "-",              # em / en dash
    "‘": "'", "’": "'",                # smart single quotes
    "“": '"', "”": '"',                # smart double quotes
    "…": "...",                             # ellipsis
    " ": " ", " ": " ", " ": " ",  # non-breaking / thin spaces
    "•": "-", "→": "->",               # bullet, arrow
}


def _ascii_fold(text: str) -> str:
    """Reduce text to ASCII before it reaches the commentary trail.

    prompt.py's plain-ASCII rule already forbids non-ASCII punctuation, but it
    governs what the AGENT writes. Thinking text is the API summarizer's output,
    which no prompt instruction reaches -- and observed thinking is full of em
    dashes. That text is now persisted, and accounts stores the trail
    ASCII-encoded (its resultJson is a QUARK ASCII String), so folding here is
    what keeps it from breaking on the way out.

    Folding, not stripping: a dropped em dash welds two clauses together, and a
    dropped accent is preferable to a dropped letter.
    """
    if not text:
        return text
    if text.isascii():
        return text
    for bad, good in _ASCII_PUNCT.items():
        text = text.replace(bad, good)
    if text.isascii():
        return text
    # NFKD splits an accented letter into letter + combining mark; dropping the
    # marks leaves the letter. Whatever survives that and is still non-ASCII
    # (CJK, emoji) has no ASCII reading and goes.
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if ord(c) < 128)


def _step_usage(mu) -> dict:
    """The two trustworthy figures for one step, for the activity rail.

    `new`     -- what this request appended to the prompt: the previous step's
                 reply plus its tool results. BOTH terms are summed because a
                 span under the minimum cacheable prefix is silently not cached
                 and bills as input_tokens rather than cache_creation.
    `carried` -- the prefix re-read from cache. This is the cost that accrues
                 every step whether or not anything new happened, and it is what
                 makes a long turn expensive.

    There is deliberately NO output figure. What reaches us here is the
    `message_start` usage, whose input side is complete and authoritative (which
    is why `new` at step N reconciles exactly with `carried` at step N+1) but
    whose `output_tokens` is a placeholder -- the API docs show it as literally
    1, 2 or 3. The real count arrives at the END of the same step's stream, on
    `message_delta`, which we do not currently see. Storing the placeholder
    under a name that reads like an output count was worse than not storing it
    at all. Whole-TURN output remains correct via ResultMessage.model_usage;
    only per-step granularity is missing.
    """
    def n(*keys):
        for k in keys:
            v = _usage_get(mu, k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
        return 0

    return {
        "new": (n("inputTokens", "input_tokens")
                + n("cacheCreationInputTokens", "cache_creation_input_tokens")),
        "carried": n("cacheReadInputTokens", "cache_read_input_tokens"),
    }


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
    apply, dataset add/remove, deprovision, morph, start/stop). The streamer uses it
    to re-read the descriptor right after such a tool finishes, so the accounts
    record reflects the new environment instead of waiting for turn end (which on a
    long turn can be minutes later).

    It no longer drives anything the USER sees. Showing "Applying..." from a guess
    about a tool name was replaced by the workspace polling /health, whose in-flight
    half comes from the DAT-183 marker -- a real running command rather than an
    inference from tool text."""
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

    # Each project is its own folder, and that folder is the cwd + workspace
    # for its chat turns: the agent's files, its per-project CLAUDE.md memory,
    # and its per-project .claude/skills all live there. ensure() materialises
    # the folder for an accounts-minted id (the accounts service is the
    # authoritative project registry; it mints the id, the agent follows).
    # Conversation-less (legacy/fallback) requests use the shared workspace.
    if conversation_id:
        conversations.ensure(conversation_id)
        cwd = str(conversations.project_dir(conversation_id))
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
        # Cross-session memory: global notes/index + this project's memory index.
        # Per-project CLAUDE.md is auto-loaded by the SDK (project source).
        memory_context=memory.build_memory_context(cwd if conversation_id else None),
        # Where to write user-authored skills (the author-skill skill uses this).
        skills_dir=str(skills.user_global_skills_dir()),
        # Index of the user's uploaded context files (name/type/size); bodies are
        # read on demand from uploads/ — never inlined into the prompt.
        files_context=conversations.build_files_context(conversation_id),
        # On-disk path to the foundry resource-cost cheat sheet (for the resource
        # guard); empty if the bundled file is missing.
        cheatsheet_path=CHEATSHEET_PATH if os.path.exists(CHEATSHEET_PATH) else "",
        # What the box can actually do right now. Read from the recorded state
        # rather than inferred, so the model is told a provision is in flight
        # instead of discovering it by colliding with one.
        foundry_status=describe_for_model(read_foundry_state()),
        # Where an app the model builds becomes reachable (DAT-202). Composed
        # here rather than read as config because it needs the username, which
        # only exists after bootstrap. Empty until then, and empty forever on a
        # self-hosted agent with no jump server in front of it -- in which case
        # the prompt says the app is local-only instead of promising a URL that
        # would 404.
        app_preview_base=(f"https://{AGENT_USERNAME}.{APP_PREVIEW_HOST}"
                          if AGENT_USERNAME and APP_PREVIEW_HOST else ""),
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
        # Load the project folder's own context: its CLAUDE.md (per-project
        # memory) and its .claude/skills (per-project user skills). "project"
        # is the cwd's .claude; we deliberately do NOT load "user"/"local".
        setting_sources=["project"],
        include_partial_messages=True,
        # Adaptive thinking is the model default; what is NOT the default is
        # getting the text back. On Opus 5 `display` defaults to "omitted", so
        # thinking blocks arrive with an empty string, the emit below is skipped,
        # and the reasoning is invisible -- while still being billed at output
        # rates. `display` controls visibility only and does not change billing.
        thinking={"type": "adaptive", "display": "summarized"},
        # Delegation denial, the shell guard and the oversized-read refusal.
        # ONE definition, shared with tests/test_tool_guards.py so the test
        # cannot pass against a shape the turn does not use.
        **guard_options(),
    )

    # Persist the user's turn and resume the project's SDK session.
    # get_sdk_session is read from disk so resume survives an agent restart;
    # the in-memory `sessions` map covers projects not in the store
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
        # The project's CONTEXT SIZE, refreshed every step and shown in the
        # live ticker. It is the whole prompt at that step -- uncached input +
        # cache writes + cache read -- so it is exact, needs no summing, and
        # stays correct across turns (a later turn's prompt already carries the
        # earlier ones). Within a turn it tracks Sum(new), since
        # carried(N) = Sum(new(1..N-1)).
        #
        # It replaced a counter that summed input_tokens + output_tokens: the
        # first is only the uncached remainder (single digits once the prefix is
        # cached) and the second is the message_start placeholder (1-3), so it
        # read in the tens on a turn whose context had already reached 64K.
        context_tokens = 0
        # The MAIN-THREAD step every commentary entry belongs to. One step can
        # emit several rail lines (narration is flushed one line per block), so
        # the grouping has to be stamped rather than inferred from the rendering.
        step_no = 0
        # The last step's figures, used to tell a NEW step from another message
        # of the step we are already in (see the AssistantMessage branch).
        last_step_usage = None
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
                # An AssistantMessage is NOT a model round: the SDK emits one per
                # content block, and every message of a round repeats that
                # round's SAME usage object. Counting messages therefore produced
                # a badge per block (observed on Sutra: 2-4 identical badges in a
                # row) and inflated the step number. A round is detected by the
                # usage CHANGING -- the figures grow monotonically as the prompt
                # grows, so an unchanged object means we are still inside one
                # round.
                #
                # A SUBAGENT's messages arrive on this same stream, carrying
                # their own conversation's usage, and are distinguishable only by
                # parent_tool_use_id (None = the main thread). Counting them into
                # one sequence interleaved two independent contexts, so the step
                # number jumped around and the context appeared to SHRINK. Only
                # the main thread is measured here; a subagent's context is its
                # own and dies with it. Its TOKENS are still billed and still
                # counted -- they arrive in ResultMessage.model_usage, so the
                # accounts totals are untouched by this.
                is_main = getattr(msg, 'parent_tool_use_id', None) is None
                mu = getattr(msg, 'usage', None)
                step_usage = None
                if is_main and isinstance(mu, dict):
                    su = _step_usage(mu)
                    # A step reporting nothing gets no badge; an all-zero line
                    # is noise in the rail.
                    if any(su.values()) and su != last_step_usage:
                        last_step_usage = su
                        step_usage = su
                        step_no += 1
                if _LOG_RAW_USAGE:
                    logger.info(
                        "step %s msg: thread=%s blocks=%s usage=%s",
                        step_no,
                        "main" if is_main else getattr(msg, 'parent_tool_use_id', '?'),
                        [type(b).__name__ for b in (msg.content or [])],
                        json.dumps(mu, default=str) if isinstance(mu, dict) else repr(mu))

                # A subagent's CONTENT is its own business. Gating only the badge
                # leaves `pending_blocks` thread-blind, so a subagent's text
                # accumulates into the same buffer as the main agent's and
                # flushes into the rail as narration -- a long delegated report
                # appearing as if Yukti had said it, interleaved with the real
                # narration and out of order relative to either thread.
                #
                # It cannot be fixed by tuning rail copy, because a subagent does
                # NOT inherit prompt.py: it runs on the SDK's default agent
                # prompt, so every rule about audience, plain language and short
                # action lines is simply absent for delegated work. The rail is
                # the main agent's account of itself; delegated work is
                # represented by the one tool line that spawned it.
                #
                # Tool calls are still COUNTED so the turn's tool_calls metric
                # stays honest -- only the rail output is suppressed.
                if not is_main:
                    tool_calls_this_turn += sum(
                        1 for b in msg.content
                        if hasattr(b, 'name') and hasattr(b, 'input'))
                    continue
                if step_usage:
                    if conversation_id:
                        conversations.append_commentary(
                            conversation_id, '', 'step', step=step_no, usage=step_usage)
                    yield sse_event('commentary', {
                        'text': '', 'kind': 'step', 'step': step_no, 'usage': step_usage,
                    })
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
                        # Folded to ASCII: this is the summarizer's prose, not
                        # the agent's, so prompt.py's punctuation rule does not
                        # reach it -- and it goes to a trail accounts stores
                        # ASCII-encoded. Folded before the yield too, so the live
                        # rail and the /history replay cannot differ.
                        thinking = _ascii_fold(getattr(block, 'thinking', ''))
                        if thinking:
                            # Persisted as well as streamed: thinking used to be
                            # live-only, so reopening a project lost the one
                            # record of reasoning that is billed but otherwise
                            # leaves no trace.
                            if conversation_id:
                                conversations.append_commentary(
                                    conversation_id, thinking, 'thinking', step=step_no)
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
                            # Only what the model actually said reaches the
                            # trail. A step that opens straight into tool calls
                            # leaves no line here, and the SPA stands one in at
                            # render time. That is deliberately NOT done here: a
                            # stand-in is the harness speaking, and this register
                            # is the agent's own voice -- persisting it would put
                            # fabricated words in the trail that accounts stores
                            # and the Conversation view shows.
                            if conversation_id:
                                conversations.append_commentary(
                                    conversation_id, burst, "narration", step=step_no)
                            yield sse_event('commentary', {
                                'text': burst, 'kind': 'narration', 'step': step_no,
                            })
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
                            # Weigh the CALL, not just the result. For Write and
                            # Edit the model generates the whole file into the
                            # call's input and the result is one line back, so a
                            # result-only figure reported ~nothing for the most
                            # expensive thing in the step. Measured off the JSON
                            # because that is the form the call takes in the
                            # prompt, escaping and all.
                            call_tokens = _estimate_tokens(
                                json.dumps(tool_input or {}, default=str))
                            if conversation_id:
                                conversations.append_commentary(
                                    conversation_id, text, level,
                                    tool_id=getattr(block, 'id', ''),
                                    command=_tool_command_text(tool_name, tool_input),
                                    step=step_no, call_tokens=call_tokens)
                            yield sse_event('commentary', {
                                'text': text, 'kind': level, 'step': step_no,
                                'call_tokens': call_tokens,
                            })

                        # An environment-changing tool just STARTED: remember its id
                        # so the descriptor is re-read when it ends. No longer pushes
                        # a "transitioning" frame -- the workspace polls /health, whose
                        # in-flight half is read live from the DAT-183 marker, so it
                        # shows the operation that is ACTUALLY running rather than one
                        # inferred from a tool name.
                        if _is_env_changing_tool(tool_name, tool_input):
                            pending_env_tool_id = getattr(block, 'id', '') or True

                    # Tool result
                    elif hasattr(block, 'tool_use_id'):
                        is_err = bool(getattr(block, 'is_error', False))
                        result_tool_id = getattr(block, 'tool_use_id', '')
                        result_content = str(getattr(block, 'content', '') or '')
                        # Weigh the FULL result before the display cap: this is
                        # what the result adds to the prompt, and the client
                        # cannot recompute it on a replay (it only ever sees the
                        # capped text). Sent live as well as persisted so both
                        # paths show the same estimator's number.
                        result_tokens = _estimate_tokens(result_content)
                        yield sse_event('tool_result', {
                            'tool_use_id': result_tool_id,
                            'content': result_content,
                            'is_error': getattr(block, 'is_error', False),
                            'result_tokens': result_tokens,
                        })
                        # Attach the (capped) output to the tool's commentary entry
                        # so the persisted Tool Detail carries command + output.
                        if conversation_id and result_tool_id:
                            conversations.attach_tool_output(
                                conversation_id, result_tool_id,
                                result_content[:_DETAIL_OUTPUT_CAP], is_err,
                                result_tokens=result_tokens)
                        if is_err:
                            err_text = 'A step reported an error'
                            if conversation_id:
                                conversations.append_commentary(
                                    conversation_id, err_text, 'error', step=step_no)
                            yield sse_event('commentary', {
                                'text': err_text, 'kind': 'error', 'step': step_no,
                            })

                        # An environment-changing tool just FINISHED: re-read the
                        # deployment now so the accounts record reflects the new
                        # environment mid-turn instead of waiting for turn end.
                        # Best-effort; never breaks the turn.
                        if pending_env_tool_id and (pending_env_tool_id is True
                                                    or pending_env_tool_id == result_tool_id):
                            pending_env_tool_id = None
                            try:
                                dep = await _fetch_deployment_state()
                                if dep:
                                    await _report_descriptor_to_accounts(
                                        conversation_id or "", dep['descriptor_text'], auth_token)
                            except Exception as e:
                                logger.warning("Mid-turn env read failed: %s", e)

                # This model round's usage -> push a live ticker so the status
                # bar tracks the context in real time.
                # Gated on step_usage (a round we have not already counted): the
                # SDK repeats one round's usage across every message of that
                # round, so accumulating per MESSAGE double- or triple-counted
                # every round. Pre-existing; only visible once the same repeat
                # showed up as duplicate step badges.
                # `tokens` is kept as the field name a client predating this
                # carries; its MEANING changed from a running new-token tally to
                # the context size, which is what the ticker now shows.
                if step_usage:
                    context_tokens = step_usage['new'] + step_usage['carried']
                    yield sse_event('ticker', {'tokens': context_tokens})

            # Stream events
            elif hasattr(msg, 'event'):
                ev = getattr(msg, 'event', {})
                if _LOG_RAW_USAGE:
                    # The one place a real PER-STEP output count could come
                    # from: `message_delta` carries the authoritative (and
                    # cumulative) output_tokens at the end of each step's
                    # stream, where message_start only carried a placeholder.
                    # Log only the usage-bearing events -- the delta firehose
                    # would be thousands of lines a turn.
                    try:
                        ev_type = ev.get('type') if isinstance(ev, dict) else None
                        if ev_type in ('message_start', 'message_delta', 'message_stop'):
                            logger.info("step %s stream %s: %s",
                                        step_no, ev_type, json.dumps(ev, default=str)[:600])
                    except Exception:   # diagnostics must never break a turn
                        pass
                yield sse_event('stream', {'event': ev})

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
            # Straight to accounts, rather than by way of the browser (DAT-235):
            # a record that depends on a tab being open is one that a headless or
            # CLI-driven change never writes at all.
            await _report_descriptor_to_accounts(
                conversation_id or "", deployment_state['descriptor_text'], auth_token)


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

            # Catch a standing decision about the environment that the model did
            # not report itself (DAT-214). Skipped when `set_environment_intent`
            # already fired this turn -- the model's own statement is the better
            # evidence, and letting an inference drawn from the same conversation
            # overwrite it is how the two mechanisms would fight.
            turn_key = _current_turn_key(conversation_id)
            if turn_key in _intent_recorded_this_turn:
                _intent_recorded_this_turn.discard(turn_key)
            else:
                intended = await classify_environment_intent(transcript, sidecar_usage)
                if intended:
                    await _report_foundry_intent_to_accounts(intended, "inferred", auth_token)

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

                # Persist the context size reached this turn, so the status bar
                # still shows it after a reload. Written BEFORE the usage
                # snapshot is emitted, so the snapshot carries it.
                if context_tokens:
                    conversations.set_context_tokens(conversation_id, context_tokens)
                    if isinstance(updated_usage, dict):
                        updated_usage['context_tokens'] = context_tokens

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
    # dirs the SDK will load (system + user-global). Per-project skills are
    # wired in once the project folder becomes the cwd.
    skills.ensure_user_plugin()
    loaded_plugins = [p["path"] for p in skills.build_plugins()]
    logger.info(f"  Skill plugins: {loaded_plugins or 'none'}")

    # Memory: scaffold the global (cross-project) memory store. Per-project
    # memory is scaffolded per project folder by conversations.ensure().
    memory.ensure_user_memory()
    logger.info(f"  User memory: {memory.USER_DIR}")
    # Fleet memory ships with the build (read-only). Absent is a valid state —
    # a self-hosted or pre-seed instance simply runs without the scope.
    if memory.FLEET_INDEX.exists():
        logger.info(f"  Fleet memory: {memory.FLEET_DIR}")
    else:
        logger.info("  Fleet memory: none shipped with this build")

    if check_api_mcp_reachable(DATAFYE_API_MCP_URL):
        logger.info(f"  Datafye API MCP: reachable at {DATAFYE_API_MCP_URL}")
    else:
        logger.warning(
            f"  Datafye API MCP: NOT REACHABLE at {DATAFYE_API_MCP_URL}. "
            f"Agent will start, but tool calls requiring the deployment will fail. "
            f"Check the foundry environment: datafye foundry local status"
        )

    # Keep the foundry observation fresh in the background, so /health never
    # waits on it. Interrogating costs real time when something is wrong (a dead
    # service makes the deployment API wait out its own reply timeout), and
    # /health is polled by accounts for dormancy decisions, by the upgrade cron
    # every minute, and by the SPA. An agent that goes quiet is indistinguishable
    # from a dead instance, which is the one thing it must never look like.
    # CLI_PATH threaded through, not left to the "datafye" default: the
    # observation now shells out to the CLI when the deployment API goes
    # quiet, and an operator who configured DATAFYE_AGENT_CLI_PATH would
    # otherwise get a different binary on that path than everywhere else.
    observer = asyncio.create_task(
        foundry.observe_forever(DATAFYE_DEPLOYMENT_API_URL, CLI_PATH))
    # The warm signal shares the reasoning but not the loop: it answers "is
    # work happening" rather than "is the environment usable", and accounts
    # consumes it to decide whether stopping this box would interrupt
    # something.
    warm_watcher = asyncio.create_task(warmth.refresh_forever(DATAFYE_DEPLOYMENT_API_URL))
    logger.info(
        f"  Foundry readiness: intent={foundry.read_intent()['intended']}, "
        f"observing {DATAFYE_DEPLOYMENT_API_URL} every {foundry.OBSERVE_INTERVAL_SECONDS}s")
    logger.info(
        f"  Warm signal: data-flow window {warmth.DATA_WINDOW_SECONDS}s, "
        f"refreshed every {warmth.REFRESH_INTERVAL_SECONDS}s")

    yield

    observer.cancel()
    warm_watcher.cancel()
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
        agent_version=AGENT_VERSION,
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
        active_proxied_apps=warmth.active_work(),
        foundry=read_foundry_state(),
    )


BOM_PATH = os.getenv("DATAFYE_AGENT_BOM_PATH", "/opt/datafye/agent/bom.json")


def _read_agent_version() -> Optional[str]:
    """The installed version, from the BOM the installer writes.

    Read ONCE at import, deliberately. /health is polled every minute by the
    upgrade cron, by accounts for dormancy and by the SPA, so it may only carry
    facts that cost no more than reading a variable -- the same rule that keeps
    the harness probe on /v1/bom instead. Caching is also exact rather than
    merely cheap here: an upgrade replaces the code and restarts the service,
    so the version cannot change under a running process.
    """
    try:
        with open(BOM_PATH) as f:
            version = json.load(f).get("agent_version")
        if version:
            return str(version)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    # A local run has no BOM. Fall back to the env var the installer also sets,
    # then to None -- never to a made-up number, since the whole point of the
    # field is to say which build is on the box.
    return os.getenv("DATAFYE_AGENT_VERSION") or None


AGENT_VERSION = _read_agent_version()


@app.get("/v1/bom")
async def bom():
    """Dependency bill-of-materials — the Datafye version this agent is built
    against. Datafye versions all components (platform, samples, CLI, docs)
    together, so it's a single version. Unauthenticated like /health (version
    numbers aren't sensitive); rendered on the Yukti agent surface.

    Also reports the Claude Code CLI actually running turns (DAT-215). It
    belongs here rather than on /health because it is a dependency fact, not a
    liveness one — and /health is polled every minute by the upgrade cron, by
    accounts for dormancy, and by the SPA, so it is the wrong place to add
    anything that ever costs more than reading a variable."""
    harness_info = harness.describe()
    try:
        with open(BOM_PATH) as f:
            document = json.load(f)
    except FileNotFoundError:
        document = {"agent_version": os.getenv("DATAFYE_AGENT_VERSION", "dev"),
                    "dependencies": {}, "note": "bom.json not present"}
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"could not read BOM: {e}")
    # Added rather than merged into `dependencies`: that block is the single
    # Datafye version, and the harness moves on a completely different cadence
    # (a pip range, resolved whenever a box was built). Folding them together
    # would imply a coherence that does not exist.
    document["harness"] = harness_info
    return document


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


# ── Resumable turns ────────────────────────────────────────────────
# A chat turn runs as a BACKGROUND task that buffers its SSE frames, so a client
# that drops mid-turn can RECONNECT (GET /v1/chat/resume) and replay from where
# it left off instead of losing the in-flight turn. Decoupling the turn from the
# HTTP response is what makes resume possible; the trade-offs it forces:
#   - an explicit Stop must cancel the task (a mere disconnect no longer does):
#     the client calls POST /v1/chat/stop.
#   - an abandoned turn (client gone, never resumes) is cancelled by a watchdog
#     once it has had no consumer for _TURN_ORPHAN_S, so it can't bill forever.
# Each buffered frame is tagged with an SSE `id:` = its sequence number; the
# client tracks the last id it saw and resumes with ?after=<seq>.
_TURN_GRACE_S = 120     # keep a finished turn's buffer this long (late resume)
_TURN_ORPHAN_S = 300    # cancel a running turn with no consumer for this long (parked turns survive a detour)
_turns: Dict[str, "_Turn"] = {}
_turn_sweeper_task: Optional["asyncio.Task"] = None


class _Turn:
    def __init__(self, turn_id: str):
        self.id = turn_id
        self.frames: List[str] = []          # buffered SSE frames; index == seq
        self.done = False
        self.task: Optional[asyncio.Task] = None
        self.cond = asyncio.Condition()
        self.consumers = 0
        self.idle_since = time.time()        # when consumers last dropped to 0
        self.created = time.time()


async def _turn_emit(turn: "_Turn", frame: str) -> None:
    async with turn.cond:
        seq = len(turn.frames)
        turn.frames.append(f"id: {seq}\n{frame}")
        turn.cond.notify_all()


async def _run_turn(turn: "_Turn", message: str, conversation_id: Optional[str],
                    algo_id: Optional[str], auth_token: Optional[str]) -> None:
    """Background producer: run the agent turn, buffering every SSE frame. On an
    explicit stop (CancelledError) or an error, close the underlying stream (so the
    SDK subprocess is torn down promptly) and emit a terminal event so a
    reconnecting client always sees the turn end."""
    gen = tracked_stream_agent_response(
        message=message, conversation_id=conversation_id,
        algo_id=algo_id, auth_token=auth_token)
    stopped = False
    err: Optional[Exception] = None
    try:
        async for frame in gen:
            await _turn_emit(turn, frame)
    except asyncio.CancelledError:
        stopped = True   # explicit stop / orphan watchdog
    except Exception as e:
        logger.exception("turn %s failed", turn.id)
        err = e
    finally:
        # Close the generator so query()'s subprocess is killed now, not at GC.
        try:
            await gen.aclose()
        except Exception:
            pass
        if stopped:
            await _turn_emit(turn, sse_event("commentary", {"text": "Stopped.", "kind": "notable"}))
            await _turn_emit(turn, sse_event("done", {"stopped": True}))
        elif err is not None:
            await _turn_emit(turn, sse_event("error", {
                "message": _turn_error_message(err),
                "error_type": type(err).__name__,
            }))
            await _turn_emit(turn, sse_event("done", {}))
        async with turn.cond:
            turn.done = True
            turn.cond.notify_all()


def _turn_error_message(err: Exception) -> str:
    """What the user should read when a turn dies (DAT-204).

    The buffer overflow surfaces as `Failed to decode JSON: JSON message exceeded
    maximum buffer size of 1048576 bytes`, which describes our transport rather
    than anything the reader did or can act on. It also reads like data
    corruption, which sends people looking in the wrong place -- the actual cause
    is one tool result being too big, and the actual remedy is to ask for less.

    Everything else keeps its own text: a message that is merely technical is
    better than one that is wrong.
    """
    text = str(err)
    if "exceeded maximum buffer size" in text:
        return (
            "That step produced more output than one message can carry, so the turn "
            "could not be completed. Anything already written to files is safe. Ask "
            "again for a smaller piece of it -- a summary, a page, or the specific "
            "part you need -- rather than the whole thing."
        )
    return text


async def _drain_turn(turn: "_Turn", after: int) -> AsyncIterator[str]:
    """Consumer: replay buffered frames after `after`, then stream live until the
    turn is done. Several consumers can drain the same turn concurrently (the
    original request and any resume reconnect)."""
    async with turn.cond:
        turn.consumers += 1
    try:
        i = max(0, after + 1)
        while True:
            async with turn.cond:
                while i >= len(turn.frames) and not turn.done:
                    await turn.cond.wait()
                new = turn.frames[i:]
                done = turn.done
            for frame in new:
                yield frame
                i += 1
            if done and i >= len(turn.frames):
                break
    finally:
        async with turn.cond:
            turn.consumers -= 1
            if turn.consumers <= 0:
                turn.idle_since = time.time()


async def _turn_sweeper() -> None:
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for tid, turn in list(_turns.items()):
            if turn.done:
                if now - turn.idle_since > _TURN_GRACE_S:
                    _turns.pop(tid, None)
            elif turn.consumers <= 0 and now - turn.idle_since > _TURN_ORPHAN_S:
                logger.info("cancelling orphaned turn %s (no consumer for %ds)", tid, _TURN_ORPHAN_S)
                if turn.task and not turn.task.done():
                    turn.task.cancel()


def _ensure_turn_sweeper() -> None:
    global _turn_sweeper_task
    if _turn_sweeper_task is None or _turn_sweeper_task.done():
        _turn_sweeper_task = asyncio.create_task(_turn_sweeper())


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
    - ticker: The conversation's CONTEXT SIZE for the live status ticker {tokens},
      emitted per model round as new + carried -- the whole prompt at that step.
      The field name predates the meaning: it used to be a running new-token
      tally, which reads in the tens once the prefix is cached
    - result: Final result {text, session_id, duration_ms, cost_usd}
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
    # The turn runs as a background task buffering its frames; the response drains
    # that buffer, so a dropped connection can resume via GET /v1/chat/resume.
    turn = _Turn(uuid.uuid4().hex)
    _turns[turn.id] = turn
    _ensure_turn_sweeper()
    # First frame carries the turn id so the client can resume this exact turn.
    await _turn_emit(turn, sse_event("turn", {"turn_id": turn.id}))
    turn.task = asyncio.create_task(
        _run_turn(turn, request.message, request.conversation_id, request.algo_id, auth_token))
    return StreamingResponse(_drain_turn(turn, -1), media_type="text/event-stream")


@app.get("/v1/chat/resume", dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def chat_resume(turn_id: str, after: int = -1):
    """Reconnect to an in-flight (or just-finished) turn and replay its frames
    after `after`, then continue live. 404 if the turn is unknown/expired — the
    client then falls back to reloading /history."""
    turn = _turns.get(turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="turn not found or expired")
    return StreamingResponse(_drain_turn(turn, after), media_type="text/event-stream")


@app.post("/v1/chat/stop", dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def chat_stop(turn_id: str):
    """Explicitly stop an in-flight turn (Stop button / Esc). Cancels the
    background task; the producer emits a Stopped + done and the drain ends."""
    turn = _turns.get(turn_id)
    if turn and turn.task and not turn.task.done():
        turn.task.cancel()
        return {"stopped": True}
    return {"stopped": False}


@app.post("/v1/foundry/stop",
          dependencies=[Depends(require_bootstrapped),
                        Depends(auth.require_accounts_lifecycle_jwt)])
async def foundry_stop():
    """Bring the foundry down cleanly, ahead of the box being stopped (DAT-125).

    Called by the accounts idle monitor immediately before StopInstance, for
    both the idle `Dormant` stop and a deliberate user `Stop`. Before this, the
    instance was simply pulled out from under a running environment: the Rumi
    applications were killed mid-write, risking unflushed transaction logs, and
    the containers were never marked stopped -- so `--restart unless-stopped`
    faithfully restored them on the next boot with no applications inside, the
    DAT-171 wedge. Observed doing exactly that on u1, sixteen minutes into a
    provision.

    The reply is what the caller needs to DECIDE with, not a success flag:
    `busy` means abort the box stop (something owns the environment), `failed`
    and `absent` mean carry on. See `foundry.graceful_stop`.

    Gated by a purpose-scoped `agent-lifecycle` token, not by the user JWT and
    not left open like `/v1/credentials/update`. That endpoint only writes a
    cache value; an unauthenticated stop would let anyone who can reach the
    agent take a user's environment down. And a *user* token would be the
    wrong instrument in the other direction -- this is accounts acting as
    accounts, so borrowing a person's identity for it would mint a
    user-equivalent credential on every dormancy tick."""
    result = await graceful_stop(CLI_PATH)
    logger.info("Foundry graceful stop: %s (%s)", result["status"], result["detail"])
    return result


class FoundryIntent(BaseModel):
    """What accounts has decided this box's foundry should be doing."""
    intended: str            # "running" | "stopped"
    source: Optional[str] = None


@app.post("/v1/foundry/intent",
          dependencies=[Depends(require_bootstrapped),
                        Depends(auth.require_accounts_lifecycle_jwt)])
async def foundry_intent(intent: FoundryIntent):
    """Receive the foundry intent accounts has recorded (DAT-198).

    Push, not pull, for two reasons. The agent is deliberately receive-only --
    accounts is the only writer in the relationship, and the agent-to-accounts
    calls that do exist all forward the *user's* JWT. And that breaks exactly
    where it is needed: the boot service reconciles the foundry before any user
    exists, so there is no JWT to query with and a pull would need a new machine
    credential.

    What lands here is a REPLICA, not the record. Accounts owns intent because
    that is where the user's decision actually arrives, and because a sandbox
    rebuild would destroy anything held only on the box.

    ⚠️ Intent is not per-mutation. The installer's upgrade, a `dataset add`, an
    engineer's debugging `stop` are all mutations and none of them are policy.
    The decisive case is dormancy: the idle monitor stopping a box must NOT
    record `stopped`, or the box wakes and correctly declines to restore an
    environment the user never asked to lose."""
    try:
        record = foundry.write_intent(intent.intended, intent.source or "accounts")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Unlike most pushes this one must report failure: if the replica was not
        # written, the boot service will reconcile against the previous value and
        # accounts needs to know its push did not land.
        logger.error("Could not record foundry intent: %s", e)
        raise HTTPException(status_code=500, detail=f"could not record intent: {e}")
    return record


@app.post("/v1/activity", status_code=204,
          dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def activity():
    """Presence heartbeat from the SPA (DAT-169).

    The accounts idle monitor measures idleness from `last_chat_activity_at`,
    which until now advanced ONLY when a chat turn ran. So a user reading a
    backtest result, studying a scorecard, or thinking for half an hour looked
    exactly like a user who had closed the tab -- and their box dormed
    underneath them.

    Deliberately the same field a chat turn bumps, rather than a second one.
    The monitor's question is "when was this box last of use to somebody", and
    reading is as good an answer as typing; splitting it would make accounts
    take a max over two fields for no gain.

    ⚠️ This only ever PREVENTS dormancy, it never reverses it: a stopped box
    cannot receive the heartbeat. Waking stays the auto-wake path's job.

    ⚠️ The caller must send this only while its tab is VISIBLE. A hidden tab
    that kept pinging would pin every abandoned browser session's box awake
    forever, and dormancy would stop saving anything. The agent cannot check
    that -- it is the frontend's half of the contract.

    Cheap by construction: one assignment, no I/O. It is called on an interval
    by every open tab, so anything more would be a per-user background load
    that exists only to say "still here"."""
    global last_chat_activity_at
    last_chat_activity_at = int(time.time() * 1000)


@app.get("/v1/skills", dependencies=[Depends(require_bootstrapped), Depends(auth.require_self_jwt)])
async def get_skills(conversation_id: Optional[str] = None):
    """List the skills available to the agent, across all tiers:
      - system: predefined, read-only (shipped with the agent)
      - user-global: agent-authored, reusable across projects
      - user-project: specific to one project (included when conversation_id is given)

    The frontend uses this to show a skill list; "running" a skill is a normal
    chat turn (e.g. "use the <name> skill"), which the model services via the
    Skill tool — there is no separate execution endpoint."""
    cwd = str(conversations.project_dir(conversation_id)) if conversation_id else None
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
    """Permanently delete a project: the agent-side folder (meta, algo code,
    per-project memory + skills). 404 if the agent never materialised it.
    Accounts deletes its project registry record separately, so a project that
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
    # A silent 404 here means every dormancy stop is ungraceful and NOTHING
    # says so: accounts treats a missing endpoint as an older agent and
    # proceeds to stop the box, which is the pre-DAT-125 behaviour restored
    # invisibly.
    ("POST", "/v1/foundry/stop"),
    # Same reasoning: a silent 404 here means accounts' intent pushes vanish and
    # every box quietly falls back to the default, which is indistinguishable
    # from working until somebody stops their environment and it comes back.
    ("POST", "/v1/foundry/intent"),
    # A silent 404 here means every reading user's box dorms underneath them,
    # and the SPA cannot tell a missing endpoint from a delivered heartbeat.
    ("POST", "/v1/activity"),
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
