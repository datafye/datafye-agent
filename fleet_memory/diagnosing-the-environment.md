# Diagnosing a Datafye environment

Facts about how a running environment actually behaves, learned from real
sandboxes. Several of them are traps: the obvious reading of the evidence is
wrong, and acting on it makes things worse.

## The only proof that an environment works is a data call

Ask the API for something real -- deployed datasets, or candles for a symbol you
know was fetched -- and see the data come back. Everything else described below
can look healthy while the environment is not.

## An empty `docker logs` proves NOTHING

Rumi services write to log FILES inside the container. They do not write to the
container's stdout. So a perfectly healthy service shows an empty `docker logs`,
exactly like one that never started.

This is not hypothetical: an agent concluded "the SIP container logs are
completely empty, which means the apps never launched", acted on it, and wrote
the rule into its own memory. It was false. Do not use `docker logs` emptiness as
evidence of anything, and never as a readiness check.

## Containers being "Up" does NOT mean the platform is up

Rumi local containers are **machines**, not applications. Their command is
`/usr/sbin/sshd -D`, and the CLI SSHes in afterwards to deploy the applications.
Because they run with `--restart unless-stopped`, a box can present a complete,
healthy-looking set of containers with no applications inside them at all -- this
is the state a box wakes in after being stopped while running.

Tells: `/home/rumi/` inside the container has only shell dotfiles (no `run/`
directory, no logs), and the API port is bound by `docker-proxy` with nothing
answering behind it.

## `foundry local status` is the right first question

It reports one verdict plus the evidence behind each line, without changing
anything:

- **IN PROGRESS** -- another operation owns the environment right now. Wait. Do
  not start anything.
- **PARTIAL** -- some services answer and some do not. `start` converges this;
  it is not a reason to rebuild.
- **DEGRADED** / **STOPPED** -- `start` first.
- **NOT PROVISIONED** -- nothing is deployed.

A dead service makes it take ~16s to answer, because it probes each service. That
is slow, not stuck.

## When something fails, the real error is already written down

A failed `provision` / `apply` / `start` / `stop` leaves a report under
`~/.datafye/logs/`:

- `foundry-<op>-<timestamp>.log` -- the full cause chain, a container inventory,
  and a tail of each container's own application log.
- `cli-<cmd>-<timestamp>.log` -- the command's console output, flushed as it went,
  so it survives even a command that was killed.

Read the newest one before forming any theory, and quote the actual error to the
user rather than saying the platform is broken. If a rebuild fails the same way
twice, stop: a second identical failure is a defect, not bad luck.

## `journalctl -u <unit>` lies unless you use sudo

Run without sudo it prints "No entries" even when the unit ran and logged, because
the calling user sees only its own messages. Always `sudo journalctl`. Under sudo
it can still miss older boots -- for a past incident, drop the `-u` filter and use
a time range, after `sudo journalctl --list-boots`.

## Never work around the deployment by hand

The environment is managed by a control plane. `docker restart`, `docker exec` to
launch something, or hand-editing inside a container cannot work and will hide the
real state. `docker ps` and `docker logs` are fine for read-only diagnosis, subject
to the caveat above. Everything that changes the environment goes through the
Datafye CLI.
