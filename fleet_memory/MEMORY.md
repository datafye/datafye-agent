# Fleet Memory

Lessons distilled from across the whole Datafye fleet and shipped with the agent
build. Read-only: this bank is curated out of band, not written by an agent.
One line per memory file.

- [Diagnosing the environment](diagnosing-the-environment.md) — what actually
  proves an environment works, and the evidence that looks conclusive but is not:
  empty `docker logs`, containers "Up" with no applications inside, and where the
  real error is already written down.
- [Sizing an environment, and what memory a flow needs](sizing-and-resource-cost.md) —
  read BEFORE any fetch, replay or backtest: the two memory ceilings (host RAM vs a
  service's own heap, which a bigger box does not raise), the default heaps and how to
  set them in the descriptor, what drives the aggregation service's memory, and the
  combined-ticks fetch that exhausts the history heap and writes nothing.
- [Platform gotchas and workarounds](platform-gotchas.md) — read before working
  with a dataset or planning a fetch: crypto symbol form and its missing quotes,
  one dataset at a time, and the tick fetch that OOMs and writes nothing.
