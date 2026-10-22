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

### Who is it, and what for? (`--task` / `$UAP_TASK`)

An agent token (`pcgprof`, `rebuild-48876`) tells a human watching the editor nothing about the
work. **Set a task label every time you drive the editor**:

```powershell
$env:UAP_AGENT_ID = "pcgprof"                 # who (same token on every call, see above)
$env:UAP_TASK     = "PCG profiling, Utah"     # what for -- a few words a director can read
```

or per call: `uap ... --agent pcgprof --task "PCG profiling, Utah"`. The flag wins over the env
var. The label is sanitised (one line, control characters folded to spaces) and cut at 80
characters. It is **display only**: it never affects identity, queue order or who is granted
what.

It is recorded, in the same state files the in-editor overlay already reads:

| Where | Field | Written by |
|-------|-------|------------|
| lease holder (`exclusive` / `shared[]`) | `task`, `requested_by` | `lease acquire`; refreshed by any editor verb or `lease heartbeat` that carries a task |
| each waiter (`waiters[]`) and `queue_detail` | `task`, `requested_by`, `reason` | the blocked `lease acquire` |
| machine-wide foreground lock (`_machine.json`) holder / waiters | `task` | the verb that took the machine turn |
| `activity.<agent>` | `task`, `verb`, `at` | every editor-touching verb, so an agent that holds NO lease (plain `exec` / `rc` traffic) still shows up for ~5 minutes |

`uap lease status` therefore prints `task` for the holder, every waiter and the machine-lock
holder (`""` when none was set). A refusal (`busy`) names the blocker's task too, as
`blocked_task`.

**Rebuild requests name their owner.** `Restart-Editor.ps1` takes its lease as the mechanical
token `rebuild-<pid>`. `lease acquire` now also records `requested_by`: `--requested-by <agent>`
if given, otherwise `$UAP_AGENT_ID` when it differs from the `--agent` token. Because the
script's child `uap` processes inherit the environment, an agent that runs
`$env:UAP_AGENT_ID="pcgprof"; $env:UAP_TASK="..."; .\Restart-Editor.ps1` shows up in the queue as
`rebuild-48876 by pcgprof` with its task, with **no change to the project's script**. The
rebuild -> relaunch re-acquire keeps both labels. Projects that want the script to be explicit
(or to work without the env) can pass them through; see "Projects: Restart-Editor.ps1" below.

### The overlay

The uap overlay (`AgentOverlay.cpp`, top-right of the editor/PIE viewport, and in every
`uap screenshot`) draws, while an agent is driving:

```
UAP  ProjectBrokenWings
RUN  exec print(1)  0.4s
agent  pcgprof
task   PCG profiling, Utah
lease  pie  4m12s
next:  rebuild-48876 by pcgprof: PCG prof~
       hangar: Hangar lights +2
fps  60.0
```

`agent` / `task` name the current driver (the machine-lock or lease holder, else whoever called
last within 60 s); a driver with no label gets a grey `task (none set: --task / UAP_TASK)` nudge.
`lease` shows the reason and how long it has been held (`EXPIRED` in red once past its TTL; when
the holder is not the driver named above its token and its own `for <task>` line are shown).
`next:` names the first two waiters with their tasks, plus `+N` for the rest, one per line and
trimmed so a small window stays readable. Existing lines (project, verb, held keys, fps, report)
are unchanged.

> The label travels in the lease files, which the shared CLI writes, so the CLI half is live for
> every project the moment its venv is updated. Drawing it needs the **plugin** half: the overlay
> is C++ (`Plugin/Source/UnrealAgentPlayerRuntime/Private/AgentOverlay.cpp`), vendored into each
> project's `Plugins/UnrealAgentPlayer`, so a project sees the new lines only after that file is
> updated there and the editor is rebuilt (`Restart-Editor.ps1`). Until then the old overlay keeps
> working and just shows tokens.

### Projects: Restart-Editor.ps1

Optional, because the environment already flows through. To make the owner explicit, give the
script `-Agent` / `-Task` parameters defaulting to the env and pass them on both `lease acquire`
calls:

```powershell
param(
    # ...existing params...
    [string]$Agent = $env:UAP_AGENT_ID,
    [string]$Task  = $env:UAP_TASK
)
# ...
$ownerArgs = @()
if ($Agent) { $ownerArgs += @('--requested-by', $Agent) }
if ($Task)  { $ownerArgs += @('--task', $Task) }
$leaseOut = & powershell -NoProfile -ExecutionPolicy Bypass -File $uapLauncher lease acquire exclusive `
    --reason rebuild --agent "rebuild-$PID" --pid $PID --wait 900 @ownerArgs
# ...and the same `@ownerArgs` on the `--reason relaunch` acquire.
```

Eviction therefore has three independent reclaimers: **stale heartbeat (TTL)** for any hold,
**dead PID** for PID-anchored holds, and, for `pie`/`level` holds only, **dead editor** (below).
Any one frees an abandoned lease.

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

## A dead editor voids its pie/level lease

A `pie` or `level` exclusive lease protects a LIVE editor session -- the loaded level, the running
PIE world. If the editor process is gone, that session cannot exist and the lease protects nothing.
Yet it used to keep blocking everyone for its full TTL, and the holder's own `uap` calls (which wait
for the editor to come back) refreshed the heartbeat so it never aged out on its own. On
2026-10-22 the PBW editor died (out of memory) while agent `respawnfix` held `pie`; the queued
`Restart-Editor.ps1` (a `rebuild-...` waiter), the one thing that could bring the editor back, could
not relaunch it until the TTL ran out.

**The rule:** a `pie` / `level` exclusive lease is void when no EDITOR process for that project is
running AND the project is not mid-rebuild. On every load / acquire / status / wait the holder is
evicted on the spot, by the same pass that does the TTL and PID-death eviction
(`coordination._evict_dead_editor`). Nothing else changes: `rebuild` / `relaunch` leases (the
editor being down is their job), shared reads, and any other reason are never touched, and a `pie`
lease on a running editor is never evicted by this.

**What counts as "the editor is running"** (`coordination.editor_running`): an `UnrealEditor.exe`
whose command line names `<project>.uproject`, matched case-insensitively on the .uproject name,
which is also the lease's project key. So School's Out's editor never counts as PBW's, and vice
versa. A standalone `-game` client of the same project does NOT count: it has no level or PIE
session for a lease to protect. Detection is dependency-free (a Toolhelp process snapshot plus the
command-line read `window_capture` already uses -- no PowerShell, no WMI) and cached for a few
seconds (5s when up, 1s when down), so status polling stays fast.

**Fail safe.** Only a probe that positively answers "no editor for this project" evicts. These all
answer "unknown" and never evict (the TTL still reclaims a genuinely abandoned hold): the process
list could not be read, an editor's command line was unreadable (elevated / other user -- it might
be this project's), a non-Windows host, an unknown project (`--project` empty), or
`UAP_EDITOR_LIVENESS=0` (escape hatch, not a setting). A rebuild or relaunch hold anywhere on the
project also vetoes the eviction.

**Rebuild goes first while the editor is down.** FIFO is right while the editor is up. With it
verifiably down, a queued `rebuild*` request moves ahead of queued pie/level (and other) waiters,
so the editor comes back before anyone tries to use it. Stable within each group; with the editor
up or unknown the queue is untouched.

**The evicted holder is told.** The eviction is recorded in the lease file:
- the holder's NEXT guarded `uap` call fails once with
  `{"ok": false, "lease_evicted": true, "why": "editor_not_running", "error": "lease_evicted:
  editor_not_running -- ..."}` (exit 1) instead of running against a session that died with the
  editor; the call after that proceeds normally. `lease heartbeat` / `lease release` by the holder
  carry the same `lease_evicted` note. Re-acquiring clears it.
- `uap lease status` shows `last_eviction` (agent, held reason, `why`, `evicted_at`, message).

It also frees the holder's machine-wide foreground lock (see below), which `lease acquire ...
--reason pie` normally takes alongside the project lease: otherwise the OTHER project would sit
out that hold's TTL for a session that no longer exists.

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
A hold past its TTL is evicted only if its project's editor is NOT in PIE: the CLI probes that
editor (`IsPIEInProgress`) first, and a live session refreshes the hold instead, so a long PIE
driven through `exec` / `rc` alone no longer ages out under the other project. `pie stop` never
waits on the machine lock: stopping gives the foreground back.

**Fail closed.** A guarded verb that cannot read or write the machine lock does NOT run. Transient
Windows sharing violations on the lock files (`O_EXCL` answering EACCES while a release is
mid-delete; `os.replace` answering WinError 5 while an editor's overlay reads the file) are retried
in place. This used to be fail-open, and on 2026-10-22 one such error during a wait started School's
Out's PIE with no machine record while Project Broken Wings was in PIE, after which PBW's next
`pie start` was granted too. `UAP_MACHINE_LOCK=0` remains the deliberate escape hatch.

**Never wait on the machine while holding the project lease.** A guarded verb from the agent that
holds its project's exclusive lease asks for the machine once. If another project has it, the verb
fails fast and the project lease is given back (so teammates are not queued behind a turn the agent
cannot use), unless that agent's own editor is in PIE: then the lease is kept, because handing a
live session to a teammate is what `lease release` refuses, and the hint says to `pie stop` and
re-acquire. Agents without a lease still wait their turn as before.

**Explicit holds:** `uap lease acquire exclusive --reason pie|level|input ... --agent <tok>` takes
both turns; if the machine is denied it gives the project lease back rather than half-holding.
`--reason rebuild` does NOT take the machine: a rebuild is CPU-heavy but takes neither the keyboard
nor a game window, and blocking the other project for a 20-minute rebuild TTL is the worse trade.
`uap lease release` hands back both -- the project's machine hold only for the agent that held the
project's exclusive lease; any other caller frees only a machine hold it took itself. Releases are
always scoped to the caller's project, so the same agent token in both projects cannot free the
other project's session.

**Inspecting and breaking it:** `uap lease status` now reports the machine holder beside the
project lease (a project lease that reads "free" explains nothing when the blocker is the other
project). `uap lease machine-status` shows it alone; `uap lease machine-release [--force]` breaks
a hold left by a session that died and has not aged out. `UAP_MACHINE_LOCK=0` disables the lock
entirely -- an escape hatch so a coordination bug can never be the thing standing between an agent
and the editor, not a setting to reach for.

## Waiting is FIFO

`acquire ... --wait <n>` used not to queue. Every blocked agent polled independently, so whoever
happened to look in the instant after a release won, regardless of how long anyone had waited. On
2026-09-24 one agent waited out two full 900s caps and lost both times to agents that asked later,
then got it on a third try; three of the six agents sharing that editor gave up on runtime
verification altogether. Reproduced with four processes arriving 1.5s apart: the old code granted
in exact REVERSE arrival order, so the longest waiter went last every time.

A blocked acquire now registers itself in a `waiters` list in arrival order, and only the head of
the queue may take the lease. The queue lives in the same state file under the same O_EXCL
filelock as the grant decision, so no latecomer can slip past the head. The machine-wide
foreground lock queues the same way, for the same reason.

- A waiter re-registers on every poll. One that dies, is killed or times out ages out on a short
  waiter TTL (15s -- a few poll intervals, not a lease TTL) and on PID-death, so it cannot wedge
  the queue; a caller that times out also dequeues itself on the way out.
- An agent that already holds the lease never queues behind its own waiters.
- A run of SHARED waiters at the head goes together, since they do not exclude each other. An
  exclusive waiter anywhere ahead still blocks shared traffic, so a queued rebuild cannot be
  starved by a stream of readers.
- Holder TTL age-out and `uap lease release --agent <token>` are unchanged.
- One exception to FIFO: with the project's editor verifiably down, `rebuild*` waiters go ahead of
  the others (see "A dead editor voids its pie/level lease").

The `busy` response carries `queue_position`, `queue_ahead` and `queue_length`, and `uap lease
status` / `lease machine-status` name the queue in order -- so an agent that gives up can report
whether it was next or ninth instead of retrying blind.

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
