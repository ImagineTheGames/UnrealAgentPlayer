"""Cross-agent editor coordination -- a per-project lease.

Multiple agents share ONE editor per project (one level, one PIE session; a rebuild takes it
down entirely). This lets them take turns instead of stepping on each other or hard-failing.

Model (decided with the user):
  * shared reads + one exclusive writer -- many agents may read concurrently; rebuild / PIE /
    level-load takes an exclusive turn that blocks reads until done.
  * acquire BLOCKS (polls) until grantable, up to a cap, then returns a structured `busy`.
  * crash-safe: each poll evicts holders whose owning process is gone OR whose heartbeat is
    stale (TTL), so a dead agent never wedges the editor.

State lives in one JSON file per project under `<reports>/.leases/<project>.json`, read-modify-
written under an O_EXCL lockfile so concurrent agents update it atomically. Pure stdlib, so it
is live for every agent via the shared venv -- no editor rebuild, no plugin change.
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
# Generous TTLs so the common case never needs manual heartbeating; PID-liveness reclaims sooner.
_TTL_BY_REASON = {"rebuild": 1200, "pie": 1200, "level": 1200, "read": 120}
_DEFAULT_TTL = 600


def _reports_base() -> pathlib.Path:
    root = os.environ.get("UAP_REPORTS_DIR")
    return pathlib.Path(root) if root else (pathlib.Path.home() / ".uap-reports")


def _leases_dir() -> pathlib.Path:
    d = _reports_base() / ".leases"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_project(project: str | None) -> str:
    return (project or "").strip().lower() or "_default"


def _lease_path(project: str | None) -> pathlib.Path:
    return _leases_dir() / f"{_safe_project(project)}.json"


def _lock_path(project: str | None) -> pathlib.Path:
    return _leases_dir() / f"{_safe_project(project)}.lock"


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
    return {"ok": True, "project": _safe_project(project), **state}


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
