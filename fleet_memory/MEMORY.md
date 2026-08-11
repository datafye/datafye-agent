# Fleet Memory

Lessons distilled from across the whole Datafye fleet and shipped with the agent
build. Read-only: this bank is curated out of band, not written by an agent.
One line per memory file.

- [Diagnosing the environment](diagnosing-the-environment.md) — what actually
  proves an environment works, and the evidence that looks conclusive but is not:
  empty `docker logs`, containers "Up" with no applications inside, and where the
  real error is already written down.
- [Platform gotchas and workarounds](platform-gotchas.md) — read before working
  with a dataset or planning a fetch: crypto symbol form and its missing quotes,
  one dataset at a time, and the tick fetch that OOMs and writes nothing.
