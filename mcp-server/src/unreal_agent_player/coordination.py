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
     * a `pie`/`level` exclusive lease protects a live editor session; when no editor process for
       the project is running (and it is not mid-rebuild) there is nothing left to protect, so
       the hold is void and evicted at once rather than after its TTL (`_evict_dead_editor`).

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
from collections.abc import Callable

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


# --- human labels -----------------------------------------------------------------------------
# An agent token (`pcgprof`, `rebuild-48876`) says WHO but never WHAT, and it is all the in-editor
# overlay and `uap lease status` had to show a director looking at a busy editor. A task label is a
# short free-text string the agent supplies (`--task` / $UAP_TASK) and the lease, the waiter queue,
# the machine lock and the activity note all carry it, so every one of those views can say what the
# agent is for. It is display-only: it never takes part in identity, ordering or any grant decision.
TASK_MAX = 80
UAP_TASK_ENV = "UAP_TASK"
_ACTIVITY_TTL = 300.0       # how long an agent's last-verb note stays on the record


def sanitize_label(text) -> str:
    """A label that is safe to store, print and draw: one line, no control chars, <= TASK_MAX.

    The overlay draws it in a fixed-width box and the lease file is read by other tools, so
    newlines, tabs and other control characters are folded to single spaces and the result is
    cut at TASK_MAX with a trailing `...`. None / non-strings become "".
    """
    if text is None:
        return ""
    out = "".join(" " if (ch.isspace() or ord(ch) < 32 or ord(ch) == 127) else ch
                  for ch in str(text))
    out = " ".join(out.split())
    if len(out) > TASK_MAX:
        out = out[:TASK_MAX - 3].rstrip() + "..."
    return out


def default_task() -> str:
    """The task label from $UAP_TASK (sanitised), or ""."""
    return sanitize_label(os.environ.get(UAP_TASK_ENV))


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
    return {"generation": 0, "exclusive": None, "shared": [], "waiters": [], "evicted": {},
            "last_eviction": None, "activity": {}}


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
                    state.setdefault("evicted", {})
                    state.setdefault("last_eviction", None)
                    state.setdefault("activity", {})
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


# --- dead-editor eviction ---------------------------------------------------------------------
# A `pie` / `level` exclusive lease protects a LIVE editor session: the level that is loaded, the
# PIE world that is running. If the editor process itself is gone, that session cannot exist any
# more and the lease protects nothing -- yet it kept blocking everyone for its full TTL, and the
# holder's own `uap` calls (which wait for the editor to come back) refreshed the heartbeat so it
# never even aged out. 2026-10-22: the PBW editor died (out of memory) under agent `respawnfix`'s
# `pie` lease, and the queued Restart-Editor, the one thing that could bring the editor back,
# waited on a lease whose entire reason for existing had just crashed.
#
# So these holds are void when no editor process for the project is running. Fail SAFE throughout:
# only a probe that positively says "no editor for this project" evicts. An unreadable process
# list, a protected process whose command line cannot be read, a non-Windows host or an unknown
# project all answer None, and None never evicts (the TTL still reclaims a genuinely abandoned hold).
EVICT_EDITOR_NOT_RUNNING = "editor_not_running"
_EDITOR_BOUND_REASONS = ("pie", "level")
_EDITOR_PROBE_OFF = {"0", "false", "no", "off"}
# "Running" is cached longer than "not running": a stale "running" only delays an eviction by a few
# seconds, but a stale "not running" would void a hold taken just after the editor came up.
_EDITOR_UP_CACHE_SECONDS = 5.0
_EDITOR_DOWN_CACHE_SECONDS = 1.0
_EDITOR_CACHE: dict[str, tuple[float, bool]] = {}
_EVICTION_NOTICE_TTL = 3600.0        # how long an evicted holder can still be told why


def _enumerate_editors() -> list[dict]:
    """Every `UnrealEditor.exe` process: [{pid, exe, cmdline}] (`cmdline` None if unreadable).

    Windows only and dependency-free: a Toolhelp snapshot for the process list and the command
    line read that window_capture already does. Far cheaper than a WMI/CIM query (no PowerShell
    spawn), which matters because lease status gets polled. Raises on any failure; the caller
    turns that into "unknown".
    """
    if os.name != "nt":
        raise OSError("editor process enumeration is only implemented on Windows")
    import ctypes
    from ctypes import wintypes

    from unreal_agent_player import window_capture as wc

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    TH32CS_SNAPPROCESS = 0x2
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    pids: list[int] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == "unrealeditor.exe":
                pids.append(int(entry.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    out = []
    for pid in pids:
        exe, cmdline = wc._process_info(pid)
        out.append({"pid": pid, "exe": exe, "cmdline": cmdline})
    return out


def editor_running(project: str | None) -> bool | None:
    """Is an EDITOR process for `project` running? True / False, or None when it cannot be told.

    Only `UnrealEditor.exe` whose command line names `<project>.uproject` counts (case-insensitive
    on the .uproject stem, which is the lease's project key), so School's Out's editor is never
    Project Broken Wings' and vice versa. A standalone `-game` client of the same project is NOT the
    editor: it has no level or PIE session for a lease to protect.

    None (never False) when the answer is uncertain -- the process list failed, an editor's command
    line was unreadable (protected / other user), the host is not Windows, or the project is
    unknown -- because callers evict on False. `$UAP_EDITOR_LIVENESS=0` forces None.
    """
    raw = os.environ.get("UAP_EDITOR_LIVENESS")
    if raw is not None and raw.strip().lower() in _EDITOR_PROBE_OFF:
        return None
    key = _scope_key(project)
    if key in ("_default", MACHINE_SCOPE):
        return None
    hit = _EDITOR_CACHE.get(key)
    if hit is not None:
        age = _now() - hit[0]
        if age < (_EDITOR_UP_CACHE_SECONDS if hit[1] else _EDITOR_DOWN_CACHE_SECONDS):
            return hit[1]
    try:
        from unreal_agent_player import window_capture as wc

        found = unreadable = False
        for p in _enumerate_editors():
            cmd = p.get("cmdline")
            if cmd is None:
                unreadable = True
                continue
            info = wc.parse_cmdline(cmd)
            if info.get("game_client"):
                continue
            if (info.get("project") or "").lower() == key:
                found = True
    except Exception:
        return None
    if not found and unreadable:
        return None                  # an editor we could not identify might be this project's
    _EDITOR_CACHE[key] = (_now(), found)
    return found


# The seam the eviction logic goes through. Tests (and the suite-wide conftest, which must not let
# the developer's real running editors decide what a test asserts) replace this one name.
_editor_probe: Callable[[str | None], bool | None] = editor_running


def _is_rebuild_reason(reason) -> bool:
    return str(reason or "").strip().lower().startswith(("rebuild", "relaunch"))


def _mid_rebuild(state: dict) -> bool:
    """Is a rebuild/relaunch hold on this project right now? Then the editor being down is the
    plan, not a fault, and nothing here is ours to void."""
    held = [state.get("exclusive"), *state.get("shared", [])]
    return any(h and _is_rebuild_reason(h.get("reason")) for h in held)


def _evict_dead_editor(state: dict, project: str | None) -> dict | None:
    """Void a `pie`/`level` exclusive lease whose editor is gone. Returns the eviction record.

    Leaves a note for the evicted holder (`evicted`, consumed by `take_eviction_notice`) and a
    lasting `last_eviction` for `uap lease status`, so the reason is never a silent mystery.
    Project scopes only: the machine lock's holders are not editor sessions.
    """
    ex = state.get("exclusive")
    if not ex or isinstance(project, _MachineScope):
        return None
    if str(ex.get("reason", "")).strip().lower().split(":")[0] not in _EDITOR_BOUND_REASONS:
        return None
    if _mid_rebuild(state):
        return None
    try:
        running = _editor_probe(project)
    except Exception:
        return None                  # fail safe: a broken probe never evicts
    if running is not False:
        return None
    key = _scope_key(project)
    rec = {"agent": ex.get("agent"), "held_reason": ex.get("reason"),
           "why": EVICT_EDITOR_NOT_RUNNING, "evicted_at": _now(),
           "acquired_at": ex.get("acquired_at"), "project": key,
           "message": (f"lease_evicted: {EVICT_EDITOR_NOT_RUNNING} -- the {key} editor process is "
                       f"gone, so your '{ex.get('reason')}' lease protected nothing and was "
                       f"voided. Any PIE session or loaded level died with the editor. "
                       f"Re-acquire once the editor is back.")}
    state["exclusive"] = None
    now = _now()
    notes = {a: n for a, n in state.get("evicted", {}).items()
             if now - float(n.get("evicted_at", 0)) < _EVICTION_NOTICE_TTL}
    if rec["agent"]:
        notes[str(rec["agent"])] = rec
    state["evicted"] = notes
    state["last_eviction"] = rec
    return rec


def _rebuild_first(state: dict, project: str | None) -> None:
    """With the editor down, move queued `rebuild*` requests ahead of pie/level (and other) waiters.

    FIFO is right while the editor is up: everyone is waiting for the same live session. With it
    down, a rebuild is the only thing that can end the outage and every other waiter needs the
    editor back first, so letting an earlier pie waiter go ahead would just hand it a corpse.
    Stable within each group. Only acts on a positive "editor not running".
    """
    q = state.get("waiters", [])
    first = [w for w in q if _is_rebuild_reason(w.get("reason"))]
    if not first or len(first) == len(q):
        return
    if all(_is_rebuild_reason(w.get("reason")) for w in q[:len(first)]):
        return                       # already at the head: nothing to reorder, no need to probe
    try:
        running = _editor_probe(project)
    except Exception:
        return
    if running is False:
        state["waiters"] = first + [w for w in q if not _is_rebuild_reason(w.get("reason"))]


def _refresh(project: str | None) -> tuple[dict, dict | None]:
    """Load the project's state with every kind of dead entry settled. Call with the lock held.

    Returns (state, eviction record or None); the caller saves, and after releasing the lock hands
    the record to `_after_eviction`.
    """
    state = _evict_stale(_load(project))
    evicted = _evict_dead_editor(state, project)
    _evict_stale_waiters(state)
    _rebuild_first(state, project)
    return state, evicted


def _after_eviction(project: str | None, evicted: dict | None) -> None:
    """A voided PIE/level hold must not leave the machine-wide foreground lock behind it.

    The same hold usually took the machine turn too (`lease acquire ... --reason pie`), and that
    would keep the OTHER project out for its own TTL for a session that no longer exists. Best
    effort, outside the project lock; it only ever frees this project's own hold.
    """
    if not evicted:
        return
    try:
        release_machine(agent=str(evicted.get("agent")), project=project)
    except Exception:
        pass


def take_eviction_notice(project: str | None, *, agent: str | None = None) -> dict | None:
    """Pop the "your lease was voided" note for `agent`, if one is pending.

    The evicted holder believes it still has a loaded level and a live PIE session; nothing else
    would tell it. Consumed on read, so the call AFTER the notice proceeds normally.
    """
    agent = agent or default_agent_id()
    _acquire_filelock(project)
    try:
        state = _load(project)
        note = state.get("evicted", {}).pop(agent, None)
        if note is not None:
            _save(project, state)
    finally:
        _release_filelock(project)
    return note


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


def _own_record(state: dict, mode: str, agent: str) -> dict | None:
    """`agent`'s existing hold in `mode`, if any (so a re-acquire can keep its labels)."""
    if mode == "exclusive":
        ex = state.get("exclusive")
        return ex if ex and ex.get("agent") == agent else None
    for h in state.get("shared", []):
        if h.get("agent") == agent:
            return h
    return None


def _waiter_alive(w: dict) -> bool:
    if _now() - float(w.get("heartbeat_at", 0)) > _WAITER_TTL:
        return False            # stopped polling -> the call it belonged to is over
    pid = int(w.get("pid", 0) or 0)
    return _pid_alive(pid) if pid else True


def _evict_stale_waiters(state: dict) -> dict:
    state["waiters"] = [w for w in state.get("waiters", []) if _waiter_alive(w)]
    return state


def _enqueue(state: dict, agent: str, mode: str, reason: str, task: str = "",
             requested_by: str = "") -> None:
    """Register `agent` as waiting, or refresh it if it already is.

    Refreshing deliberately does NOT move it to the back -- arrival order is the whole point.
    `task` / `requested_by` are display labels only (see sanitize_label).
    """
    q = state.setdefault("waiters", [])
    for w in q:
        if w.get("agent") == agent:
            w["heartbeat_at"] = _now()
            w["mode"] = mode
            if task:
                w["task"] = task
            if requested_by:
                w["requested_by"] = requested_by
            return
    q.append({"agent": agent, "mode": mode, "reason": reason, "pid": os.getpid(),
              "since": _now(), "heartbeat_at": _now(), "task": task,
              "requested_by": requested_by})


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
            pid: int | None = None, wait: float = DEFAULT_WAIT_CAP, ttl: int | None = None,
            task: str | None = None, requested_by: str | None = None) -> dict:
    """Block until a `mode` ('exclusive'|'shared') lease is grantable, then take it.

    `pid` is the liveness anchor stored with the lease (default: this process). Pass the pid of a
    long-lived process (e.g. a rebuild script's $PID) to have PID-death auto-reclaim the lease, or
    pass 0 for a cross-call hold that relies on TTL + heartbeat instead.

    Waiting is FIFO. A blocked caller joins the queue in arrival order and only the head may take
    the lease when it frees, so an agent that has been waiting longest cannot be overtaken by one
    that arrives later. The `busy` return carries `queue_position` / `queue_ahead` so a caller
    that gives up can say whether it was next or ninth instead of retrying blind.

    `task` is a short human label for what the agent is doing ("PCG profiling pass"), and
    `requested_by` names the agent a mechanical token acts for (a `rebuild-<pid>` script lease
    taken on behalf of `pcgprof`). Both are sanitised, stored on the holder AND waiter records and
    shown by `uap lease status` and the in-editor overlay; neither affects who is granted what.
    A re-entrant acquire that supplies no label keeps the one already on the record, so the
    rebuild -> relaunch flip does not wipe it.

    Returns {ok, granted, generation, ...} on success, or {ok:False, busy:True, holder} if the
    wait cap elapses. Re-entrant per agent (re-acquiring refreshes, never deadlocks on self).
    """
    if mode not in ("exclusive", "shared"):
        raise ValueError(f"mode must be exclusive|shared, got {mode!r}")
    task = sanitize_label(task)
    requested_by = sanitize_label(requested_by)
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
        evicted = None
        try:
            state, evicted = _refresh(project)
            mine = _holds_already(state, mode, agent)
            if not mine:
                _enqueue(state, agent, mode, reason, task, requested_by)
                _rebuild_first(state, project)      # a fresh rebuild request may jump the queue
            if _grantable(state, mode, agent) and (mine or _turn_ok(state, mode, agent)):
                _dequeue(state, agent)
                # A fresh grant supersedes any "you were evicted" note this agent had pending.
                state.get("evicted", {}).pop(agent, None)
                prev = _own_record(state, mode, agent) or {}
                rec = {"agent": agent, "reason": reason, "pid": pid,
                       "acquired_at": _now(), "heartbeat_at": _now(), "ttl": ttl,
                       "task": task or prev.get("task", ""),
                       "requested_by": requested_by or prev.get("requested_by", "")}
                if mode == "exclusive":
                    state["exclusive"] = rec
                    state["shared"] = [h for h in state["shared"] if h.get("agent") != agent]
                else:
                    state["shared"] = ([h for h in state["shared"] if h.get("agent") != agent]
                                       + [rec])
                _save(project, state)
                return {"ok": True, "granted": True, "agent": agent, "mode": mode,
                        "reason": reason, "task": rec["task"],
                        "requested_by": rec["requested_by"],
                        "generation": state.get("generation", 0),
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
            _after_eviction(project, evicted)
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
        note = state.get("evicted", {}).pop(agent, None)
        _save(project, state)
    finally:
        _release_filelock(project)
    res = {"ok": True, "released": was, "agent": agent}
    if note:
        # Nothing was held because the lease was already voided, not because the caller never
        # had one. Say so instead of a bare `released: false`.
        res["lease_evicted"] = note
    return res


def heartbeat(project: str | None, *, agent: str | None = None,
              task: str | None = None) -> dict:
    """Refresh `agent`'s holds. A non-empty `task` also replaces the label on them, so an agent
    that moves on to new work shows the new work without re-acquiring."""
    agent = agent or default_agent_id()
    task = sanitize_label(task)
    _acquire_filelock(project)
    try:
        state = _load(project)
        touched = 0
        ex = state.get("exclusive")
        if ex and ex.get("agent") == agent:
            ex["heartbeat_at"] = _now()
            if task:
                ex["task"] = task
            touched += 1
        for h in state.get("shared", []):
            if h.get("agent") == agent:
                h["heartbeat_at"] = _now()
                if task:
                    h["task"] = task
                touched += 1
        note = state.get("evicted", {}).pop(agent, None)
        _save(project, state)
    finally:
        _release_filelock(project)
    res = {"ok": True, "refreshed": touched, "agent": agent}
    if note:
        res["lease_evicted"] = note
    return res


def note_activity(project: str | None, *, agent: str, verb: str = "",
                  task: str | None = None) -> None:
    """Record that `agent` just issued `verb`, so a view of the editor can name who is using it
    even when that agent holds no lease (most `exec` / `rc` / `status` traffic never takes one).

    One small entry per agent token in the project's state file, replaced on every call and
    dropped after _ACTIVITY_TTL. Display-only; nothing consults it to decide anything. Callers
    treat a failure as non-fatal.
    """
    task = sanitize_label(task)
    _acquire_filelock(project)
    try:
        state = _load(project)
        now = _now()
        act = {a: n for a, n in (state.get("activity") or {}).items()
               if isinstance(n, dict) and now - float(n.get("at", 0)) < _ACTIVITY_TTL}
        prev = act.get(agent) or {}
        act[agent] = {"verb": sanitize_label(verb), "at": now,
                      "task": task or prev.get("task", "")}
        state["activity"] = act
        _save(project, state)
    finally:
        _release_filelock(project)


def _queue_detail(waiters: list[dict]) -> list[dict]:
    """The queue in arrival order with each waiter's labels, for humans reading status."""
    return [{"agent": str(w.get("agent")), "task": w.get("task", ""),
             "reason": w.get("reason", ""), "requested_by": w.get("requested_by", "")}
            for w in waiters]


def _with_labels(rec: dict | None) -> dict | None:
    """A holder / waiter record guaranteed to carry `task` and `requested_by` keys (records
    written before labels existed lack them) so consumers never have to probe for the key."""
    if not isinstance(rec, dict):
        return rec
    out = dict(rec)
    out.setdefault("task", "")
    out.setdefault("requested_by", "")
    return out


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
    evicted = None
    _acquire_filelock(project)
    try:
        state, evicted = _refresh(project)
        _save(project, state)
    finally:
        _release_filelock(project)
        _after_eviction(project, evicted)
    # `waiters` rides along inside **state; `queue` names them in arrival order so a human
    # reading the output can see who is next without decoding timestamps.
    state = dict(state)
    state["exclusive"] = _with_labels(state.get("exclusive"))
    state["shared"] = [_with_labels(h) for h in state.get("shared", [])]
    state["waiters"] = [_with_labels(w) for w in state.get("waiters", [])]
    return {"ok": True, "project": _scope_key(project),
            "queue": [str(w.get("agent")) for w in state.get("waiters", [])],
            "queue_detail": _queue_detail(state["waiters"]), **state}


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
            evicted = None
            _acquire_filelock(project)
            try:
                state, evicted = _refresh(project)
                _save(project, state)
            finally:
                _release_filelock(project)
                _after_eviction(project, evicted)
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
            "reason": holder.get("reason"), "task": holder.get("task", ""), "holder": holder,
            "waited_seconds": round(max(0.0, wait), 1),
            "queue_position": position, "queue_ahead": ahead or [], "queue_length": queue_len}


def acquire_machine(project: str | None, *, reason: str = "", agent: str | None = None,
                    pid: int | None = None, wait: float = DEFAULT_WAIT_CAP,
                    ttl: int | None = None, task: str | None = None) -> dict:
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
    task = sanitize_label(task)
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
            _enqueue(state, agent, "exclusive", reason, task)
            if holder is None and _turn_ok(state, "exclusive", agent):
                _dequeue(state, agent)
                rec = {"agent": agent, "project": mine, "reason": reason, "pid": pid,
                       "acquired_at": _now(), "heartbeat_at": _now(), "ttl": ttl,
                       "task": task}
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
        waiters = [_with_labels(w) for w in _load(MACHINE_SENTINEL).get("waiters", [])]
    finally:
        _release_filelock(MACHINE_SENTINEL)
    return {"ok": True, "scope": "machine", "enabled": machine_lock_enabled(),
            "holder": _with_labels(holder), "queue": [str(w.get("agent")) for w in waiters],
            "queue_detail": _queue_detail(waiters)}


def wait_if_blocked(project: str | None, *, agent: str | None = None,
                    wait: float = DEFAULT_WAIT_CAP) -> dict:
    """For read-only ops: block while ANOTHER agent holds the exclusive lease, then return.

    Fail-open: any coordination error returns immediately so a lease bug can never brick uap.
    """
    agent = agent or default_agent_id()
    deadline = _now() + max(0.0, wait)
    while True:
        try:
            evicted = None
            _acquire_filelock(project)
            try:
                state, evicted = _refresh(project)
                _save(project, state)
            finally:
                _release_filelock(project)
                _after_eviction(project, evicted)
            ex = state.get("exclusive")
            if ex is None or ex.get("agent") == agent:
                return {"ok": True, "blocked": False}
            holder = ex
        except Exception:
            return {"ok": True, "blocked": False, "note": "coordination unavailable; proceeding"}
        if _now() >= deadline:
            return {"ok": False, "blocked": True, "holder": holder}
        time.sleep(POLL_SECONDS)
