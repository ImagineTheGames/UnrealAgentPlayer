"""Cross-agent editor coordination -- a per-project lease plus a machine-wide foreground lock.

Two scopes, because two different resources are contended:

1. **Per-project lease** (`<reports>/.leases/<project>.json`). Multiple agents share ONE editor
   per project (one level, one PIE session; a rebuild takes it down entirely). This lets them
   take turns instead of stepping on each other or hard-failing.

   Model (decided with the user):
     * shared reads + one exclusive writer -- many agents may read concurrently; rebuild / PIE /
       level-load takes an exclusive turn that blocks reads until done.
     * acquire BLOCKS (polls) until grantable, up to a cap, then returns a structured `busy`.
     * waiting is FIFO -- a blocked acquire joins a queue and only the head of it may take the
       lease when it frees (see `_turn_ok`), so waiting is eventually rewarded.
     * crash-safe: each poll evicts holders whose owning process is gone OR whose heartbeat is
       stale (TTL), and waiters that stopped polling, so neither a dead agent nor a dead waiter
       ever wedges the editor.

2. **Machine-wide foreground lock** (`<reports>/.leases/_machine.json`). A project lease is
   scoped to its own project, so it says nothing about the OTHER project's editor -- and one
   workstation has exactly ONE keyboard, ONE foreground window and ONE GPU. PIE in either editor
   steals all three from the other. So the operations that start/hold PIE, inject real input, or
   need the foreground window take a SECOND, machine-scoped turn on top of the project lease.
   Same mechanics (block-then-busy, heartbeat, TTL eviction), one holder, and the holder record
   carries its `project` so a blocked caller is told which project is holding the machine.

   Same-project calls pass straight THROUGH this lock -- intra-project turn-taking is the project
   lease's job. A single-project workstation therefore never waits on it at all.

Each state file is read-modify-written under an O_EXCL lockfile so concurrent agents update it
atomically. Pure stdlib, so it is live for every agent via the shared venv -- no editor rebuild,
no plugin change.
"""
from __future__ import annotations

import errno
import json
import os
import pathlib
import time
import uuid

POLL_SECONDS = 2.0
DEFAULT_WAIT_CAP = 900          # 15 min -- covers a full rebuild
_FILELOCK_TIMEOUT = 10.0
_FILELOCK_STALE = 30.0
# TTL is "how long after the holder's LAST uap call before the lease is reclaimed" -- every
# editor op by the holder heartbeats it (see cli.main), so an actively-working agent keeps its
# hold indefinitely without manual heartbeating.
#
# pie/level are deliberately shorter than rebuild. Now that the exclusive lease actually blocks
# other agents, an abandoned hold is far more damaging than it was when the lease was advisory:
# at 1200 it wedged every other agent for 20 minutes. rebuild stays long because a rebuild
# legitimately runs many minutes with no uap calls at all (the editor is down).
_TTL_BY_REASON = {"rebuild": 1200, "pie": 600, "level": 600, "read": 120}
_DEFAULT_TTL = 600

# --- FIFO wait queue --------------------------------------------------------------------------
# Before this, `acquire` was a race: every blocked agent polled independently and whoever happened
# to look in the instant after a release won, so an agent could wait out TWO full 900s caps and
# lose both times to agents that arrived later (2026-09-24; three of six agents abandoned runtime
# verification because of it). Each individual call behaved as documented; the emergent behaviour
# was starvation.
#
# So a blocked acquire now REGISTERS itself, in arrival order, and only the head of the queue may
# take the lease. The queue lives in the same state file, under the same filelock, so it is
# atomic with the grant decision -- there is no window in which a latecomer can slip past the head.
#
# A waiter re-registers (heartbeats) on every poll while it blocks. A waiter that dies, is killed,
# or times out therefore disappears on its own: this TTL is deliberately a few poll intervals, not
# a lease TTL, because a waiter is only ever alive inside one blocking call.
_WAITER_TTL = 15.0

# --- machine-wide foreground lock -------------------------------------------------------------
# Its own scope key, so it reuses the whole per-project file/lock/eviction machinery unchanged.
# `_safe_project` reserves this name, so a project literally called "_machine" cannot collide
# with it.
MACHINE_SCOPE = "_machine"
# Matches the pie/level project TTL: an abandoned hold must not wedge the OTHER project for
# longer than an abandoned hold wedges its own. Every foreground `uap` call from the holding
# project refreshes it, so an agent that is actually working keeps it indefinitely.
MACHINE_TTL = 600
# A hold created just for the duration of one command. Short, because the `finally` in cli.main
# releases it anyway -- the TTL is only the backstop for a killed process.
MACHINE_TRANSIENT_TTL = 120
_MACHINE_LOCK_OFF = {"0", "false", "no", "off"}


def _reports_base() -> pathlib.Path:
    root = os.environ.get("UAP_REPORTS_DIR")
    return pathlib.Path(root) if root else (pathlib.Path.home() / ".uap-reports")


def _leases_dir() -> pathlib.Path:
    d = _reports_base() / ".leases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_project(project: str | None) -> str:
    name = (project or "").strip().lower() or "_default"
    # MACHINE_SCOPE owns its own lease file. A project of that name would otherwise share it and
    # silently take/free the machine lock as if it were its own project lease.
    return f"{name}-project" if name == MACHINE_SCOPE else name


class _MachineScope(str):
    """Scope key for the machine-wide lock.

    A `str` subclass so it satisfies every `project: str | None` signature in this module and
    flows through `_load` / `_save` / the filelock unchanged, while `_scope_key` still tells it
    apart from a project name by TYPE. No string a caller can pass can be mistaken for it.
    """


MACHINE_SENTINEL = _MachineScope(MACHINE_SCOPE)


def _scope_key(project: str | None) -> str:
    if isinstance(project, _MachineScope):
        return MACHINE_SCOPE
    return _safe_project(project)


def _lease_path(project: str | None) -> pathlib.Path:
    return _leases_dir() / f"{_scope_key(project)}.json"


def _lock_path(project: str | None) -> pathlib.Path:
    return _leases_dir() / f"{_scope_key(project)}.lock"


def hold_ledger_path(project: str | None) -> pathlib.Path:
    """Per-project record of input holds the CLI has started but not waited out.

    Not a lease: it grants nothing and blocks nothing. It exists because a hold keeps running
    in-engine after the process that started it has exited, so without a note on disk no later
    call can tell that one is still live -- which is how consecutive `input hold` calls came to
    overlap in silence. Lives beside the leases because it is the same cross-process problem.
    """
    return _leases_dir() / f"{_scope_key(project)}.holds.json"


def default_agent_id() -> str:
    """Identity token for a lease.

    NOTE: this harness gives NO stable per-agent shell PID -- each tool call is a fresh shell and
    $PPID is a shared init (1). So there is no reliable auto-identity that persists ACROSS an
    agent's calls. Therefore:
      * single-process holds (a whole `uap rebuild` in one process) default to this process's pid,
        which is alive for the hold and reclaimed by PID-death when it exits -- fully robust;
      * holds that must span multiple calls (an agent keeping PIE for a while) MUST pass an explicit
        --agent token (and rely on TTL + heartbeat + release), because a per-call default would not
        survive to the next call.
    $UAP_AGENT_ID overrides.
    """
    env = os.environ.get("UAP_AGENT_ID")
    if env:
        return env.strip()
    return f"pid-{os.getpid()}"


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True  # fail-open: cannot check -> assume alive (TTL still reclaims it)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    except Exception:
        return True


def _now() -> float:
    return time.time()


class CoordinationError(RuntimeError):
    """A lease decision could not be made safely, so none was made.

    Both subclasses exist because of ClickUp 17tm466ftzp, where a LIVE unexpired exclusive
    lease was handed to a second agent and the holder's PIE session died under it. Both halves
    of that failure were a read that could not tell "the answer is no" from "I could not read
    the answer", and returned the benign-looking one. Anything that cannot establish the
    current state must refuse, not assume the lease is free.
    """


class LeaseUnavailable(CoordinationError):
    """The coordination mutex could not be taken."""


class LeaseStateUnreadable(CoordinationError):
    """The lease file exists but its contents could not be parsed."""


# Which lock file this process currently owns, and with what token. There is no nesting in this
# module -- every critical section acquires and releases the same scope's lock -- so one entry
# per scope is enough.
_LOCK_OWNER: dict[str, str] = {}


def _acquire_filelock(project: str | None) -> None:
    """Take the scope's mutex, or raise.

    It used to give the mutex up and carry on after `_FILELOCK_TIMEOUT`, with the reasoning
    that a wedged lockfile must not brick coordination. But every critical section in this
    module is a sub-millisecond read-modify-write, so a 10s wait never means "busy", it means
    "something is wrong" -- and carrying on unprotected makes two writers lose one of the two
    updates, which for `acquire` means two exclusive holders. The wedged-lock case is already
    handled, and better, by breaking a lock older than `_FILELOCK_STALE`; proceeding without
    the mutex only converted a visible stall into a silent exclusivity failure.
    """
    lp = _lock_path(project)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = _now() + _FILELOCK_TIMEOUT
    while True:
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, token.encode())
            finally:
                os.close(fd)
            _LOCK_OWNER[str(lp)] = token
            return
        except FileExistsError:
            try:
                if _now() - lp.stat().st_mtime > _FILELOCK_STALE:
                    lp.unlink()          # a wedged lock must not brick coordination
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                pass
            if _now() > deadline:
                raise LeaseUnavailable(
                    f"could not take the coordination lock {lp} within {_FILELOCK_TIMEOUT:.0f}s. "
                    f"Refusing to make a lease decision without it -- an unsynchronised "
                    f"read-modify-write here grants the same exclusive lease twice."
                ) from None
            time.sleep(0.05)


def _release_filelock(project: str | None) -> None:
    """Remove the lock only while it is still OURS.

    An unconditional unlink released whichever lock happened to be there. Once one lock had
    been broken as stale and replaced, the original owner's release deleted the NEW owner's
    lock on its way out, so a single lost lock cascaded into every pair of callers after it.
    """
    lp = _lock_path(project)
    mine = _LOCK_OWNER.pop(str(lp), None)
    if mine is None:
        return
    try:
        if lp.read_text(encoding="utf-8").strip() != mine:
            return                       # somebody broke ours and took it; leave theirs alone
    except OSError:
        return
    try:
        lp.unlink()
    except OSError:
        pass


def _blank() -> dict:
    return {"generation": 0, "exclusive": None, "shared": [], "waiters": []}


_LOAD_RETRIES = 5
_LOAD_RETRY_SLEEP = 0.02


def _load(project: str | None) -> dict:
    """The current state, or raise -- never a blank state standing in for an unreadable one.

    ABSENT and UNREADABLE are different facts and this used to collapse them: any
    JSONDecodeError became `_blank()`, whose `exclusive` is None, i.e. "the lease is free".
    Combined with the non-atomic `_save` below, a reader that caught the file mid-write
    concluded nobody held the lease, granted itself exclusive, and wrote that over the real
    holder's record -- which is how a live, unexpired exclusive lease vanished and its holder's
    PIE session died under the next agent (ClickUp 17tm466ftzp).

    A missing file still means genuinely blank: nothing has ever taken this lease. A present
    but unparseable file is retried briefly -- if it is a torn read it resolves in
    milliseconds -- and then refused.
    """
    p = _lease_path(project)
    detail = "unknown"
    for attempt in range(_LOAD_RETRIES):
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _blank()          # never written -> nobody holds anything
        except OSError as exc:
            detail = repr(exc)
        else:
            try:
                state = json.loads(raw)
            except ValueError as exc:
                detail = f"{exc} (first 80 bytes: {raw[:80]!r})"
            else:
                if isinstance(state, dict):
                    state.setdefault("generation", 0)
                    state.setdefault("exclusive", None)
                    state.setdefault("shared", [])
                    state.setdefault("waiters", [])
                    return state
                detail = f"top level is {type(state).__name__}, not an object"
        if attempt < _LOAD_RETRIES - 1:
            time.sleep(_LOAD_RETRY_SLEEP)
    raise LeaseStateUnreadable(
        f"lease state {p} exists but could not be read after {_LOAD_RETRIES} attempts: "
        f"{detail}. Refusing to treat it as free -- that is how a live lease gets handed to a "
        f"second agent. Delete the file only if no agent holds the editor.")


def _save(project: str | None, state: dict) -> None:
    """Atomically replace the state file.

    `write_text` truncates the target and then writes, so a reader could see an empty or
    half-written file -- and `_load` read that as "the lease is free". `os.replace` is atomic
    on both Windows and POSIX, so a reader now sees either the previous state or the new one,
    never a partial one.
    """
    p = _lease_path(project)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass                     # already renamed into place, or never created


def _holder_alive(h: dict) -> bool:
    if _now() - float(h.get("heartbeat_at", 0)) > float(h.get("ttl", _DEFAULT_TTL)):
        return False  # heartbeat stale -> dead, regardless of pid
    pid = int(h.get("pid", 0))
    if pid == 0:
        return True  # TTL-only hold (cross-call): heartbeat is the only liveness signal
    return _pid_alive(pid)


def _evict_stale(state: dict) -> dict:
    ex = state.get("exclusive")
    if ex and not _holder_alive(ex):
        state["exclusive"] = None
    state["shared"] = [h for h in state.get("shared", []) if _holder_alive(h)]
    return state


def _grantable(state: dict, mode: str, agent: str) -> bool:
    ex = state.get("exclusive")
    if mode == "exclusive":
        others_shared = [h for h in state.get("shared", []) if h.get("agent") != agent]
        # Re-entrant: an agent re-acquiring its OWN exclusive is fine (never self-deadlock).
        return (ex is None or ex.get("agent") == agent) and not others_shared
    # shared: ok unless someone else holds exclusive
    return ex is None or ex.get("agent") == agent


def _holds_already(state: dict, mode: str, agent: str) -> bool:
    """Does `agent` already hold this lease in `mode`?

    A re-acquire is a refresh, not a request, so it must never join the queue: an agent that
    already owns the lease queueing behind its own waiters would deadlock itself.
    """
    ex = state.get("exclusive")
    if mode == "exclusive":
        return bool(ex) and ex.get("agent") == agent
    return any(h.get("agent") == agent for h in state.get("shared", []))


def _waiter_alive(w: dict) -> bool:
    if _now() - float(w.get("heartbeat_at", 0)) > _WAITER_TTL:
        return False            # stopped polling -> the call it belonged to is over
    pid = int(w.get("pid", 0) or 0)
    return _pid_alive(pid) if pid else True


def _evict_stale_waiters(state: dict) -> dict:
    state["waiters"] = [w for w in state.get("waiters", []) if _waiter_alive(w)]
    return state


def _enqueue(state: dict, agent: str, mode: str, reason: str) -> None:
    """Register `agent` as waiting, or refresh it if it already is.

    Refreshing deliberately does NOT move it to the back -- arrival order is the whole point.
    """
    q = state.setdefault("waiters", [])
    for w in q:
        if w.get("agent") == agent:
            w["heartbeat_at"] = _now()
            w["mode"] = mode
            return
    q.append({"agent": agent, "mode": mode, "reason": reason, "pid": os.getpid(),
              "since": _now(), "heartbeat_at": _now()})


def _dequeue(state: dict, agent: str) -> None:
    state["waiters"] = [w for w in state.get("waiters", []) if w.get("agent") != agent]


def _queue_position(state: dict, agent: str) -> int:
    """1-based position in the queue; 0 when not queued."""
    for i, w in enumerate(state.get("waiters", [])):
        if w.get("agent") == agent:
            return i + 1
    return 0


def _queue_ahead(state: dict, agent: str) -> list[str]:
    ahead: list[str] = []
    for w in state.get("waiters", []):
        if w.get("agent") == agent:
            break
        ahead.append(str(w.get("agent")))
    return ahead


def _turn_ok(state: dict, mode: str, agent: str) -> bool:
    """FIFO gate: is it this agent's turn to take the lease?

    Only the head of the queue may take it -- with one exception, a run of SHARED waiters at the
    head. Those do not exclude one another, so making the 2nd shared waiter sit out a poll cycle
    behind the 1st would cost latency and buy nothing. An exclusive waiter anywhere ahead always
    blocks, which is exactly what stops shared traffic from starving a rebuild forever.
    """
    for w in state.get("waiters", []):
        if w.get("agent") == agent:
            return True
        if mode != "shared" or w.get("mode") != "shared":
            return False
    return True                 # not queued at all (fail-open)


def _ttl_for(reason: str, ttl: int | None) -> int:
    if ttl:
        return int(ttl)
    return _TTL_BY_REASON.get((reason or "").split(":")[0], _DEFAULT_TTL)


def acquire(project: str | None, mode: str, *, reason: str = "", agent: str | None = None,
            pid: int | None = None, wait: float = DEFAULT_WAIT_CAP, ttl: int | None = None) -> dict:
    """Block until a `mode` ('exclusive'|'shared') lease is grantable, then take it.

    `pid` is the liveness anchor stored with the lease (default: this process). Pass the pid of a
    long-lived process (e.g. a rebuild script's $PID) to have PID-death auto-reclaim the lease, or
    pass 0 for a cross-call hold that relies on TTL + heartbeat instead.

    Waiting is FIFO. A blocked caller joins the queue in arrival order and only the head may take
    the lease when it frees, so an agent that has been waiting longest cannot be overtaken by one
    that arrives later. The `busy` return carries `queue_position` / `queue_ahead` so a caller
    that gives up can say whether it was next or ninth instead of retrying blind.

    Returns {ok, granted, generation, ...} on success, or {ok:False, busy:True, holder} if the
    wait cap elapses. Re-entrant per agent (re-acquiring refreshes, never deadlocks on self).
    """
    if mode not in ("exclusive", "shared"):
        raise ValueError(f"mode must be exclusive|shared, got {mode!r}")
    agent = agent or default_agent_id()
    pid = os.getpid() if pid is None else int(pid)
    ttl = _ttl_for(reason, ttl)
    started = _now()
    deadline = started + max(0.0, wait)
    holder = None
    position = 0
    ahead: list[str] = []
    queue_len = 0
    while True:
        _acquire_filelock(project)
        try:
            state = _evict_stale_waiters(_evict_stale(_load(project)))
            mine = _holds_already(state, mode, agent)
            if not mine:
                _enqueue(state, agent, mode, reason)
            if _grantable(state, mode, agent) and (mine or _turn_ok(state, mode, agent)):
                _dequeue(state, agent)
                rec = {"agent": agent, "reason": reason, "pid": pid,
                       "acquired_at": _now(), "heartbeat_at": _now(), "ttl": ttl}
                if mode == "exclusive":
                    state["exclusive"] = rec
                    state["shared"] = [h for h in state["shared"] if h.get("agent") != agent]
                else:
                    state["shared"] = ([h for h in state["shared"] if h.get("agent") != agent]
                                       + [rec])
                _save(project, state)
                return {"ok": True, "granted": True, "agent": agent, "mode": mode,
                        "reason": reason, "generation": state.get("generation", 0),
                        "queued_seconds": round(_now() - started, 1),
                        "queue_length": len(state.get("waiters", []))}
            holder = state.get("exclusive") or (state.get("shared") or [None])[0]
            position = _queue_position(state, agent)
            ahead = _queue_ahead(state, agent)
            queue_len = len(state.get("waiters", []))
            # Persist the registration/heartbeat, so every other waiter can see our place.
            _save(project, state)
        finally:
            _release_filelock(project)
        if _now() >= deadline:
            # Give the slot back: a caller that has stopped waiting must not hold up the queue
            # for the _WAITER_TTL it would otherwise take to age out.
            _acquire_filelock(project)
            try:
                state = _load(project)
                _dequeue(state, agent)
                _save(project, state)
            finally:
                _release_filelock(project)
            return {"ok": False, "granted": False, "busy": True, "agent": agent,
                    "mode": mode, "holder": holder,
                    "waited_seconds": round(max(0.0, wait), 1),
                    "queue_position": position, "queue_length": queue_len,
                    "queue_ahead": ahead,
                    "hint": ("you left the FIFO queue by timing out; a fresh acquire re-joins at "
                             "the BACK. queue_position is where you stood when the wait capped: "
                             "1 means you were next." if position
                             else "the lease was held but you were not queued behind it")}
        time.sleep(POLL_SECONDS)


def release(project: str | None, *, agent: str | None = None) -> dict:
    agent = agent or default_agent_id()
    _acquire_filelock(project)
    try:
        state = _load(project)
        ex = state.get("exclusive")
        was = False
        if ex and ex.get("agent") == agent:
            state["exclusive"] = None
            was = True
        before = len(state.get("shared", []))
        state["shared"] = [h for h in state.get("shared", []) if h.get("agent") != agent]
        was = was or len(state["shared"]) != before
        # An agent that releases is done, so it is not waiting for anything either. Drops a stale
        # queue entry left by a killed `acquire --wait` from the same token.
        _dequeue(state, agent)
        _save(project, state)
    finally:
        _release_filelock(project)
    return {"ok": True, "released": was, "agent": agent}


def heartbeat(project: str | None, *, agent: str | None = None) -> dict:
    agent = agent or default_agent_id()
    _acquire_filelock(project)
    try:
        state = _load(project)
        touched = 0
        ex = state.get("exclusive")
        if ex and ex.get("agent") == agent:
            ex["heartbeat_at"] = _now()
            touched += 1
        for h in state.get("shared", []):
            if h.get("agent") == agent:
                h["heartbeat_at"] = _now()
                touched += 1
        _save(project, state)
    finally:
        _release_filelock(project)
    return {"ok": True, "refreshed": touched, "agent": agent}


def bump_generation(project: str | None) -> int:
    """Signal that the editor bounced (rebuild). Shared users notice their RC died and re-sync."""
    _acquire_filelock(project)
    try:
        state = _load(project)
        state["generation"] = int(state.get("generation", 0)) + 1
        _save(project, state)
        return state["generation"]
    finally:
        _release_filelock(project)


def status(project: str | None) -> dict:
    _acquire_filelock(project)
    try:
        state = _evict_stale_waiters(_evict_stale(_load(project)))
        _save(project, state)
    finally:
        _release_filelock(project)
    # `waiters` rides along inside **state; `queue` names them in arrival order so a human
    # reading the output can see who is next without decoding timestamps.
    return {"ok": True, "project": _scope_key(project),
            "queue": [str(w.get("agent")) for w in state.get("waiters", [])], **state}


def wait_while_rebuild(project: str | None, *, wait: float = DEFAULT_WAIT_CAP) -> dict:
    """For any editor-touching op: block while a REBUILD is in progress (editor is down), then
    return so the op runs against the freshly-relaunched editor instead of hard-failing.

    Identity-free on purpose -- it keys on the exclusive lease's reason (`rebuild*`), not on who
    holds it, so it works despite this harness having no stable per-agent id. Fail-open: any
    coordination error returns immediately so a lease bug can never brick uap.
    """
    deadline = _now() + max(0.0, wait)
    waited = False
    while True:
        try:
            _acquire_filelock(project)
            try:
                state = _evict_stale_waiters(_evict_stale(_load(project)))
                _save(project, state)
            finally:
                _release_filelock(project)
            ex = state.get("exclusive")
            if not ex or not str(ex.get("reason", "")).startswith("rebuild"):
                return {"ok": True, "waited": waited}
            holder = ex
        except Exception:
            return {"ok": True, "waited": waited, "note": "coordination unavailable; proceeding"}
        if _now() >= deadline:
            return {"ok": False, "timed_out": True, "holder": holder}
        waited = True
        time.sleep(POLL_SECONDS)


# ----------------------------------------------------------------------------------------------
# Machine-wide foreground lock
#
# The project lease is scoped to one project's editor, which is correct for everything that
# project's editor owns alone (its level, its RC port, its PIE world) and useless for everything
# the WORKSTATION owns: the keyboard, the foreground window, the GPU. Two projects open at once
# (Project Broken Wings + School's Out VR on the same box, running the same uap install) each held
# their own lease and happily started PIE under each other.
#
# So: one more turn, taken machine-wide, by the ops that genuinely need those shared resources.
# Everything else -- reads, `rc`, `exec`, `status`, logs -- is untouched and stays project-scoped.
# ----------------------------------------------------------------------------------------------


def machine_lock_enabled() -> bool:
    """False disables the machine lock entirely ($UAP_MACHINE_LOCK=0).

    An escape hatch, not a setting to reach for: it exists so a lock bug can never be the thing
    standing between an agent and the editor.
    """
    raw = os.environ.get("UAP_MACHINE_LOCK")
    return not (raw is not None and raw.strip().lower() in _MACHINE_LOCK_OFF)


def _machine_holder() -> dict | None:
    state = _evict_stale(_load(MACHINE_SENTINEL))
    _save(MACHINE_SENTINEL, state)
    return state.get("exclusive")


def _machine_busy(holder: dict | None, agent: str, wait: float, *, position: int = 0,
                  ahead: list[str] | None = None, queue_len: int = 0) -> dict:
    holder = holder or {}
    return {"ok": False, "granted": False, "busy": True, "acquired": False,
            "agent": agent, "scope": "machine",
            "blocked_by": holder.get("agent"), "project": holder.get("project"),
            "reason": holder.get("reason"), "holder": holder,
            "waited_seconds": round(max(0.0, wait), 1),
            "queue_position": position, "queue_ahead": ahead or [], "queue_length": queue_len}


def acquire_machine(project: str | None, *, reason: str = "", agent: str | None = None,
                    pid: int | None = None, wait: float = DEFAULT_WAIT_CAP,
                    ttl: int | None = None) -> dict:
    """Block until this machine's foreground lock is ours, then take it.

    Grant rule -- the lock arbitrates BETWEEN projects, never within one:
      * free                       -> take it (`acquired: True`, caller owns the release);
      * held by THIS project       -> pass through, refresh its heartbeat (`acquired: False`),
                                      because intra-project turn-taking is the project lease's
                                      job and a second gate there would only deadlock agents that
                                      the project lease already serializes correctly;
      * held by ANOTHER project    -> poll until it frees, then as above; on the wait cap, return
                                      `{ok: False, busy: True, blocked_by, project}`.

    Pass `pid=0` for a hold that must outlive this process (a PIE session held across several
    calls); the default anchors to this process so a kill reclaims it immediately.

    Waiting here is FIFO for the same reason the project lease's is (ClickUp 17tm466ft2n): with
    two projects open, whichever agent happened to poll first after a release won, so the one
    that had waited longest could be overtaken indefinitely. A blocked caller queues; only the
    head may take the lock. The pass-through case (this project already holds it) skips the queue
    entirely -- it is not a request, and queueing it would deadlock the holding project.
    """
    agent = agent or default_agent_id()
    pid = os.getpid() if pid is None else int(pid)
    ttl = int(ttl) if ttl else MACHINE_TTL
    mine = _safe_project(project)
    deadline = _now() + max(0.0, wait)
    holder = None
    position = 0
    ahead: list[str] = []
    queue_len = 0
    while True:
        _acquire_filelock(MACHINE_SENTINEL)
        try:
            state = _evict_stale_waiters(_evict_stale(_load(MACHINE_SENTINEL)))
            holder = state.get("exclusive")
            if holder is not None and holder.get("project") == mine:
                holder["heartbeat_at"] = _now()
                _dequeue(state, agent)
                _save(MACHINE_SENTINEL, state)
                return {"ok": True, "granted": True, "acquired": False, "agent": agent,
                        "scope": "machine", "project": mine, "holder": holder}
            _enqueue(state, agent, "exclusive", reason)
            if holder is None and _turn_ok(state, "exclusive", agent):
                _dequeue(state, agent)
                rec = {"agent": agent, "project": mine, "reason": reason, "pid": pid,
                       "acquired_at": _now(), "heartbeat_at": _now(), "ttl": ttl}
                state["exclusive"] = rec
                _save(MACHINE_SENTINEL, state)
                return {"ok": True, "granted": True, "acquired": True, "agent": agent,
                        "scope": "machine", "project": mine, "reason": reason, "holder": rec}
            position = _queue_position(state, agent)
            ahead = _queue_ahead(state, agent)
            queue_len = len(state.get("waiters", []))
            _save(MACHINE_SENTINEL, state)
        finally:
            _release_filelock(MACHINE_SENTINEL)
        if _now() >= deadline:
            _acquire_filelock(MACHINE_SENTINEL)
            try:
                state = _load(MACHINE_SENTINEL)
                _dequeue(state, agent)
                _save(MACHINE_SENTINEL, state)
            finally:
                _release_filelock(MACHINE_SENTINEL)
            return _machine_busy(holder, agent, wait, position=position, ahead=ahead,
                                 queue_len=queue_len)
        time.sleep(POLL_SECONDS)


def release_machine(*, agent: str | None = None, project: str | None = None,
                    force: bool = False) -> dict:
    """Free the machine lock if it is ours.

    Matches on the holder's `agent` OR its `project`: `uap pie stop` legitimately frees the hold a
    DIFFERENT agent on the same project took with `pie start`, because the thing being held (that
    project's PIE session) is genuinely over. `force=True` breaks a hold belonging to anyone --
    the break-glass for a session that died without releasing and has not yet aged out.
    """
    agent = agent or default_agent_id()
    mine = _safe_project(project) if project is not None else None
    holder = None
    released = False
    _acquire_filelock(MACHINE_SENTINEL)
    try:
        state = _load(MACHINE_SENTINEL)
        holder = state.get("exclusive")
        if holder and (force or holder.get("agent") == agent
                       or (mine is not None and holder.get("project") == mine)):
            state["exclusive"] = None
            released = True
            _dequeue(state, agent)
            _save(MACHINE_SENTINEL, state)
    finally:
        _release_filelock(MACHINE_SENTINEL)
    return {"ok": True, "released": released, "scope": "machine", "agent": agent,
            "was_held_by": holder if released else None, "holder": None if released else holder}


def machine_status() -> dict:
    """Who owns this machine's foreground right now (stale holders evicted first)."""
    _acquire_filelock(MACHINE_SENTINEL)
    try:
        holder = _machine_holder()
        queue = [str(w.get("agent")) for w in _load(MACHINE_SENTINEL).get("waiters", [])]
    finally:
        _release_filelock(MACHINE_SENTINEL)
    return {"ok": True, "scope": "machine", "enabled": machine_lock_enabled(), "holder": holder,
            "queue": queue}


def wait_if_blocked(project: str | None, *, agent: str | None = None,
                    wait: float = DEFAULT_WAIT_CAP) -> dict:
    """For read-only ops: block while ANOTHER agent holds the exclusive lease, then return.

    Fail-open: any coordination error returns immediately so a lease bug can never brick uap.
    """
    agent = agent or default_agent_id()
    deadline = _now() + max(0.0, wait)
    while True:
        try:
            _acquire_filelock(project)
            try:
                state = _evict_stale_waiters(_evict_stale(_load(project)))
                _save(project, state)
            finally:
                _release_filelock(project)
            ex = state.get("exclusive")
            if ex is None or ex.get("agent") == agent:
                return {"ok": True, "blocked": False}
            holder = ex
        except Exception:
            return {"ok": True, "blocked": False, "note": "coordination unavailable; proceeding"}
        if _now() >= deadline:
            return {"ok": False, "blocked": True, "holder": holder}
        time.sleep(POLL_SECONDS)
