# Agent coordination -- editor lease broker

## Problem

Multiple agents work at once and share ONE editor per project. That editor is a single
stateful resource: one level loaded, one PIE session, and a rebuild takes it down entirely.
Today agents either step on each other (one rebuilds while another is mid-`uap`) or hard-fail
and stop, forcing the human to manually tell them "editor is free now."

Cross-project contention is a SECOND problem on the same machine, solved separately below
("Two projects, one workstation"): separate editors and a per-project RC port stop the two
projects from *addressing* each other, and do nothing about the keyboard, foreground window and
GPU they share.

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
- **release(agent):** drop this agent's record. **Refuses while PIE is still in progress** (see
  below) unless `--force`.
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
- Those same verbs (minus `status`) then call `wait_if_blocked`: if ANOTHER agent holds the
  exclusive lease -- for any reason, not just `rebuild` -- the op waits for it, and on timeout
  returns `{"ok":false,"busy":true,"blocked_by":...}` with exit 1 instead of running. `status` is
  exempt on purpose: it is the health probe you reach for while diagnosing a stuck editor.

  > This is what makes the lease a lock rather than a sticky note. Until 2026-08, `wait_if_blocked`
  > existed but had **no callers**, so only `reason=rebuild*` blocked anything -- `--reason pie`
  > recorded a holder that nothing consulted, and other agents swapped levels and started PIE
  > straight through a held lease.

**Explicit (opt-in, needs a consistent `--agent` token):**
- `uap lease acquire exclusive --reason pie|level:<path> --agent <tok> --wait 900` ... `uap lease
  release --agent <tok>` -- for holding PIE / a specific level across several calls so another agent
  serializes behind you. Stop PIE (and see it confirmed) BEFORE releasing -- release refuses while a
  session is live. `uap lease status` shows contention; `uap lease heartbeat --agent <tok>`
  extends a long hold.
- **Carry that same token on your editor ops** (`--agent <tok>`, or export `UAP_AGENT_ID=<tok>`),
  or your own exclusive lease will block you. There is no reliable auto-identity in this harness,
  so an unidentified call is indistinguishable from a stranger's.

### Release must hand over a CLEAN editor

`uap lease release` is the moment the system says "the editor is free"; the next agent acquires on
that word alone and never re-checks. So release now asks the editor whether PIE is still in
progress (`IsPIEInProgress`, degrading to `IsInPIE` on an older plugin copy) and **refuses** while
it is, telling the caller to `uap pie stop` first. `--force` releases anyway, for a deliberate
handover of a live session. It is fail-open on an unreachable editor -- a dead editor has no PIE
session, and a coordination check must never wedge the lease.

Why this one is worth a hard refusal rather than a warning: a `pie stop` that lies is recoverable
if the caller checks, but a release granted on that lie is not. By the time anyone notices, a
DIFFERENT agent already holds the lease and is driving an editor still in PIE, and nothing in the
system will ever tell either of them. That was live until 2026-08-28, because `pie stop` answered
`ok:true` for a stop that had not happened (known-issues #25) and release took its word for it.
`pie stop` now confirms the teardown and fails loudly if it does not happen; the release guard is
the second line, for every other way a session can still be live.

TTL is "time since the holder's last `uap` call": every editor op by the holder heartbeats its own
lease, so an actively-working agent never needs manual heartbeating, while an abandoned hold ages
out (pie/level 10 min, rebuild 20 min, reads 2 min) and PID-death evicts sooner where anchored.
`uap lease status` names the holder; `uap lease release --agent <tok>` breaks an abandoned one.

## Two projects, one workstation (the machine-wide foreground lock)

Everything above is scoped to ONE project's editor. That scoping is correct for what a project's
editor owns alone -- its level, its RC port, its PIE world -- and it is exactly why it could not
see the real second problem: a developer runs **both** projects at once from the **same uap
install** (Project Broken Wings pins `UAP_PROJECT=ProjectBrokenWings`, School's Out VR pins
`UAP_PROJECT=SchoolsOut`, both launchers resolving to the same repo and venv). Each project's
lease lives in its own file, so a PBW agent asking "is the editor free?" got "yes" while a SOVR
agent was mid-PIE, and vice versa. Both leases were right; neither was answering the question
that mattered, because a workstation has **one keyboard, one foreground window and one GPU**, and
PIE in either editor takes all three from the other.

So there is a second lock, machine-scoped, on top of the project lease:
`~/.uap-reports/.leases/_machine.json`, same mechanics (block-then-`busy`, heartbeat, TTL + PID
eviction, O_EXCL lockfile) and one holder, whose record carries its `project`:

```json
{ "exclusive": { "agent": "id", "project": "schoolsout", "reason": "pie:start",
                 "pid": 0, "acquired_at": .., "heartbeat_at": .., "ttl": 600 } }
```

**What takes it:** `pie`, `input`, `screenshot`, `click`, `tab`, `nav`, `read-ui` -- the verbs
that start/hold PIE, inject real input, or need the foreground window (a screenshot of a
background editor is a stale 3fps frame, and `read-ui` needs focus to answer at all). Blocked
callers WAIT, exactly as they do for an exclusive project lease, and on the cap return
`{"ok": false, "busy": true, "blocked_by": "<agent>", "project": "SchoolsOut", "scope": "machine"}`
with exit 1.

**What does not:** `status`, `log`, `sample`, `helpers` -- read-only, and they are the verbs you
reach for *while* diagnosing a locked machine. Nor `rc` / `exec` / `exec-file`: those can do
anything, but they are the generic transport for ordinary project-scoped work, and gating every
one of them on the other project's PIE session would serialize the two projects almost completely
-- a far bigger behaviour change than the clash being fixed. They stay governed by the project
lease.

**Same-project calls pass straight through.** The lock arbitrates BETWEEN projects only; within a
project, turn-taking already has an owner (the exclusive lease) and a second gate there would
deadlock agents it serializes correctly. This is also what keeps the common case free: on a
one-project machine nothing else ever asks, so the wait is always zero.

**Hold lifetimes.** `pie start` takes a STICKY hold (`pid: 0`, TTL 600) because what it is holding
is the SESSION, which outlives the command; `pie stop` gives it back, but only on a *confirmed*
stop -- freeing the machine on a stop that did not happen would hand the other project a live PIE,
the same failure `lease release` refuses. Every other guarded verb takes a transient hold released
in a `finally`, so a crash or an exception cannot leave the machine locked. Any pass-through call
from the holding project refreshes the heartbeat, so an agent that is actually working keeps it.

**Explicit holds:** `uap lease acquire exclusive --reason pie|level|input ... --agent <tok>` takes
both turns; if the machine is denied it gives the project lease back rather than half-holding.
`--reason rebuild` does NOT take the machine: a rebuild is CPU-heavy but takes neither the keyboard
nor a game window, and blocking the other project for a 20-minute rebuild TTL is the worse trade.
`uap lease release` hands back both.

**Inspecting and breaking it:** `uap lease status` now reports the machine holder beside the
project lease (a project lease that reads "free" explains nothing when the blocker is the other
project). `uap lease machine-status` shows it alone; `uap lease machine-release [--force]` breaks
a hold left by a session that died and has not aged out. `UAP_MACHINE_LOCK=0` disables the lock
entirely -- an escape hatch so a coordination bug can never be the thing standing between an agent
and the editor, not a setting to reach for.

## Non-goals

- True parallel stateful work on one editor (physically impossible: one level, one PIE).
- Spawning a second editor per agent (RAM; the machine already strains with two).
- True parallel PIE in two projects at once. Also physically impossible on one workstation: one
  keyboard, one foreground window. The machine lock makes them take turns, not overlap.

## Rollout

Pure Python in the `uap` CLI + a `coordination.py` module -> live for all agents via the shared
venv, no editor rebuild, no P4 for the code. The lease is baked into each project's
`Restart-Editor.ps1` (local tooling) and the rule is stated in each project's agent-instructions
file (`AGENTS.md` for SchoolsOut, `CLAUDE.md` for PBW -- both P4-tracked, read by every tool). Rule:
rebuild ONLY via `Restart-Editor.ps1` (it self-coordinates); never bounce the editor by hand; `uap`
editor ops auto-wait through a rebuild; hold PIE/level across calls with `uap lease ... --agent`.

The machine-wide lock ships the same way, and needs nothing from either project: both projects'
`uap.ps1` launchers already resolve to the same repo and venv, so it is live for both the moment
this lands. Nothing in a launcher changes -- `UAP_PROJECT` is what the lock reads to tell the two
apart, and both already pin it.
