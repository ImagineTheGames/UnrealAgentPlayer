# Agent coordination -- editor lease broker

## Problem

Multiple agents work at once and share ONE editor per project. That editor is a single
stateful resource: one level loaded, one PIE session, and a rebuild takes it down entirely.
Today agents either step on each other (one rebuilds while another is mid-`uap`) or hard-fail
and stop, forcing the human to manually tell them "editor is free now."

Cross-project contention is already solved (separate editors + per-project RC port pin). This
is strictly about **multiple agents on the same editor**.

Two agents fundamentally cannot both have different levels / PIE live on one editor, so the goal
is not true parallelism -- it is **clean turn-taking with auto-wait/resume and no hard-fails**.

## Decided model

- **Busy behavior:** block & auto-resume, up to a cap. A `uap` op that needs a busy editor polls
  until it frees, then proceeds -- no fail, no human nudge. Only if the cap (default 15 min,
  covers a rebuild) is exceeded does it return a structured `busy {holder, reason, since}` so the
  agent can report back.
- **Concurrency:** shared reads + one exclusive writer. Many agents may read concurrently
  (status / read-ui / screenshot / read-only exec); rebuild / PIE / level-load takes an exclusive
  turn that blocks reads until done.
- **Different levels:** take turns on the one editor (no second-editor spawning -- a VR editor is
  GBs of RAM). Agents serialize via the lease.

## Mechanism

Per-project lease file `~/.uap-reports/.leases/<project>.json`, read-modify-written under an
OS-exclusive lockfile (`O_CREAT|O_EXCL` spin) so concurrent agents update it atomically.

Record shape:

```json
{
  "generation": 7,
  "exclusive": { "agent": "id", "reason": "rebuild", "pid": 1234,
                 "acquired_at": 1719, "heartbeat_at": 1720, "ttl": 1200 },
  "shared": [ { "agent": "id", "reason": "read-ui", "pid": 88, "acquired_at": .., "heartbeat_at": .., "ttl": 120 } ]
}
```

- **acquire(mode, reason, --wait cap, --ttl):** poll ~2s. Each poll first EVICTS stale holders --
  a holder whose `pid` is no longer alive, OR whose `heartbeat_at` is older than its `ttl`
  (crash-safe: a dead agent never wedges the editor). Grant when: exclusive -> no other holders;
  shared -> no exclusive holder. On cap exceeded, return `busy` with the current holder.
- **release(agent):** drop this agent's record.
- **heartbeat(agent):** refresh `heartbeat_at` for long holds.
- **status:** dump holders + queue for humans/agents.
- **generation:** bumped when the editor bounces (rebuild). Shared holders re-sync (their RC
  connection was killed) by noticing the bump.

### Agent identity (verified: no reliable auto-identity in this harness)

We probed it: `$PPID` is a shared init (`1`) and the actual shell PID changes every tool call, so
there is NO stable per-agent token that survives across an agent's calls. That reality shapes the
design:

- **Single-process holds** (a whole rebuild in one process) anchor the lease to that process's PID.
  It is alive for the entire hold and reclaimed by PID-death the instant it exits/crashes -- fully
  robust, no identity needed. `Restart-Editor.ps1` uses this: it acquires with `--pid $PID` (its own
  PID) and never releases explicitly; exiting reclaims the lease.
- **Cross-call holds** (an agent keeping PIE/a level for a while) CANNOT rely on a per-call default
  (the acquiring process exits immediately, and PID-death would evict it at once). So a standalone
  `uap lease acquire` defaults to `--pid 0` = **TTL-only**, and the agent MUST pass a consistent
  `--agent <token>` on every related call, then `release` (or let the TTL reclaim it). `$UAP_AGENT_ID`
  overrides the default token.

Eviction therefore has two independent reclaimers: **stale heartbeat (TTL)** for any hold, and
**dead PID** for PID-anchored holds. Either one frees an abandoned lease.

## What's enforced automatically vs explicit (v1)

Enforce at the choke point, don't trust agents to remember -- but only where it's ROBUST without a
stable per-agent id (see above). So v1 splits into automatic and opt-in:

**Automatic (no identity needed, fully robust):**
- `Restart-Editor.ps1` takes `exclusive(rebuild)` anchored to its own PID before it touches the
  editor, and reclaims on exit. Calling the script directly coordinates exactly like a `uap` verb
  would -- there is no separate `uap rebuild` to remember, and no way to bypass by running the script
  "the normal way."
- Every editor-touching `uap` verb (`status`/`rc`/`exec`/`exec-file`/`pie`/`read-ui`/`click`/`tab`/
  `nav`/`screenshot`) calls `wait_while_rebuild` first: if a rebuild is in progress it BLOCKS until
  the editor is back, then proceeds. Identity-free (keys on the lease's `reason`, not who holds it).
  Fail-open, so a coordination bug can never brick `uap`.

**Explicit (opt-in, needs a consistent `--agent` token):**
- `uap lease acquire exclusive --reason pie|level:<path> --agent <tok> --wait 900` ... `uap lease
  release --agent <tok>` -- for holding PIE / a specific level across several calls so another agent
  serializes behind you. `uap lease status` shows contention; `uap lease heartbeat --agent <tok>`
  extends a long hold.

Generous TTLs (rebuild/pie/level 20 min, reads 2 min) + PID-death eviction mean the common case
needs no manual heartbeating. A future version can auto-lease PIE if the harness ever exposes a
stable agent id.

## Non-goals

- True parallel stateful work on one editor (physically impossible: one level, one PIE).
- Spawning a second editor per agent (RAM; the machine already strains with two).
- Cross-project coordination (already handled by separate editors + RC port pins).

## Rollout

Pure Python in the `uap` CLI + a `coordination.py` module -> live for all agents via the shared
venv, no editor rebuild, no P4 for the code. The lease is baked into each project's
`Restart-Editor.ps1` (local tooling) and the rule is stated in each project's agent-instructions
file (`AGENTS.md` for SchoolsOut, `CLAUDE.md` for PBW -- both P4-tracked, read by every tool). Rule:
rebuild ONLY via `Restart-Editor.ps1` (it self-coordinates); never bounce the editor by hand; `uap`
editor ops auto-wait through a rebuild; hold PIE/level across calls with `uap lease ... --agent`.
