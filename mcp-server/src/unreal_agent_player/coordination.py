"""Cross-agent editor coordination -- a per-project lease plus a machine-wide foreground lock.

Two scopes, because two different resources are contended:

1. **Per-project lease** (`<reports>/.leases/<project>.json`). Multiple agents share ONE editor
   per project (one level, one PIE session; a rebuild takes it down entirely). This lets them
   take turns instead of stepping on each other or hard-failing.

   Model (decided with the user):
     * shared reads + one exclusive writer -- many agents may read concurrently; rebuild / PIE /
       level-load takes an exclusive turn that blocks reads until done.
     * acquire BLOCKS (polls) until grantable, up to a cap, then returns a structured `busy`.
     * crash-safe: each poll evicts holders whose owning process is gone OR whose heartbeat is
       stale (TTL), so a dead agent never wedges the editor.

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


def _acquire_filelock(project: str | None) -> None:
    lp = _lock_path(project)
    deadline = _now() + _FILELOCK_TIMEOUT
    while True:
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            try:
                if _now() - lp.stat().st_mtime > _FILELOCK_STALE:
                    lp.unlink()
                    continue
            except FileNotFoundError:
                continue
            if _now() > deadline:
                # Give up the mutex but proceed -- a wedged lockfile must not brick coordination.
                return
            time.sleep(0.05)


def _release_filelock(project: str | None) -> None:
    try:
        _lock_path(project).unlink()
    except FileNotFoundError:
        pass


def _blank() -> dict:
    return {"generation": 0, "exclusive": None, "shared": []}


def _load(project: str | None) -> dict:
    p = _lease_path(project)
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return _blank()
    state.setdefault("generation", 0)
    state.setdefault("exclusive", None)
    state.setdefault("shared", [])
    return state


def _save(project: str | None, state: dict) -> None:
    _lease_path(project).write_text(json.dumps(state, indent=2), encoding="utf-8")


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

    Returns {ok, granted, generation, ...} on success, or {ok:False, busy:True, holder} if the
    wait cap elapses. Re-entrant per agent (re-acquiring refreshes, never deadlocks on self).
    """
    if mode not in ("exclusive", "shared"):
        raise ValueError(f"mode must be exclusive|shared, got {mode!r}")
    agent = agent or default_agent_id()
    pid = os.getpid() if pid is None else int(pid)
    ttl = _ttl_for(reason, ttl)
    deadline = _now() + max(0.0, wait)
    holder = None
    while True:
        _acquire_filelock(project)
        try:
            state = _evict_stale(_load(project))
            if _grantable(state, mode, agent):
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
                        "reason": reason, "generation": state.get("generation", 0)}
            holder = state.get("exclusive") or (state.get("shared") or [None])[0]
        finally:
            _release_filelock(project)
        if _now() >= deadline:
            return {"ok": False, "granted": False, "busy": True, "agent": agent,
                    "mode": mode, "holder": holder, "waited_seconds": round(max(0.0, wait), 1)}
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
        state = _evict_stale(_load(project))
        _save(project, state)
    finally:
        _release_filelock(project)
    return {"ok": True, "project": _scope_key(project), **state}


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
                state = _evict_stale(_load(project))
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


def _machine_busy(holder: dict | None, agent: str, wait: float) -> dict:
    holder = holder or {}
    return {"ok": False, "granted": False, "busy": True, "acquired": False,
            "agent": agent, "scope": "machine",
            "blocked_by": holder.get("agent"), "project": holder.get("project"),
            "reason": holder.get("reason"), "holder": holder,
            "waited_seconds": round(max(0.0, wait), 1)}


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
    """
    agent = agent or default_agent_id()
    pid = os.getpid() if pid is None else int(pid)
    ttl = int(ttl) if ttl else MACHINE_TTL
    mine = _safe_project(project)
    deadline = _now() + max(0.0, wait)
    holder = None
    while True:
        _acquire_filelock(MACHINE_SENTINEL)
        try:
            state = _evict_stale(_load(MACHINE_SENTINEL))
            holder = state.get("exclusive")
            if holder is not None and holder.get("project") == mine:
                holder["heartbeat_at"] = _now()
                _save(MACHINE_SENTINEL, state)
                return {"ok": True, "granted": True, "acquired": False, "agent": agent,
                        "scope": "machine", "project": mine, "holder": holder}
            if holder is None:
                rec = {"agent": agent, "project": mine, "reason": reason, "pid": pid,
                       "acquired_at": _now(), "heartbeat_at": _now(), "ttl": ttl}
                state["exclusive"] = rec
                _save(MACHINE_SENTINEL, state)
                return {"ok": True, "granted": True, "acquired": True, "agent": agent,
                        "scope": "machine", "project": mine, "reason": reason, "holder": rec}
        finally:
            _release_filelock(MACHINE_SENTINEL)
        if _now() >= deadline:
            return _machine_busy(holder, agent, wait)
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
    finally:
        _release_filelock(MACHINE_SENTINEL)
    return {"ok": True, "scope": "machine", "enabled": machine_lock_enabled(), "holder": holder}


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
                state = _evict_stale(_load(project))
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
