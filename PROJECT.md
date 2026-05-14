# Datafye Agent

## What Is This?

This is the brain behind Datafye's AI-powered algo development experience. When a user sits down in the Datafye Agent App (the frontend) and says "I want to build a strategy that buys stocks when their 10-day moving average crosses above the 50-day," this backend is what turns that sentence into a working, testable trading algorithm.

Think of it as giving every algo developer their own personal quant assistant — one that knows the Datafye platform inside out, can spin up data environments, write Python code, test strategies against historical data, and set up simulated trading. All through conversation.

## How It Works

### The Big Picture

```
User (browser)
    ↓ chat message
Datafye Agent App (frontend)
    ↓ POST /v1/chat (SSE)
Datafye Agent (this service)
    ↓ Claude Agent SDK
Claude (Anthropic)
    ↓ tool calls
Local Machine:
  ├── Datafye Docs (on disk)
  ├── Datafye CLI (foundry, trading, data)
  ├── User's Python algo code (workspace)
  └── GitHub (algo repos)
```

Each user gets their own instance of this service. That's not an accident — algo development is stateful. The agent needs to remember what you're building, what environment is running, what you tried last. A shared service would be a concurrency nightmare and a security risk (user A's broker credentials leaking to user B).

### The Claude Agent SDK

At the heart of this service is the Claude Agent SDK's `query()` function. It's an async generator that yields a stream of messages as Claude thinks, calls tools, and generates responses. We wrap this in a FastAPI SSE endpoint that the frontend consumes.

The SDK gives Claude access to tools — file operations (Read, Write, Edit), shell execution (Bash), search (Glob, Grep), and planning tools. We add the Datafye-specific capabilities through the system prompt and the Bash tool: the agent can run `datafye foundry local provision`, `curl` the REST API, execute Python scripts, and manage git repos.

### The System Prompt

The system prompt (in `prompt.py`) is assembled at runtime from the current state:
- Where the docs are on disk
- Where the CLI is
- What credentials the user has configured
- Which algo they're currently working on

This means the agent always knows what it can and can't do. If the user hasn't configured their Massive (Polygon) API key, the agent knows it can't provision a SIP dataset and will tell the user to add their key in Settings rather than failing silently.

### Session Continuity

The Claude Agent SDK supports session resumption. When a user sends a message, we check if there's an existing session for their conversation. If so, we resume it — Claude remembers the entire conversation history, what files it created, what environments are running. This is critical for algo development where a single strategy might take dozens of back-and-forth exchanges to refine.

### Credentials Management

User credentials (data provider API keys, broker credentials) flow through in two ways:

1. **At launch**: Environment variables set when the user's instance starts
2. **At runtime**: The frontend's Settings modal calls `POST /v1/credentials` to update keys without restarting the service

The agent's system prompt is rebuilt on every chat request, so credential changes are immediately reflected in what the agent tells the user it can do.

**ConnectTrade credentials are a special case.** Most credentials are symmetric — one key, one user, done. ConnectTrade has two layers: a *client* identity (who Datafye is, as a ConnectTrade tenant) and a *user* identity (who this particular sandbox's human owner is inside that tenant). They have very different lifetimes:

- **Client creds** (`client_id` / `client_secret`): today these come from env vars (`DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID/_SECRET`). They're the same for every sandbox. TODO: fetch them from the Datafye accounts-manager so we don't have to bake them into AMIs or distribute them through env files.
- **User creds** (`user_id` / `user_secret`): lazy-provisioned on the first `POST /v1/broker/connections` call by hitting ConnectTrade's `POST /users`, then persisted to `~/.datafye/agent/broker_user.json` (mode `0600`). A single file is fine here because every sandbox is single-user by design — there's nobody else to collide with. TODO: migrate this into accounts-manager per-user storage so a user who blows away their sandbox and gets a new one doesn't end up with an orphaned ConnectTrade user.

The broker module binds the same shared `credentials` dict that the rest of the service uses, so env-provided creds, runtime `/v1/credentials` updates, and lazy-provisioned user creds all converge in one place.

## Architecture Decisions & Why

### Why Per-User Instances (Not Shared)?

Three reasons:
1. **Security**: Each user's broker credentials, API keys, and algo code live in their own process
2. **State isolation**: The agent's session, working directory, and environment are user-specific
3. **Resource control**: One user's heavy backtest doesn't starve another's chat

The tradeoff is operational complexity — you need orchestration to spin up/down instances. But for algo development with real financial credentials, isolation isn't optional.

### Why Local Docs Instead of an MCP Server?

The docchat backend uses a GitBook MCP server for documentation. We deliberately chose to put the docs on disk instead:
- **Faster**: No HTTP round-trips for every doc lookup
- **Reliable**: No dependency on GitBook's uptime
- **Complete**: The agent can Glob and Grep across the entire doc set, not just search by query

The docs are synced from the `datafye-docs` repo.

### Why Python-Only Algos (Not SDK/Java)?

The Datafye platform has a Java-based Algo SDK for high-performance, integrated strategies. We're not using it here because:
- The target user may not know Java
- Python is the lingua franca of quant finance
- Data Cloud Only mode (REST/WebSocket APIs) is more accessible
- SDK-based algos can be added later as a separate path

### Why Conversational Dataset Config (Not Forms)?

A dropdown can't capture "I want to use daily OHLC and EMA data for US tech stocks, plus some alternative sentiment data." The agent can. It understands intent, maps it to the right datasets and schemas, and provisions the environment — all in one conversation. This is particularly important for users who don't know what datasets exist or what schemas they need.

## The Algo Development Flow

Here's what a typical session looks like from the agent's perspective:

1. **User describes idea**: "I want to build a mean-reversion strategy on AAPL and MSFT"
2. **Agent determines data needs**: SIP dataset, ohlc-1d and sma-1d schemas, symbols AAPL and MSFT
3. **Agent checks credentials**: Massive API key configured? Yes → proceed. No → "Please add your Massive API key in Settings"
4. **Agent builds descriptor**: Creates a YAML deployment descriptor
5. **Agent provisions environment**: Runs `datafye foundry local provision -x descriptor.yaml`
6. **Agent writes algo code**: Creates Python files in the workspace
7. **Agent tests**: Downloads historical data, runs the algo, collects results
8. **Agent presents results**: Returns, win rate, trade count — the frontend shows these in the scorecard and charts
9. **Iteration**: User says "try a shorter lookback period" → agent modifies and re-tests
10. **Simulated trading**: If broker is configured, agent provisions a trading environment

## Broker Integration

Eventually a strategy has to touch real money — or at least money-shaped money (paper trading). That means connecting the user's brokerage account. We don't want to be in the business of collecting and storing broker OAuth tokens ourselves, so we punt that to **ConnectTrade**, a broker-aggregator that speaks a single API and handles the OAuth dance for every supported broker.

The agent exposes four endpoints under `/v1/broker/*`:

- `GET /v1/broker/brokers` — returns the list of brokers Datafye supports. This mirrors the `StocksBroker` enum in `datafye-roe` (ALPACA, LIGHTSPEED, TASTYTRADE, TRADESTATION, TRADEZERO, WEBULL) so the frontend and the trading engine can't drift apart.
- `GET /v1/broker/connections` — proxies ConnectTrade's `GET /connections` and flattens the response to `{connection_id, broker, status, type, accounts}` so the frontend doesn't have to know ConnectTrade's wire format.
- `POST /v1/broker/connections` — takes `{type, broker}`, validates the broker, and calls ConnectTrade's `POST /connections` with the broker pre-selected and our `redirect_url` set. ConnectTrade returns a `connection_request_url`; we remap that to `authorization_url` and hand it back to the caller.
- `DELETE /v1/broker/connections/{id}` — proxies the delete.

### Who opens the popup?

A subtle design point: the agent only *produces* the OAuth URL. It does **not** open a browser window. Two reasons — a backend process literally can't open a browser window on the user's machine, and even if it could, browsers only allow popups in response to a user gesture (a click). So the flow is: frontend clicks "Connect Alpaca" → calls `POST /v1/broker/connections` → gets back `authorization_url` → opens the popup itself. The agent's job ends at producing the URL.

### Data flow

```
frontend                agent                        ConnectTrade
   │  POST /v1/broker/connections
   │ ───────────────────▶
   │                        POST /connections
   │                        (with client+user creds,
   │                         broker, redirect_url)
   │                       ──────────────────────────▶
   │                       ◀──────────────────────────
   │                        connection_request_url
   │ ◀───────────────────
   │  authorization_url
   │
   │  window.open(authorization_url)  [user gesture]
   ▼
(user completes OAuth on ConnectTrade's hosted UI)
```

The default `redirect_url` is `https://developer.datafye.io/broker-callback.html` — a static page that just signals the parent window that OAuth finished.

We use `permission_mode="bypassPermissions"` which means the agent can execute anything. This is necessary for CLI operations and Python execution, but it means:
- A malicious prompt could potentially access the host system
- The per-user isolation is critical — each user gets their own EC2 instance in a Rumi private cloud

### Session Memory Has Limits

The Claude Agent SDK session stores conversation history, but there's a context window limit. Long algo development sessions might hit it. The SDK handles this with automatic context compression, but be aware that very early conversation context might get summarized or dropped.

### Credential Rotation

If a user updates their API key in Settings while an environment is running with the old key, the environment won't automatically pick up the new key. The agent would need to re-provision. This is a known edge case.

### ConnectTrade Gotchas (Learned the Hard Way)

Two surprises from wiring up the broker module that are worth writing down so the next person doesn't spend a Saturday on them:

1. **`user_secret` can go stale.** We observed a case where a previously-working `user_secret` stopped authenticating — ConnectTrade would reject it with an auth error even though nothing on our side had changed. The recovery path is: call ConnectTrade's `rotate-secret` endpoint to mint a fresh secret, overwrite the one in `broker_user.json`, and retry. The agent doesn't do this automatically yet; if you see auth failures on a previously-working sandbox, rotate first, blame yourself second. (Moving user-secret storage into the accounts-manager will let us rotate once and fan out to every sandbox the user owns, instead of per-sandbox manual fixes.)
2. **A brokerage account can only be linked to one ConnectTrade user per tenant.** If `POST /connections` comes back with a 409, the instinct is "my request is malformed." It usually isn't. It means another ConnectTrade user in the Datafye tenant has already linked that same Alpaca (or Lightspeed, or whatever) account. This shows up in practice when a developer tests with their personal Alpaca account across multiple sandbox users — the second sandbox gets a 409 that looks like a code bug but is actually ConnectTrade doing the right thing. Surface this to the user as "this brokerage account is already linked to another Datafye user," not as a generic error.

## Deployment

The agent runs **natively** on the host (not in a Docker container). This was a deliberate decision — the agent uses the Datafye CLI to spin up Datafye environment containers via Docker, and Docker-in-Docker is painful. Since the whole instance is dedicated to one user, there's no isolation benefit from containerizing the agent. The AMI is the packaging.

Two deployment modes:
- **Hosted**: Pre-baked AMI in a Rumi private cloud. Each user gets a sandbox instance at `{username}.app.datafye.io`, proxied through a jump server with wildcard SSL. Managed by the datafye-accounts service (elastic stop/start based on activity).
- **Standalone (Marketplace)**: Minimal AMI with a first-boot script. User provides their Anthropic key and DNS via EC2 user data. Everything downloads and installs on first boot.

The agent source is open source — the value is in the Datafye platform, not the glue code. Power users can fork and customize the prompt, add tools, tweak behavior.

## What's Next — Receive-Only Integration with `datafye-accounts`

The next phase moves the agent from "standalone process the user configures by hand" to "receive-only worker in the Datafye sandbox plane." The design was settled in the 2026-05-13 design session; this section captures it.

The shape of the agent's role contracts: it **receives** credentials and JWTs, it **does** work, it never **asks** accounts for anything. Accounts is the only writer in the relationship.

### Thing 1: Identity bootstrap from IMDS

At process startup, the agent reads its EC2 `Name` tag via Instance Metadata Service:

```python
# pseudo
name_tag = read_imds("/meta-data/tags/instance/Name")        # e.g. "agents-u123456"
username = name_tag.removeprefix("agents-")                  # "u123456"
```

The agent stores this on its global state and refuses to start if the tag is missing. The username is the single piece of identity the agent has — no secrets, no shared keys, just a tag that the AwsProvisioner sets at launch time. `AwsProvisioner.launchServiceInstance` always tags the instance with `Name=<instanceName>`, so as long as the provisioner is invoked with `instanceName == username`, the agent picks up its identity from AWS itself.

### Thing 2: Encrypted on-disk credentials store

The agent doesn't have a Rumi store (it's Python/FastAPI, not a Rumi application). Credentials live in a single binary file with light encryption:

- Path: `~/.datafye/agent/credentials.bin` (mode 0600)
- Format: msgpack
- Encryption: `cryptography.fernet`. Key derived deterministically from the EC2 instance ID (`sha256("datafye-agent-creds-v1::" + instance_id)`, base64-urlsafe-encoded). The instance ID comes from IMDS — same call as the identity bootstrap. No key persisted to disk; the key is reconstructable on every agent restart.
- Threat model: defends against casual file inspection and against leaked EBS snapshots (snapshot doesn't contain the instance ID, so key can't be derived offline). Does not defend against an attacker who has shell on the running instance — at that point they can read IMDS anyway. That's acceptable; encryption-at-rest is one layer of many.
- Replaces today's `~/.datafye/agent/broker_user.json` plain-JSON file. Existing ConnectTrade user creds fold into this same store as one of the providers.
- Compute a `credentialsGeneration` UUID (deterministic hash of contents) on load and on update; expose in `/health`.

### Thing 3: Endpoint changes

- **New: `POST /v1/credentials/update`** — body `{ provider, value }`. Accounts-only push. Updates in-memory cache + persists to `credentials.bin` atomically. Returns 204.
- **Update `GET /health`** to include: `credentialsGeneration`, `lastChatActivityAt`, `runningJobs`, `activeProxiedApps`. Accounts polls this for both idle detection and cache-loss recovery (if `credentialsGeneration` differs from what accounts last pushed, accounts re-pushes everything — see the [datafye-accounts PROJECT.md](../datafye-accounts/PROJECT.md) idle-monitor section).
- **Remove the existing direct-write `POST /v1/credentials`** (or repurpose to return 410 Gone with a "use accounts" message). The frontend stops writing credentials directly to the agent in the new model — all writes go through accounts.
- **JWT validation middleware** on `/v1/chat`, `/v1/broker/*`. Verify accounts-signed JWT against accounts' JWKS, check `sub == self.username`. Reject otherwise.

### Why this shape

- **No agent → accounts calls** means no shared secret to bootstrap, no IAM-signing layer, no fallback mode when JWTs aren't available. Background tasks use the cached credentials, which are kept fresh by accounts' push + the generation-counter recovery mechanism.
- **The agent never sees its own credentials before they're pushed.** Even on first boot, the agent has an empty cache until accounts (driven by the user's Settings activity) pushes values in. That's by design — accounts is the source of truth.
- **No "stale cache + no user connected" race.** Credentials only change when the user takes action; the user is by definition online during that action; the push lands on the live agent before the user disconnects.

### Implementation order

| Order | Item |
|---|---|
| 1 | Thing 1 (IMDS identity) + Thing 2 (credentials store) — can be tested against the existing `gkumar74` sandbox by manually setting the `Name` tag and bootstrapping a few credentials. |
| 2 | Thing 3 (new endpoint + health changes + JWT middleware) — requires accounts to be able to push, so this lands after `datafye-accounts` Chunks 1 + 4. |

### Smaller follow-ups already on the list

- SDK-based algos (Java / Datafye SDK path alongside Python)
- Live trading (currently capped at simulated/paper)
