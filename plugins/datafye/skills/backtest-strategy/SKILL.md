---
name: backtest-strategy
description: Use when the user wants to test, evaluate, or backtest a trading strategy against historical data, or asks "how would this have performed", "run it on last year", "test this on AAPL", "show me the scorecard", "what were the returns". Fetches historical data, runs the algo over it, and presents a performance scorecard.
---

# Test a strategy against historical data

Run a user's strategy over historical market data and present a clear performance report (the "scorecard"). Speak in plain language — say "test against historical data", not "backtest", unless the user is clearly technical.

## Prerequisites

- The strategy's Python code exists in the workspace.
- A foundry environment is provisioned with the datasets/schemas/symbols the strategy needs. If not, provision one first (see the `provision-environment` skill).

## Steps

1. **Confirm the test window and universe.** Ask for (or infer) the date range and the symbols to test. Default to a recent, representative window if the user has no preference, and state what you chose.

2. **Fetch the historical data.** Use the `datafye-api` MCP tools (`mcp__datafye-api__*`) to pull the historical bars/ticks the strategy consumes. Validate the data shape matches what the algo expects before running.

3. **Run the strategy over the data.** Execute the algo's Python against the fetched history (via Bash). Capture every simulated trade: entry/exit time, side, size, and price.

4. **Compute the scorecard.** From the trade log, calculate at least:
   - Total return and return over the period
   - Win rate (share of profitable trades)
   - Number of trades
   - Largest win / largest loss, and max drawdown if feasible
   Keep the metric set honest — if something can't be computed reliably from the available data, say so rather than inventing it.

5. **Present results clearly.** Show the scorecard as a table and call out the headline numbers in a sentence. If the frontend renders charts, structure the trade/equity data so it can be displayed.

6. **Invite iteration.** Offer concrete next steps in the user's terms ("try a shorter lookback", "test on a different period", "add a stop-loss") and re-run on request.

## Notes

- Use the `datafye-api` MCP server for all data access — not `curl` or the CLI.
- Never overstate performance. Report what the data supports, flag small sample sizes, and remind the user that historical results do not guarantee future performance when relevant.
