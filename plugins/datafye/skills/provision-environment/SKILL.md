---
name: provision-environment
description: Use when the user wants to set up, provision, spin up, reconfigure, or tear down a Datafye data or trading environment. Trigger phrases include "get me AAPL daily bars", "set up a SIP dataset", "I need crypto data", "add MSFT to the environment", "provision a paper-trading env", "spin up an environment for backtesting". Translates a described data need into a deployment descriptor and provisions it via the Datafye CLI.
---

# Provision a Datafye environment

Turn a plain-language data requirement into a running Datafye environment. Do this before writing or testing any algo that needs market data.

## Steps

1. **Determine what the user needs.** From their description, decide:
   - **Datasets**: SIP (US equities), Crypto, Palpha (Precision Alpha), HWAI, or Synthetic.
   - **Schemas** within those datasets: `ohlc`, `ema`, `sma`, `ticks`, etc., and their frequencies (e.g. `ohlc-1d`, `sma-1d`).
   - **Symbols**: the tickers the user named, or sensible defaults if they only described a universe.
   - **Mode**: `backtest` (foundry, no broker) for development and historical testing, or `paper` (trading, broker-connected) for simulated trading.

2. **Check credentials.** Each dataset has a provider key (SIP/Crypto → Massive; Palpha → Precision Alpha; HWAI → HWAI). If the required key is not configured, tell the user to add it in Settings (gear icon). Do not ask them to paste keys in chat. Do not proceed with a dataset whose key is missing.

3. **Build the deployment descriptor.** Write a YAML descriptor for the datasets, schemas, symbols, and mode. Provider keys are referenced with shell-style substitution (e.g. `${MASSIVE_API_KEY}`) — the agent exports configured credentials into the environment, so leave them as `${VAR}` references, never inline literal keys.

4. **Provision.**
   - Development / backtesting (no broker): `datafye foundry local provision -x <descriptor>.yaml`
   - Simulated trading (broker): `datafye trading local provision -x <descriptor>.yaml`
   - Reconfigure a running environment: re-run provision with the updated descriptor, or `datafye foundry local upgrade`.
   - Tear down: `datafye foundry local stop`.

5. **Confirm it's live.** After provisioning completes, interact with the deployment through the `datafye-api` MCP tools (`mcp__datafye-api__*`) — inspect datasets, schemas, and symbols to confirm the environment matches what the user asked for. Do not use `curl` or the CLI for queries the MCP server covers.

6. **Report back** in plain language: what data is now available, for which symbols, and whether it's a development or simulated-trading environment.

## Notes

- Prefer the `datafye-api` MCP server over `curl`/CLI for anything it covers (data queries, deployment inspection).
- Use the CLI only for environment lifecycle (provision, upgrade, stop) and raw data streaming to disk.
- Always check the Datafye docs for exact descriptor schema and CLI flags before guessing.
