"""What one of the agent's own model calls cost, priced exactly as the metering gateway prices it (DAT-485).

The SDK's calls arrive already priced: Claude Code reports each model's costUSD. The agent's side-calls
(title, satisfaction, the activity classifier, ...) go straight to /v1/messages, and that response
carries no cost, so they used to be reported to accounts at $0. On u3 that hid $0.0120 of Haiku a
turn, exactly the gap between the console's Project Usage and the gateway's per-project figure.

⚠️ PRICES IS A COPY of the `prices` table in nvx-llm-gateway's deploy/n5.yaml and deploy/datafye.yaml
(identical today), and cost_micros is the gateway's own algorithm (llm_gateway/pricing.py). A box off
the gateway has nothing else to ask, so the agent must carry the table; tests/sidecar_cost.py fails if
this copy and a gateway checkout disagree. Change the gateway's table and this one together.

Prices are USD per million tokens, which is also micros per token.
"""

import fnmatch
import logging
from typing import Optional

logger = logging.getLogger(__name__)

PRICES = [
    {"models": ["claude-fable-5-1*", "claude-fable-5*"], "input": 10, "output": 50, "cache_read": 0.25, "cache_write_5m": 12.5},
    {"models": ["claude-opus-5*", "claude-opus-4-5*", "claude-opus-4-6*", "claude-opus-4-7*", "claude-opus-4-8*"], "input": 5, "output": 25, "cache_read": 0.5, "cache_write_5m": 6.25},
    {"models": ["claude-opus-4-1*", "claude-opus-4-2025*"], "input": 15, "output": 75, "cache_read": 1.5, "cache_write_5m": 18.75},
    {"models": ["claude-sonnet-5*"], "input": 2, "output": 10, "cache_read": 0.2, "cache_write_5m": 2.5},
    {"models": ["claude-sonnet-4-*"], "input": 3, "output": 15, "cache_read": 0.3, "cache_write_5m": 3.75},
    {"models": ["claude-haiku-4-5*"], "input": 1, "output": 5, "cache_read": 0.1, "cache_write_5m": 1.25},
]


def _price_for(model: str) -> Optional[dict]:
    """The first row whose pattern matches, in table order, as the gateway picks it."""
    found = None
    for row in PRICES:
        if found is None and any(fnmatch.fnmatchcase(model, p) for p in row["models"]):
            found = row
    return found


def cost_micros(model: Optional[str], usage: Optional[dict]) -> Optional[int]:
    """Cost of one call in micros, or None when it cannot be priced (logged, never guessed as 0).

    `model` must be the model that ANSWERED (the response's `model`), not the alias asked for, so the
    price is the one the gateway applies to the same call. Rounded per call, as the gateway rounds.
    """
    price = _price_for(model) if model and usage else None
    result = None
    if model and usage and price is None:
        logger.warning("no price for model %s; its side-call is reported without a cost", model)
    elif price is not None:
        def n(key):
            return int(usage.get(key) or 0)

        writes = n("cache_creation_input_tokens")
        one_hour = int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
        # Without a TTL breakdown every write is a 5-minute write.
        five_min = max(writes - one_hour, 0)
        cache_write_1h = price.get("cache_write_1h", price["input"] * 2)
        total = (n("input_tokens") * price["input"]
                 + n("output_tokens") * price["output"]
                 + n("cache_read_input_tokens") * price["cache_read"]
                 + five_min * price["cache_write_5m"]
                 + one_hour * cache_write_1h)
        result = int(round(total))
    return result
