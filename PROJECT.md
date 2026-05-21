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

The SDK gives Claude access to tools — file operations (Read, Write, Edit), shell execution (Bash), search (Glob, Grep), and planning tools. We add the Datafye-specific capabilities through the system prompt and the Bash tool: the agent can run `datafye foundry local provision`, `curl` the REST API, execute Python scripts, and manage git repos. The exact allowed set lives in `INTERNAL_TOOLS` in `main.py`.

**One tool we deliberately removed: `AskUserQuestion`.** It's the Claude Code harness's *structured-prompt* tool — in the Claude Code CLI it renders an interactive multiple-choice question. The Datafye workspace has no UI handler for it, so when the model reached for it, the question simply vanished into the void — the user saw the agent go quiet instead of being asked anything. Dropping it from `INTERNAL_TOOLS` forces the model to fall back to asking its question inline as ordinary chat text, which the frontend already renders. The broader lesson: an agent's tool list has to match the *surface it's actually running on*, not the SDK's full menu — a tool with no handler is worse than no tool, because it fails silently.

### The Anthropic Key Is a Credential, Not a Setting

It would be tempting to treat the Anthropic API key as plumbing — a startup env var, set once, assumed present. We deliberately don't. The key is just another **credential**: it lives in the same encrypted credentials store as the user's data-provider keys, under `anthropic_api_key`, and it arrives through the same accounts push channel as everything else.

The Claude Agent SDK runs Claude in a subprocess and that subprocess reads `ANTHROPIC_API_KEY` from the environment. So whenever the key changes — at bootstrap, or on a later credentials push — `_apply_anthropic_key()` syncs the stored value into `os.environ["ANTHROPIC_API_KEY"]` and then validates it against the Anthropic API. The agent tracks `anthropic_key_status` — one of `missing`, `ok`, `invalid`, or `unvalidated` (a network blip the agent treats as a soft pass) — and reports it on `/health`.

The payoff is that the agent **starts and stays manageable with no Anthropic key at all**. It can be bootstrapped, accept credential pushes, answer health probes — everything except chat. `/v1/chat` returns 503 when the key is missing and 502 when it's invalid, so the frontend can show a precise "add an Anthropic key" message instead of the agent failing to boot or crashing mid-stream. This matters because a sandbox might be provisioned before the user has chosen a billing plan or entered a bring-your-own key.

### The System Prompt

The system prompt (in `prompt.py`) is assembled at runtime from the current state:
- Where the docs are on disk
- Where the CLI is
- What credentials the user has configured
- Which algo they're currently working on

This means the agent always knows what it can and can't do. If the user hasn't configured their Massive (Polygon) API key, the agent knows it can't provision a SIP dataset and will tell the user to add their key in Settings rather than failing silently.

### Session Continuity

The Claude Agent SDK supports session resumption. When a user sends a message, we check if there's an existing session for their conversation. If so, we resume it — Claude remembers the entire conversation history, what files it created, what environments are running. This is critical for algo development where a single strategy might take dozens of back-and-forth exchanges to refine.

Originally that session-id lookup was an in-memory dict, which meant a restarted agent forgot every session — a fresh sandbox boot would start every conversation from scratch even though the SDK's own transcript was still on disk. Now the SDK session id is persisted (see [Conversations: Projects That Survive a Restart](#conversations-projects-that-survive-a-restart)), so `/v1/chat` resumes the right SDK session even across a process restart. The in-memory dict is kept only as a fallback for conversations that have no on-disk record.

### Conversations: Projects That Survive a Restart

What the frontend calls a **project** the agent calls a **conversation**, and `conversations.py` is its little on-disk database — one JSON file per conversation under `~/.datafye/agent/conversations/<id>.json`. Each record holds the human-readable name, the message history (user + assistant turns), a *commentary* log (the audit trail of background activity — see below), and the SDK session id.

Three design choices are worth calling out:

- **Plain JSON, not the encrypted store.** Conversation content isn't a secret key, and the Claude Agent SDK already writes its own transcripts to disk unencrypted — encrypting an *index* of those would buy nothing. Files are mode `0600` and written via temp-file-plus-atomic-rename, so a crash mid-write can't truncate an existing file. (Contrast with `credentials.bin`, which *is* encrypted because it holds Fernet-protected secrets.)
- **`ensure()` vs `create()` — who mints the id.** This is the load-bearing distinction. The accounts service is now the authoritative project registry: it mints project ids and the frontend creates/lists projects against accounts, not the agent. So a chat turn arrives with an *accounts-minted* `conversation_id` the agent has never seen. `conversations.ensure(id)` lazily materialises a local record for that exact id (never minting its own), which is what makes `append_message`/`append_commentary` actually persist — those helpers no-op when no file exists, so `/v1/chat` calls `ensure()` first. The agent's own `create()` (and the `POST`/`GET /v1/conversations` endpoints that use it) are now **legacy/unused** — left in place because they're harmless, but no longer on the frontend's path.
- **No per-user namespacing or locking.** The agent serves exactly one user, so there's nobody to collide with; the atomic rename is the only concurrency control needed.

The lesson here is a recurring one in this codebase: **decide who owns identity, then make every other component a follower.** Just as accounts owns the agent's *identity* (bootstrap push) and its *credentials* (credentials push), it now owns the *project registry* too. The agent's job is to materialise local state for ids it's handed, never to invent them — which keeps the agent and accounts from drifting into two competing lists of "what projects exist."

### Commentary: A Live Activity Feed

While Claude works, it calls a lot of tools — reading files, grepping, running shell commands, hitting the Datafye environment. Streaming every one of those to the user would be noise. So `_tool_commentary()` filters: only *meaningful background work* — `Bash` commands and MCP calls — becomes a human-readable commentary line ("Running: datafye foundry local provision ...", "Querying the Datafye environment"). File-level tools (Read/Edit/Grep) are too granular and are skipped.

Each commentary line is both emitted live as a `commentary` SSE event *and* appended to the conversation's on-disk commentary log, so the activity panel can be replayed when a user reopens a project (`GET /v1/conversations/{id}/history` returns both `messages` and `commentary`).

### Reflecting the Environment Back to the Frontend

The agent can spin up a Datafye environment, but the frontend needs to *show* what's running — which datasets, which symbols, backtest vs paper-trading, which broker. Rather than try to track that state by parsing the agent's own tool calls (fragile), we read it from the source of truth: after each chat turn, `_fetch_deployment_state()` makes a best-effort call to the deployed environment's deployment REST API (`GET .../deployment/{descriptor,datasets,symbols}` at `DATAFYE_AGENT_DEPLOYMENT_API_URL`). If no environment is up — connection refused, 404, no descriptor — it returns `None` and the agent simply emits nothing. The `descriptor` call is load-bearing; `datasets` and `symbols` are enrichment and tolerated to fail.

On success the agent emits two SSE events:

- **`descriptor`** — the raw deployment-descriptor YAML text, which the frontend relays verbatim to accounts (accounts keeps the canonical record of what each project deployed).
- **`env_status`** — a parsed, frontend-friendly summary: `{status, env_type, datasets, symbols, broker, mode}`. `mode` is the descriptor's `mode` (`backtest`/`paper`); `env_type` is the friendly label ("Foundry" for backtest, "Trading" for paper).

There's a gotcha buried in that payload that cost us a confusing afternoon, so it's worth writing down: **the environment-type field is keyed `env_type`, not `type`.** Every SSE frame is `{type: <event-name>, ...payload}` — `sse_event()` sets `type` to the event name (`env_status`, `descriptor`, etc.). If the payload *also* carried a `type` key, the spread would clobber the frame discriminator and the frontend's event router would mis-dispatch the message. Renaming the field to `env_type` sidesteps the collision entirely. The general rule: never put a `type` key in an SSE payload that gets spread into a frame whose discriminator is also `type`.

### Bootstrap: How the Agent Learns Who It Is

A freshly-launched agent is a blank slate. It doesn't know its username, it doesn't have the key to decrypt its own credentials, and it has no Anthropic key to talk to Claude. So it doesn't pretend otherwise — it boots into an **awaiting-bootstrap** holding state. In that state exactly two endpoints answer: `GET /health` (so accounts can see it's alive and not yet bootstrapped) and `POST /bootstrap`. Every user-facing endpoint returns HTTP 503, enforced by a single `require_bootstrapped` FastAPI dependency. Nothing runs against a `None` identity.

The accounts service drives it out of that state. Once the instance is reachable, accounts calls `POST /bootstrap` with an **accounts-signed JWT** in the `Authorization: Bearer` header (`purpose=agent-bootstrap`, verified against the accounts JWKS). The token carries two claims:

- `user_id` — the agent's identity from this moment on.
- `creds_key` — the Fernet key for the encrypted credentials store. It is `base64url(HMAC-SHA256(K_master, username))`, where `K_master` is an accounts-side secret the agent never sees. The agent receives the *derived* key, not the master secret.

The handler configures auth, opens the credentials store with `creds_key`, syncs the Anthropic key, exports the rest of the stored credentials into the process environment (`_apply_credentials_env()`, so the CLI's `${VAR}` substitution resolves), and leaves the holding state. It's idempotent for the same user — accounts re-pushes after a restart and the agent just re-binds to the same identity — but a bootstrap for a *different* user is rejected with a 409. An agent is one user's agent for life.

Why a push instead of the agent reading its own EC2 `Name` tag from instance metadata (the old model)? Two reasons. First, it keeps accounts as the single source of truth — the same design principle that runs through the whole sandbox plane: the agent *receives*, it never *asks*. Second, it lets accounts hand the agent its credentials-store key without that key ever being derivable from anything on the instance itself. A leaked EBS snapshot is just an encrypted blob; the key lives only in accounts and in the running process's memory.

### Credentials Management

User credentials (data provider API keys, broker credentials, and the Anthropic key itself) live in an encrypted on-disk store, opened at bootstrap with the delivered `creds_key`. Updates flow through one channel:

- **The accounts push**: The frontend's Settings modal writes to the accounts service; accounts then pushes each changed value to the agent via `POST /v1/credentials/update` (body `{provider, value}`). The store auto-persists on write. The old direct-write `POST /v1/credentials` endpoint is gone — it returns 410 Gone with a pointer to accounts, so any stale caller fails loudly instead of writing values the next push would clobber.
- **Local-dev seed**: For local development, env vars (`DATAFYE_AGENT_*`) seed the store the first time it is created. In production the store starts empty and accounts fills it in.

The agent's system prompt is rebuilt on every chat request, so credential changes are immediately reflected in what the agent tells the user it can do.

**Credentials have to escape the agent's memory to be useful.** A subtle but important detail: storing a credential in the encrypted store isn't enough. The Datafye CLI provisions environments from YAML deployment descriptors that use shell-style substitution — `polygon_api_key: ${POLYGON_API_KEY}`. The CLI is a *subprocess* the agent spawns, and it reads those variables from the process environment. If the values only live in the agent's in-memory store, the CLI sees blank substitutions and provisioning silently produces a dataset with no API key.

So `_apply_credentials_env()` walks the store and exports every data-provider, broker, and GitHub credential into `os.environ` — and it does this on bootstrap *and* after every `/v1/credentials/update` push, so a key the user adds mid-session takes effect on the next provision without a restart. Two wrinkles worth knowing:

- **Historical renames are exported under both names.** Polygon became Massive; Palpha became Precision Alpha. A descriptor in the wild might reference either, so the store key `massive_api_key` is exported as *both* `POLYGON_API_KEY` and `MASSIVE_API_KEY`, and `palpha_api_key` as both `PALPHA_API_KEY` and `PRECISION_ALPHA_API_KEY`. The map lives in `_CREDENTIAL_ENV_MAP`.
- **Unset means unset.** When a credential is absent from the store, `_apply_credentials_env()` *pops* its env vars rather than leaving stale values behind — so revoking a key in Settings actually de-provisions it from the next CLI run.

This generalises the trick the Anthropic key already used (`_apply_anthropic_key()`); the Anthropic key stays on its own path because it additionally *validates* against the Anthropic API, which the others don't.

**ConnectTrade credentials are a special case.** Most credentials are symmetric — one key, one user, done. ConnectTrade has two layers: a *client* identity (who Datafye is, as a ConnectTrade tenant) and a *user* identity (who this particular sandbox's human owner is inside that tenant). They have very different lifetimes:

- **Client creds** (`client_id` / `client_secret`): today these come from env vars (`DATAFYE_AGENT_CONNECTTRADE_CLIENT_ID/_SECRET`). They're the same for every sandbox. TODO: fetch them from the Datafye accounts-manager so we don't have to bake them into AMIs or distribute them through env files.
- **User creds** (`user_id` / `user_secret`): lazy-provisioned on the first `POST /v1/broker/connections` call by hitting ConnectTrade's `POST /users`, then persisted to `~/.datafye/agent/broker_user.json` (mode `0600`). A single file is fine here because every sandbox is single-user by design — there's nobody else to collide with. TODO: migrate this into accounts-manager per-user storage so a user who blows away their sandbox and gets a new one doesn't end up with an orphaned ConnectTrade user.

The broker module binds the same shared `credentials` dict that the rest of the service uses, so env-provided creds, accounts pushes via `/v1/credentials/update`, and lazy-provisioned user creds all converge in one place.

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
- **Hosted**: Pre-baked AMI in a Rumi private cloud. Each user gets a sandbox instance at `{username}.app.datafye.io`, proxied through a jump server with wildcard SSL. Managed by the datafye-accounts service (elastic stop/start based on activity). The AMI carries no user-specific data — identity, credentials, and the Anthropic key are all delivered at runtime by accounts over HTTP (`POST /bootstrap` and `POST /v1/credentials/update`).
- **Standalone (Marketplace)**: Minimal AMI with a first-boot script. User provides DNS via EC2 user data; everything downloads and installs on first boot.

The installer no longer takes an `--anthropic-key` flag, and `first-boot.sh` no longer reads an Anthropic key out of EC2 user data. The Anthropic key now arrives the same way every other credential does — over the accounts credentials channel — for *both* hosted and standalone. That collapses what used to be two key-delivery paths into one and lets the installer do something simpler: it just **always starts the agent**, which boots into the awaiting-bootstrap holding state and waits for accounts to push it identity and credentials. There's no longer any "do we have a key to start with?" branch in the install flow. (`pyyaml` was added to `requirements.txt` to parse the deployment descriptor for `env_status`.)

The agent source is open source — the value is in the Datafye platform, not the glue code. Power users can fork and customize the prompt, add tools, tweak behavior.

## Receive-Only Integration with `datafye-accounts`

The agent is a **receive-only worker** in the Datafye sandbox plane. The shape of its role: it **receives** credentials and JWTs, it **does** work, it never **asks** accounts for anything. Accounts is the only writer in the relationship. This section captures how that integration is wired.

### Identity bootstrap — by push, not by metadata

An earlier design had the agent read its own EC2 `Name` tag from the Instance Metadata Service at startup and derive its credentials-store key from the EC2 instance ID. That was scrapped. The agent no longer touches AWS metadata at all — there is no `identity.py`.

Instead, identity arrives by **push** (see [Bootstrap: How the Agent Learns Who It Is](#bootstrap-how-the-agent-learns-who-it-is) above). Accounts calls `POST /bootstrap` with a signed JWT carrying `user_id` and `creds_key`. The agent has zero identity until that call lands.

The push model is strictly better than reading IMDS:

- **The credentials-store key never lives on the instance.** With the old instance-ID-derived key, anyone with the instance ID could reconstruct the key. The new key is `HMAC-SHA256(K_master, username)` — `K_master` is an accounts-side secret, so the key is *only* derivable inside accounts. The agent gets the finished key over an authenticated channel and holds it in memory.
- **No "refuse to start" failure mode.** The old agent crashed if the `Name` tag was missing. The new agent always starts; if no bootstrap has arrived it simply sits in the holding state answering `/health` — which is exactly what accounts needs in order to know it should push.
- **One trust anchor.** Identity, the credentials-store key, and every later credential all flow through the same accounts-signed channel. There's no second mechanism (IMDS) to reason about or secure.

### Encrypted on-disk credentials store

The agent doesn't have a Rumi store (it's Python/FastAPI, not a Rumi application). Credentials live in a single binary file:

- Path: `~/.datafye/agent/credentials.bin` (mode 0600)
- Format: msgpack
- Encryption: `cryptography.fernet`. The Fernet key is the `creds_key` delivered in the bootstrap push — `base64url(HMAC-SHA256(K_master, username))`. It is held in memory only and never persisted to disk.
- Threat model: defends against casual filesystem inspection and against leaked EBS snapshots (the snapshot is an opaque encrypted blob; the key isn't on it and can't be derived from it). Does not defend against an attacker with shell on the running instance — at that point they can read process memory anyway. That's acceptable; encryption-at-rest is one layer of many.
- Replaces the old `~/.datafye/agent/broker_user.json` plain-JSON file. On first load the store migrates any existing ConnectTrade user creds out of that file and deletes it.
- A `generation` value (a short deterministic hash of the contents) is computed on load and on every write, and exposed in `/health`.

### Endpoint shape

- **`POST /bootstrap`** — accounts-only. Establishes identity + credentials-store key from a signed JWT. Idempotent for the same user, 409 on rebind.
- **`POST /v1/credentials/update`** — accounts-only push, body `{provider, value}`. Updates the in-memory store and persists to `credentials.bin` atomically. Returns 204. A push to `anthropic_api_key` also re-syncs and re-validates the Anthropic key.
- **`GET /health`** — reports `bootstrapped`, `anthropic_key_status`, `credentials_generation`, `last_chat_activity_at`, `running_jobs`, `active_proxied_apps`. `username` and `credentials_generation` are `null` until bootstrapped. Accounts polls this for idle detection and cache-loss recovery (if `credentials_generation` drifts from what accounts last pushed, accounts re-pushes everything — see the [datafye-accounts PROJECT.md](../datafye-accounts/PROJECT.md) idle-monitor section).
- **`POST /v1/credentials`** — removed; returns 410 Gone. The frontend no longer writes credentials directly to the agent; all writes go through accounts.
- **`/v1/conversations*`** — the agent's chat-layer conversation store (history replay via `GET /v1/conversations/{id}/history`). The id-minting `POST`/list `GET` are legacy/unused now that accounts is the authoritative project registry; the agent materialises a local record for an accounts-minted id via `conversations.ensure()` on the first chat turn.
- **JWT validation** on `/v1/chat`, `/v1/credentials/status`, and `/v1/broker/*`: verify the accounts-signed JWT against accounts' JWKS and check `sub == the agent's bootstrapped username`. Reject otherwise.

**Clock skew bit us at bootstrap.** The very first JWT an agent ever sees is the bootstrap token, minted by accounts moments before. If the accounts host's clock runs even a few seconds ahead of the agent's, that token's `iat` (issued-at) lands in the agent's *future*, and PyJWT rejects it with "token is not yet valid (iat)" — a 401 that makes a perfectly correct bootstrap fail intermittently and undebuggably. The fix is to pass `leeway=_CLOCK_SKEW_LEEWAY_SECONDS` (default 60s, env `DATAFYE_AGENT_JWT_LEEWAY_SECONDS`) to *both* `jwt.decode` calls in `auth.py` (`verify_bootstrap_token` and `require_self_jwt`), so small clock differences on any time-based claim (iat/nbf/exp) are tolerated. Lesson: any time you verify a freshly-minted token on a *different* host from the one that signed it, budget for clock skew — distributed clocks are never exactly equal.

### Why this shape

- **No agent → accounts calls** means no shared secret to bootstrap a request, no IAM-signing layer, no fallback mode when JWTs aren't available. Background tasks use the cached credentials, kept fresh by accounts' push plus the generation-counter recovery mechanism.
- **The agent never sees its own credentials before they're pushed.** Even on first boot the store is empty until accounts (driven by the user's Settings activity) pushes values in. That's by design — accounts is the source of truth.
- **No "stale cache + no user connected" race.** Credentials only change when the user takes action; the user is by definition online during that action; the push lands on the live agent before the user disconnects.

### Smaller follow-ups already on the list

- SDK-based algos (Java / Datafye SDK path alongside Python)
- Live trading (currently capped at simulated/paper)
