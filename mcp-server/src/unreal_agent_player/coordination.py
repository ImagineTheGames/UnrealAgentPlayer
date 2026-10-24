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

   Running standalone / packaged game clients hold it for their project on their own, found by a
   process scan and tracked by pid, because nothing that starts them goes through a guarded verb
   (see `game_clients` and `_settle_machine`).

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
# Windows sharing violations on the lock/state files are transient (another process has the file
# open for a few milliseconds). Ride them out for up to ~2s before calling it an error.
_SHARING_RETRIES = 100
_SHARING_RETRY_SLEEP = 0.02
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
        except PermissionError:
            # Windows answers O_EXCL with EACCES, not EEXIST, while the previous holder's lockfile
            # is still being deleted (or a scanner has it open). That is "held, for a moment", and
            # it used to escape as a raw PermissionError: measured 2026-10-22 at about one critical
            # section in 200 with eight contending processes. The CLI then treated the machine lock
            # as unavailable and ran `pie start` without it -- two projects in PIE at once.
            if _now() > deadline:
                raise LeaseUnavailable(
                    f"could not create the coordination lock {lp} within "
                    f"{_FILELOCK_TIMEOUT:.0f}s (access denied every time). Refusing to make a "
                    f"lease decision without it.") from None
            time.sleep(0.01)
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
    for attempt in range(_SHARING_RETRIES):
        try:
            if lp.read_text(encoding="utf-8").strip() != mine:
                return                   # somebody broke ours and took it; leave theirs alone
            lp.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            # Another process has the file open for a moment (Windows sharing violation). Giving
            # up here left OUR lock on disk until it went stale 30s later, and every caller in
            # between timed out with LeaseUnavailable.
            time.sleep(_SHARING_RETRY_SLEEP)
        except OSError:
            return


def _blank() -> dict:
    return {"generation": 0, "exclusive": None, "shared": [], "waiters": [], "evicted": {},
            "last_eviction": None, "activity": {}, "pie_session": None, "pending_reap": None,
            "last_reap": None, "unowned_pie": None, "inflight": {}}


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
                    state.setdefault("pie_session", None)
                    state.setdefault("pending_reap", None)
                    state.setdefault("last_reap", None)
                    state.setdefault("unowned_pie", None)
                    state.setdefault("inflight", {})
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
        for attempt in range(_SHARING_RETRIES):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                # WinError 5: a reader has the target open without FILE_SHARE_DELETE. Each editor's
                # overlay reads these files once a second exactly like that (UE's default
                # OpenRead), so this is routine, not exceptional. Retry briefly; only a reader
                # that never lets go surfaces as an error.
                if attempt == _SHARING_RETRIES - 1:
                    raise
                time.sleep(_SHARING_RETRY_SLEEP)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass                     # already renamed into place, or never created


def _holder_alive(h: dict) -> bool:
    # A hold that tracks game-client processes (`pids`) lives exactly as long as they do: TTL does
    # not age it out under a running client, and it frees the moment the last one exits, so a
    # crashed or closed client never leaves the machine locked.
    pids = h.get("pids")
    if pids:
        return any(_pid_alive(int(p)) for p in pids)
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


def _enumerate_processes() -> list[tuple[int, str]]:
    """Every process on the box as (pid, exe file name), from one Toolhelp snapshot.

    Windows only and dependency-free (no PowerShell, no WMI), because lease and machine-lock
    status get polled. Raises on any failure; callers turn that into "unknown".
    """
    if os.name != "nt":
        raise OSError("process enumeration is only implemented on Windows")
    import ctypes
    from ctypes import wintypes

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
    out: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def _enumerate_editors() -> list[dict]:
    """Every `UnrealEditor.exe` process: [{pid, exe, cmdline}] (`cmdline` None if unreadable).

    Raises on any failure; the caller turns that into "unknown".
    """
    from unreal_agent_player import window_capture as wc

    out = []
    for pid, name in _enumerate_processes():
        if name.lower() == "unrealeditor.exe":
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


# --- standalone / packaged game clients -------------------------------------------------------
# A game client is a game window: it takes the GPU, the foreground and (for a test) the keyboard
# exactly as PIE does. But nothing that starts one goes through a guarded uap verb -- School's
# Out's `launch_2p_standalone.ps1` runs `Start-Process UnrealEditor.exe <proj> -game`, the MCP
# server's `game_launch` spawns the same thing, a teammate double-clicks a packaged build -- so
# while they ran, the machine lock read "free" to the other project. On 2026-10-22 two School's
# Out clients were up for a two-client test while Project Broken Wings was granted the machine
# and started PIE.
#
# So the machine lock LOOKS: every live client is, by itself, a machine hold for its project
# (adopted under the agent name GAME_CLIENTS_AGENT, pids tracked, freed when the last one exits).
# Detection is the same dependency-free process snapshot `editor_running` uses. Fail safe: a
# snapshot that fails answers None ("cannot tell"), which changes nothing -- the lock behaves as
# it did before clients were visible to it.
GAME_CLIENTS_AGENT = "game-clients"
GAME_CLIENTS_REASON = "game-clients"
# A `-game` process that has no window takes no foreground: a dedicated server or a headless run.
_WINDOWLESS_FLAGS = ("-server", "-nullrhi")
_GAME_CLIENT_PROBE_OFF = {"0", "false", "no", "off"}
_GAME_CLIENT_CACHE_SECONDS = 1.0
_GAME_CLIENT_CACHE: list = []          # [(at, result)] -- one entry, cleared by tests


def _known_projects() -> set[str]:
    """Project keys this machine has coordinated (one lease file each). A packaged build's exe
    name is matched only against these, so an unrelated `Foo-Win64-Shipping.exe` is never taken
    for a project."""
    out = set()
    for f in _leases_dir().glob("*.json"):
        stem = f.stem
        if stem and not stem.startswith("_") and "." not in stem:
            out.add(stem)
    return out


def game_clients() -> dict[str, list[int]] | None:
    """Live game-client processes by project key: {"schoolsout": [21716, 59372]}.

    Counts `UnrealEditor*.exe <project>.uproject -game` (what launch_2p_standalone.ps1 and MCP
    `game_launch` start) and a packaged `<Project>[-<Platform>-<Config>].exe` under a `Binaries`
    folder, for a project this machine knows. Never the editor itself, and never a windowless
    `-server` / `-nullrhi` process. None when the process list cannot be read, the host is not
    Windows, or `$UAP_GAME_CLIENT_PROBE=0`.
    """
    raw = os.environ.get("UAP_GAME_CLIENT_PROBE")
    if raw is not None and raw.strip().lower() in _GAME_CLIENT_PROBE_OFF:
        return None
    if _GAME_CLIENT_CACHE and _now() - _GAME_CLIENT_CACHE[0][0] < _GAME_CLIENT_CACHE_SECONDS:
        return _GAME_CLIENT_CACHE[0][1]
    try:
        from unreal_agent_player import window_capture as wc

        known = _known_projects()
        found: dict[str, list[int]] = {}
        for pid, name in _enumerate_processes():
            lname = name.lower()
            stem = os.path.splitext(lname)[0]
            editor_binary = stem.startswith("unrealeditor")
            if not editor_binary and wc._PACKAGED_SUFFIX_RE.sub("", stem) not in known:
                continue
            exe, cmdline = wc._process_info(pid)
            if editor_binary:
                if not cmdline:
                    continue
                info = wc.parse_cmdline(cmdline)
                if not info.get("game_client") or not info.get("project"):
                    continue
                key = _safe_project(info["project"])
            else:
                if not exe or "\\binaries\\" not in exe.replace("/", "\\").lower():
                    continue
                key = wc._PACKAGED_SUFFIX_RE.sub("", stem)
            if cmdline and any(wc._has_flag(cmdline, f) for f in _WINDOWLESS_FLAGS):
                continue
            found.setdefault(key, []).append(int(pid))
    except Exception:
        return None
    result = {k: sorted(v) for k, v in found.items()}
    _GAME_CLIENT_CACHE[:] = [(_now(), result)]
    return result


# The seam the machine lock goes through (tests and the suite-wide conftest replace it).
_game_client_probe: Callable[[], dict[str, list[int]] | None] = game_clients


def _live_game_clients() -> dict[str, list[int]] | None:
    """The probe, never raising. Call WITHOUT the filelock (it enumerates processes)."""
    try:
        res = _game_client_probe()
    except Exception:
        return None
    if res is None:
        return None
    return {_safe_project(k): sorted(int(p) for p in v) for k, v in res.items() if v}


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
    state = _load(project)
    aged_out = _ttl_expired_exclusive(state, project)
    state = _evict_stale(state)
    if aged_out is not None and state.get("exclusive") is None:
        _note_pending_reap(state, aged_out)
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
        # A holder that aged out may have left its PIE running: stop that BEFORE the editor is
        # handed to the next agent, not after it has started driving a session that is not its own.
        reap_stale_pie(project)
        _acquire_filelock(project)
        evicted = None
        try:
            state, evicted = _refresh(project)
            mine = _holds_already(state, mode, agent)
            if not mine:
                _enqueue(state, agent, mode, reason, task, requested_by)
                _rebuild_first(state, project)      # a fresh rebuild request may jump the queue
            if _grantable(state, mode, agent) and (mine or _turn_ok(state, mode, agent)) \
                    and (mine or _reaper_holder(state) is None):
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
            holder = (state.get("exclusive") or (state.get("shared") or [None])[0]
                      or _reaper_holder(state))
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
    # `was_exclusive` tells the caller whether this agent owned the project's editor turn, which
    # is what entitles it to hand back the project's machine hold as well (cli `lease release`).
    res = {"ok": True, "released": was, "was_exclusive": bool(ex and ex.get("agent") == agent),
           "agent": agent}
    if note:
        # Nothing was held because the lease was already voided, not because the caller never
        # had one. Say so instead of a bare `released: false`.
        res["lease_evicted"] = note
    return res


def holds_exclusive(project: str | None, *, agent: str) -> bool:
    """Does `agent` hold `project`'s exclusive lease right now? (Read-only.)"""
    _acquire_filelock(project)
    try:
        ex = _evict_stale(_load(project)).get("exclusive")
    finally:
        _release_filelock(project)
    return bool(ex) and ex.get("agent") == agent


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
    reap_stale_pie(project)          # `lease status` is a reaper run too (opportunistic)
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
# Stale PIE reaper
#
# A lease ageing out freed the LEASE and nothing else. 2026-10-23: agent `gpufoliage` was in PBW
# PIE when it hit its turn limit and stopped without handing off. Its `pie` lease aged out on the
# 600s TTL, but PIE kept running in the PBW editor for about 1.5 hours, holding the machine-wide
# lock (a live PIE session keeps a stale machine hold alive, see `_live_pie_holds`), stealing focus
# from the other project and burning the GPU, until a human noticed.
#
# So a session uap started is stopped once nobody is driving it:
#   * `lease_ttl`: a non-rebuild exclusive lease aged out on its TTL (the holder made no uap call
#     for that long) and the editor is still in PIE;
#   * `orphan`: nobody holds the lease, PIE is still running, and no uap call has touched this
#     project's editor for the idle window (an agent that ran `pie start` without a lease).
# Rules:
#   * NEVER a human's PIE. Only a session uap itself started (`pie_session`, written by
#     `note_pie_started`) is ever stopped, and the running session must be provably THAT one: its
#     start time (now - GetPIEElapsedSeconds) has to fall inside the window the uap start was
#     made in. A session uap cannot identify is reported (`unowned_pie`) and left alone.
#   * Any uap call that reads lease state may run it (it is opportunistic); two racing callers are
#     serialized by a claim taken under the filelock, so exactly one of them stops PIE.
#   * The probes are network round trips to the editor, so they never run inside the filelock.
#   * Never while a rebuild holds the project, a live holder owns the lease, or another uap call
#     is mid-flight against this editor (`inflight`).
#   * $UAP_PIE_REAPER=0 turns it off.
# The CLI installs the two seams below (an RC-backed probe and its confirmed `pie stop`). With
# either missing (the MCP server, the test suite by default) the reaper does nothing.
# ----------------------------------------------------------------------------------------------
EVICT_STALE_PIE = "stale_pie_reaped"
REAP_FAILED = "stale_pie_reap_failed"
REAPER_AGENT = "uap-reaper"
_PIE_REAPER_OFF = {"0", "false", "no", "off"}
_REAP_CHECK_INTERVAL = 15.0       # at most one probe per project per this many seconds
_REAP_CLAIM_TTL = 120.0           # a claim older than this belonged to a reaper that died
_SESSION_SLACK = 5.0              # clock/RC slack before the uap start request
_SESSION_CONFIRMED_SLACK = 30.0   # play world confirmed live -> PostPIEStarted, at most this later
_SESSION_UNCONFIRMED_WINDOW = 300.0   # a start nobody watched come up (`--no-wait`, rc, exec)
_PENDING_REAP_MAX_AGE = 3600.0    # an eviction whose editor never answered stops being chased
_INFLIGHT_MAX_AGE = 7200.0        # a call record this old belongs to a process that was killed

# "Is PIE running in this project's editor, and since when?" ->
#   {"in_progress": True/False/None, "started_at": unix seconds or None}
_pie_session_probe: Callable[[str], dict | None] | None = None
# Stop PIE in this project's editor and confirm it: -> {"stopped": bool, ...}
_pie_stopper: Callable[[str], dict] | None = None


def pie_reaper_enabled() -> bool:
    """False disables the stale-PIE reaper entirely ($UAP_PIE_REAPER=0)."""
    raw = os.environ.get("UAP_PIE_REAPER")
    return not (raw is not None and raw.strip().lower() in _PIE_REAPER_OFF)


def reap_idle_seconds() -> float:
    """How long a uap-started PIE with NO lease holder may go without a uap call before it is
    reaped. Same as the pie lease TTL by default; $UAP_PIE_REAP_IDLE overrides."""
    raw = os.environ.get("UAP_PIE_REAP_IDLE")
    try:
        return max(0.0, float(raw)) if raw and raw.strip() else float(_TTL_BY_REASON["pie"])
    except ValueError:
        return float(_TTL_BY_REASON["pie"])


def _ttl_expired_exclusive(state: dict, project: str | None) -> dict | None:
    """The exclusive holder about to be evicted for INACTIVITY (heartbeat past its TTL), if any.

    Not a rebuild/relaunch (the editor being down is its job), not a pid-tracked client hold, and
    project scopes only. Call before `_evict_stale`.
    """
    ex = state.get("exclusive")
    if not ex or isinstance(project, _MachineScope) or ex.get("pids"):
        return None
    if _is_rebuild_reason(ex.get("reason")):
        return None
    if _now() - float(ex.get("heartbeat_at", 0)) <= float(ex.get("ttl", _DEFAULT_TTL)):
        return None
    return ex


def _note_pending_reap(state: dict, ex: dict) -> None:
    """Remember that `ex` aged out, so whichever caller runs the reaper next checks its PIE.

    Recorded in the state rather than acted on here because the eviction happens inside the
    filelock, where the editor must not be probed, and the caller that evicts is not always one
    that can run the reaper.
    """
    state["pending_reap"] = {"agent": ex.get("agent"), "held_reason": ex.get("reason"),
                             "task": ex.get("task", ""), "heartbeat_at": ex.get("heartbeat_at"),
                             "ttl": ex.get("ttl"), "evicted_at": _now(), "attempted_at": 0.0}


def _other_inflight(state: dict) -> list[str]:
    """Pids of OTHER uap processes currently running a verb against this editor. Prunes dead ones."""
    now = _now()
    me = str(os.getpid())
    live: dict = {}
    others: list[str] = []
    for pid, rec in (state.get("inflight") or {}).items():
        try:
            at = float((rec or {}).get("at", 0))
            alive = now - at < _INFLIGHT_MAX_AGE and _pid_alive(int(pid))
        except (TypeError, ValueError):
            alive = False
        if alive:
            live[pid] = rec
            if pid != me:
                others.append(pid)
    state["inflight"] = live
    return others


def _reap_claim_fresh(state: dict) -> dict | None:
    claim = state.get("reaping")
    if claim and _now() - float(claim.get("at", 0)) < _REAP_CLAIM_TTL:
        return claim
    return None


def _reaper_holder(state: dict) -> dict | None:
    """A pseudo-holder while a reap is in progress, so nobody is granted an editor mid-stop."""
    claim = _reap_claim_fresh(state)
    if not claim or claim.get("pid") == os.getpid():
        return None
    return {"agent": REAPER_AGENT, "reason": "stale_pie_reap", "task": "stopping a stale PIE",
            "acquired_at": claim.get("at"), "heartbeat_at": claim.get("at"),
            "ttl": _REAP_CLAIM_TTL, "requested_by": ""}


def _reap_cause(state: dict, project: str | None) -> str | None:
    """Why this project's PIE should be checked right now ("lease_ttl" / "orphan"), or None.

    Cheap and local (no probe): call with the filelock held. May drop a `pending_reap` that a new
    holder has made moot.
    """
    if not pie_reaper_enabled() or _pie_session_probe is None or _pie_stopper is None:
        return None
    if isinstance(project, _MachineScope) or _scope_key(project) == "_default":
        return None
    if _mid_rebuild(state) or _reap_claim_fresh(state):
        return None
    now = _now()
    pending = state.get("pending_reap")
    if pending and now - float(pending.get("evicted_at", 0)) > _PENDING_REAP_MAX_AGE:
        state["pending_reap"] = pending = None
    if state.get("exclusive"):
        # A live holder owns the editor now; the session it is driving is its business.
        if pending:
            state["pending_reap"] = None
        return None
    if _other_inflight(state):
        return None
    if pending:
        if now - float(pending.get("attempted_at", 0)) < _REAP_CHECK_INTERVAL:
            return None
        return "lease_ttl"
    sess = state.get("pie_session")
    if not sess:
        return None
    if now - float(state.get("reap_checked_at", 0) or 0) < _REAP_CHECK_INTERVAL:
        return None
    last = max(float(state.get("last_uap_at", 0) or 0), float(sess.get("requested_at", 0) or 0),
               float(sess.get("confirmed_at", 0) or 0))
    return "orphan" if now - last > reap_idle_seconds() else None


def _session_is_ours(sess: dict | None, started_at) -> bool:
    """Is the PIE session that started at `started_at` the one uap started (`sess`)?"""
    if not sess or started_at is None:
        return False
    req = float(sess.get("requested_at", 0) or 0)
    if not req:
        return False
    conf = sess.get("confirmed_at")
    hi = (float(conf) + _SESSION_CONFIRMED_SLACK) if conf else (req + _SESSION_UNCONFIRMED_WINDOW)
    return req - _SESSION_SLACK <= float(started_at) <= hi


def _reap_log(project: str | None, rec: dict) -> None:
    """Append one line per reaper decision to `<leases>/reaper.log`. Best effort."""
    try:
        with open(_leases_dir() / "reaper.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"project": _scope_key(project), **rec}) + "\n")
    except OSError:
        pass


def reap_stale_pie(project: str | None) -> dict | None:
    """Stop a PIE session uap started that nobody is driving any more. None when nothing to do.

    Safe to call from anywhere that is NOT holding a filelock; it is cheap when there is nothing
    to do (one locked read). Never raises. See the section comment above for the rules.
    """
    if not pie_reaper_enabled() or _pie_session_probe is None or _pie_stopper is None:
        return None
    try:
        return _reap(project)
    except Exception as exc:
        return {"reaped": False, "error": f"{type(exc).__name__}: {exc}"}


def _reap(project: str | None) -> dict | None:
    token = uuid.uuid4().hex[:12]
    evicted = None
    _acquire_filelock(project)
    try:
        state, evicted = _refresh(project)
        cause = _reap_cause(state, project)
        if cause:
            state["reaping"] = {"token": token, "pid": os.getpid(), "at": _now(), "cause": cause}
            if cause == "lease_ttl":
                state["pending_reap"]["attempted_at"] = _now()
            else:
                state["reap_checked_at"] = _now()
        sess = dict(state.get("pie_session") or {}) or None
        pending = dict(state.get("pending_reap") or {}) or None
        idle_since = float(state.get("last_uap_at", 0) or 0)
        _save(project, state)
    finally:
        _release_filelock(project)
        _after_eviction(project, evicted)
    if not cause:
        return None

    target = str((sess or {}).get("target") or project or "")
    outcome: dict = {"cause": cause}
    try:
        info = _pie_session_probe(target) or {}
    except Exception as exc:
        info = {"in_progress": None, "error": f"{type(exc).__name__}: {exc}"}
    live = info.get("in_progress")
    started_at = info.get("started_at")
    outcome.update({"in_progress": live, "session_started_at": started_at})
    stop: dict | None = None             # None: no stop was attempted
    owned = live is True and _session_is_ours(sess, started_at)
    if owned:
        if _claim_still_ours(project, token, sess):
            try:
                stop = _pie_stopper(target) or {}
            except Exception as exc:
                stop = {"stopped": False, "error": f"{type(exc).__name__}: {exc}"}
            outcome["stopped"] = bool(stop.get("stopped"))
        else:
            # Somebody took the lease or started a new session while we probed: theirs now.
            live = "superseded"

    rec = _record_reap(project, token, cause, sess, pending, live, started_at, stop, idle_since,
                       info)
    if rec and rec.get("why") == EVICT_STALE_PIE:
        _release_machine_after_reap(project, rec, sess, pending)
        _reap_log(project, rec)
    elif rec:
        _reap_log(project, rec)
    return rec or {"reaped": False, **outcome}


def _claim_still_ours(project: str | None, token: str, sess: dict | None) -> bool:
    """Last look before stopping: our claim stands and nobody started a new session or took the
    lease since we probed."""
    _acquire_filelock(project)
    try:
        state = _load(project)
        claim = state.get("reaping") or {}
        cur = state.get("pie_session") or {}
        return (claim.get("token") == token and not state.get("exclusive")
                and cur.get("requested_at") == (sess or {}).get("requested_at"))
    finally:
        _release_filelock(project)


def _record_reap(project, token, cause, sess, pending, live, started_at, stop, idle_since,
                 info) -> dict | None:
    """Write what the reaper found and did, and release the claim. Returns the record for a reap
    (or a failed reap / an unowned session), None for "nothing was running"."""
    now = _now()
    key = _scope_key(project)
    rec: dict | None = None
    _acquire_filelock(project)
    try:
        state = _load(project)
        if (state.get("reaping") or {}).get("token") == token:
            state.pop("reaping", None)
        same_session = (state.get("pie_session") or {}).get("requested_at") == \
            (sess or {}).get("requested_at")
        if live == "superseded":
            pass                         # only the claim is released
        elif live is False:
            # Nothing is running: whatever uap started is over (stopped by hand, crashed ...).
            if same_session:
                state["pie_session"] = None
            state["pending_reap"] = None
            state["unowned_pie"] = None
        elif live is None:
            # The editor did not answer. If it is verifiably gone, so is any session in it.
            try:
                gone = _editor_probe(project) is False
            except Exception:
                gone = False
            if gone:
                if same_session:
                    state["pie_session"] = None
                state["pending_reap"] = None
        elif stop is not None and stop.get("stopped"):
            agent = (pending or {}).get("agent") or (sess or {}).get("agent")
            held = (pending or {}).get("held_reason") or (sess or {}).get("via") or "pie"
            idle = now - max(idle_since, float((sess or {}).get("requested_at", 0) or 0))
            if cause == "lease_ttl":
                why_text = (f"its '{held}' lease aged out after {int(float((pending or {}).get('ttl') or 0))}s "
                            f"with no uap call")
            else:
                why_text = f"no lease was held and no uap call touched the editor for {int(idle)}s"
            rec = {"why": EVICT_STALE_PIE, "cause": cause, "agent": agent, "held_reason": held,
                   "pie_started_by": (sess or {}).get("agent"), "project": key,
                   "evicted_at": now, "reaped_at": now, "reaped_by": default_agent_id(),
                   "session_started_at": started_at,
                   "acquired_at": (sess or {}).get("requested_at"),
                   "message": (f"lease_evicted: {EVICT_STALE_PIE} -- the {key} PIE session uap "
                               f"started for {agent} was still running after {why_text}, so it "
                               f"was stopped (confirmed) and the machine-wide lock given back. "
                               f"Nothing you were doing in that session survives. Re-acquire "
                               f"(`uap lease acquire exclusive --reason pie --agent <token>`) and "
                               f"`uap pie start` again.")}
            state["pie_session"] = None
            state["pending_reap"] = None
            state["unowned_pie"] = None
            state["last_reap"] = rec
            state["last_eviction"] = rec
            notes = {a: n for a, n in state.get("evicted", {}).items()
                     if now - float(n.get("evicted_at", 0)) < _EVICTION_NOTICE_TTL}
            for who in {agent, (sess or {}).get("agent")}:
                if who:
                    notes[str(who)] = rec
            state["evicted"] = notes
        elif stop is not None:
            rec = {"why": REAP_FAILED, "cause": cause, "agent": (sess or {}).get("agent"),
                   "project": key, "at": now, "error": stop.get("error", "stop not confirmed"),
                   "message": (f"the stale {key} PIE session could not be stopped "
                               f"({stop.get('error', 'stop not confirmed')}); it will be retried. "
                               f"Stop it in the editor by hand if it persists.")}
            state["last_reap"] = rec
        else:
            # PIE is running, but not a session uap can prove it started: a human's (or the MCP
            # server's, or one an unrecognised API started). Never stopped -- only reported.
            rec = {"why": "unowned_pie_left_running", "cause": cause, "project": key,
                   "seen_at": now, "session_started_at": started_at,
                   "evicted_agent": (pending or {}).get("agent"),
                   "message": (f"PIE is running in the {key} editor but uap has no record of "
                               f"starting this session (a human, the MCP server or an "
                               f"unrecognised API started it, or this editor's plugin cannot "
                               f"report the session start), so the reaper did NOT stop it.")}
            if info.get("error"):
                rec["probe_error"] = info["error"]
            state["unowned_pie"] = rec
            state["pending_reap"] = None
            if same_session and started_at is not None:
                state["pie_session"] = None      # our record is provably not this session
        _save(project, state)
    finally:
        _release_filelock(project)
    return rec


def _release_machine_after_reap(project, rec: dict, sess: dict | None,
                                pending: dict | None) -> None:
    """The reaped session was what held the machine for this project: give it back. Running
    standalone clients of the project are never killed (uap did not necessarily launch them);
    they keep the machine on their own (`_settle_machine`) and are named in the record."""
    try:
        res = release_machine(agent=str((pending or sess or {}).get("agent") or REAPER_AGENT),
                              project=project, project_wide=True)
        rec["machine_released"] = bool(res.get("released"))
    except Exception as exc:
        rec["machine_released"] = False
        rec["machine_error"] = f"{type(exc).__name__}: {exc}"
    clients = (_live_game_clients() or {}).get(_safe_project(project))
    if clients:
        rec["game_clients_left_running"] = clients
        try:
            _acquire_filelock(project)
            try:
                state = _load(project)
                if (state.get("last_reap") or {}).get("reaped_at") == rec.get("reaped_at"):
                    state["last_reap"]["game_clients_left_running"] = clients
                    _save(project, state)
            finally:
                _release_filelock(project)
        except CoordinationError:
            pass


def note_pie_started(project: str | None, *, agent: str, requested_at: float,
                     confirmed_at: float | None = None, via: str = "pie start") -> None:
    """Record that uap started a PIE session in `project` (the reaper only ever stops these).

    `requested_at` is when the start was asked for; `confirmed_at` when the play world was seen
    live, if it was. Together they bound the session start the reaper matches against.
    """
    _acquire_filelock(project)
    try:
        state = _load(project)
        prev = state.get("pie_session") or {}
        if prev.get("requested_at"):
            # A second start while the first session is (as far as uap knows) still running is a
            # no-op in the editor: the session that keeps running is the FIRST one. Keep its
            # window open, or the running session would no longer match and could never be reaped.
            requested_at = min(float(requested_at), float(prev["requested_at"]))
        state["pie_session"] = {"agent": agent, "target": str(project or ""), "via": via,
                                "requested_at": float(requested_at),
                                "confirmed_at": float(confirmed_at) if confirmed_at else None,
                                "pid": os.getpid()}
        state["unowned_pie"] = None
        state["last_uap_at"] = _now()
        _save(project, state)
    finally:
        _release_filelock(project)


def note_pie_stopped(project: str | None) -> None:
    """A confirmed stop: nothing uap started is running in `project` any more."""
    _acquire_filelock(project)
    try:
        state = _load(project)
        if state.get("pie_session") or state.get("pending_reap") or state.get("unowned_pie"):
            state["pie_session"] = None
            state["pending_reap"] = None
            state["unowned_pie"] = None
            _save(project, state)
    finally:
        _release_filelock(project)


def begin_call(project: str | None, verb: str = "") -> None:
    """A uap verb is about to run against this editor: it counts as activity until `end_call`.

    The reaper never stops PIE under a call in flight (a long `sample`, `input hold` or `exec`
    makes no further uap calls while it runs, yet is plainly driving the session).
    """
    _acquire_filelock(project)
    try:
        state = _load(project)
        state.setdefault("inflight", {})[str(os.getpid())] = {"verb": sanitize_label(verb),
                                                              "at": _now()}
        state["last_uap_at"] = _now()
        _save(project, state)
    finally:
        _release_filelock(project)


def end_call(project: str | None) -> None:
    _acquire_filelock(project)
    try:
        state = _load(project)
        (state.get("inflight") or {}).pop(str(os.getpid()), None)
        state["last_uap_at"] = _now()
        _save(project, state)
    finally:
        _release_filelock(project)


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


# "Is this project's editor in PIE right now?" -> True / False / None (cannot tell). The CLI installs
# an RC-backed probe; with none installed (or None returned) a stale hold ages out on its TTL
# exactly as before.
#
# Why it exists: a sticky `pie start` hold is refreshed only by the holder's FOREGROUND verbs. An
# agent driving a long PIE session through exec / rc alone never touches it, so after MACHINE_TTL
# it aged out while the session was still running and the other project was granted the machine.
# The PIE session is the thing being held, so the session itself is what keeps the hold alive.
_machine_pie_probe: Callable[[str], bool | None] | None = None


def _hold_key(h: dict) -> tuple:
    return (h.get("agent"), h.get("project"), h.get("acquired_at"))


def _live_pie_holds() -> set:
    """Keys of stale machine holds whose project's editor is still in PIE. Call WITHOUT the lock.

    The probe is a network round trip to an editor, so it must never run inside the filelock
    (every other agent's coordination call would queue behind it). It reads the state unlocked,
    which is safe: `_save` is atomic, and the locked pass that follows only acts on a hold whose
    identity still matches the one probed.
    """
    probe = _machine_pie_probe
    if probe is None:
        return set()
    try:
        ex = _load(MACHINE_SENTINEL).get("exclusive")
    except Exception:
        return set()
    if not ex or _holder_alive(ex):
        return set()
    target = str(ex.get("target") or ex.get("project") or "")
    try:
        live = probe(target)
    except Exception:
        return set()
    if live is True and target:
        # A live session keeps the hold -- unless it is a session uap started that nobody is
        # driving any more. Then it is stopped instead (2026-10-23: one held the machine, and the
        # other project, for 1.5 hours). This is the path the OTHER project's calls reach, which
        # matters: the stale project's own agents are, by definition, no longer calling.
        reaped = reap_stale_pie(target)
        if reaped and reaped.get("why") == EVICT_STALE_PIE:
            return set()
    return {_hold_key(ex)} if live is True else set()


def _adopted_record(project: str, pids: list[int]) -> dict:
    """The machine hold that running game clients ARE, when nobody holds the lock for them."""
    pids = sorted(int(p) for p in pids)
    return {"agent": GAME_CLIENTS_AGENT, "project": project, "target": project,
            "reason": GAME_CLIENTS_REASON, "pid": 0, "pids": pids, "adopted": True,
            "acquired_at": _now(), "heartbeat_at": _now(), "ttl": MACHINE_TTL,
            "task": f"standalone game client(s) of {project} running (pids "
                    f"{', '.join(str(p) for p in pids)})"}


def _settle_machine(state: dict, live: set,
                    clients: dict[str, list[int]] | None = None) -> dict:
    """Bring the machine record up to date with what is actually running, then evict the dead.

    * a stale hold the PIE probe found still in PIE is refreshed (`live`);
    * a hold whose project has live game clients is kept alive by them, and names them;
    * a hold that exists only because of game clients goes when they do;
    * a FREE lock with game clients running is taken by them, for their project.
    `clients` None means "cannot tell": nothing is adopted or dropped on its account.
    """
    ex = state.get("exclusive")
    if ex and _hold_key(ex) in live and not _holder_alive(ex):
        ex["heartbeat_at"] = _now()
        ex["pid"] = 0                # the session, not the process that started it, is the anchor
        ex["kept_by_live_pie"] = True
    if ex and clients is not None:
        running = clients.get(ex.get("project"))
        if running:
            ex["clients"] = running
            if ex.get("adopted"):
                ex["pids"] = running
                ex["task"] = _adopted_record(str(ex.get("project")), running)["task"]
            elif int(ex.get("pid", 0) or 0) == 0 and not ex.get("pids"):
                # A sticky hold (a `2p` lease, a PIE session) whose project's clients are up:
                # the clients are the session, so they keep it, however its agent drives them.
                ex["heartbeat_at"] = _now()
                ex["kept_by_game_clients"] = True
        elif ex.get("adopted"):
            state["exclusive"] = None
        else:
            ex.pop("clients", None)
    state = _evict_stale_waiters(_evict_stale(state))
    if state.get("exclusive") is None and clients:
        # Deterministic when two projects' clients are both up (itself a conflict, reported by
        # machine_status): the first by name holds the machine, the other is listed.
        proj = min(clients)
        state["exclusive"] = _adopted_record(proj, clients[proj])
    return state


def _machine_holder(live: set | frozenset = frozenset(),
                    clients: dict[str, list[int]] | None = None) -> dict | None:
    state = _settle_machine(_load(MACHINE_SENTINEL), set(live), clients)
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

    NEVER RAISES for a coordination failure. A poll that cannot read or write the lock is a poll
    that was not granted: it is retried, and if the failures outlast the wait the caller gets
    `busy` with `coordination_unsafe: True`. It used to raise, and the CLI ran the verb on any
    exception, so one transient Windows sharing violation in the middle of a wait turned "another
    project is in PIE" into "go ahead": School's Out started PIE under Project Broken Wings'
    live session (2026-10-22).
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
    errors = 0
    last_error = ""
    while True:
        try:
            granted, holder, position, ahead, queue_len = _machine_poll(
                project, mine, agent=agent, reason=reason, pid=pid, ttl=ttl, task=task)
            if granted is not None:
                return granted
            last_error = ""
        except (OSError, CoordinationError) as exc:
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"
            if errors <= _MACHINE_QUICK_RETRIES:
                time.sleep(_SHARING_RETRY_SLEEP)
                continue                 # a sharing violation clears in milliseconds
        if _now() >= deadline:
            try:
                _acquire_filelock(MACHINE_SENTINEL)
                try:
                    state = _load(MACHINE_SENTINEL)
                    _dequeue(state, agent)
                    _save(MACHINE_SENTINEL, state)
                finally:
                    _release_filelock(MACHINE_SENTINEL)
            except (OSError, CoordinationError):
                pass                     # the waiter entry ages out on its own (_WAITER_TTL)
            busy = _machine_busy(holder, agent, wait, position=position, ahead=ahead,
                                 queue_len=queue_len)
            if last_error:
                busy["coordination_unsafe"] = True
                busy["error"] = last_error
            return busy
        time.sleep(POLL_SECONDS)


# How many back-to-back retries a poll gets for a coordination error before it counts as a missed
# turn. Lets `wait=0` callers ride out a sharing violation instead of reporting busy for it.
_MACHINE_QUICK_RETRIES = 5


def _machine_poll(project, mine: str, *, agent: str, reason: str, pid: int, ttl: int,
                  task: str) -> tuple:
    """One locked look at the machine lock: (grant-or-None, holder, position, ahead, queue_len)."""
    live = _live_pie_holds()
    clients = _live_game_clients()
    _acquire_filelock(MACHINE_SENTINEL)
    try:
        state = _settle_machine(_load(MACHINE_SENTINEL), live, clients)
        holder = state.get("exclusive")
        if holder is not None and holder.get("project") == mine and holder.get("adopted") \
                and pid == 0:
            # A sticky hold (`pie start`, `lease acquire --reason 2p`) in the project whose
            # clients already hold the machine: the agent takes the hold over, so its own session
            # does not lose the machine the moment the clients exit.
            _dequeue(state, agent)
            rec = {"agent": agent, "project": mine, "target": str(project or ""),
                   "reason": reason, "pid": 0, "acquired_at": _now(), "heartbeat_at": _now(),
                   "ttl": ttl, "task": task, "clients": list(holder.get("pids") or [])}
            state["exclusive"] = rec
            _save(MACHINE_SENTINEL, state)
            return ({"ok": True, "granted": True, "acquired": True, "agent": agent,
                     "scope": "machine", "project": mine, "reason": reason, "holder": rec,
                     "took_over_from": GAME_CLIENTS_AGENT},
                    rec, 0, [], 0)
        if holder is not None and holder.get("project") == mine:
            holder["heartbeat_at"] = _now()
            _dequeue(state, agent)
            _save(MACHINE_SENTINEL, state)
            return ({"ok": True, "granted": True, "acquired": False, "agent": agent,
                     "scope": "machine", "project": mine, "holder": holder},
                    holder, 0, [], 0)
        _enqueue(state, agent, "exclusive", reason, task)
        if holder is None and _turn_ok(state, "exclusive", agent):
            _dequeue(state, agent)
            # `target` keeps the caller's own spelling of the project, which is what the live-PIE
            # probe needs to address that project's editor.
            rec = {"agent": agent, "project": mine, "target": str(project or ""),
                   "reason": reason, "pid": pid, "acquired_at": _now(), "heartbeat_at": _now(),
                   "ttl": ttl, "task": task}
            state["exclusive"] = rec
            _save(MACHINE_SENTINEL, state)
            return ({"ok": True, "granted": True, "acquired": True, "agent": agent,
                     "scope": "machine", "project": mine, "reason": reason, "holder": rec},
                    rec, 0, [], 0)
        position = _queue_position(state, agent)
        ahead = _queue_ahead(state, agent)
        queue_len = len(state.get("waiters", []))
        _save(MACHINE_SENTINEL, state)
        return None, holder, position, ahead, queue_len
    finally:
        _release_filelock(MACHINE_SENTINEL)


def release_machine(*, agent: str | None = None, project: str | None = None,
                    force: bool = False, project_wide: bool = True) -> dict:
    """Free the machine lock if it is ours.

    With `project`, only a hold of THAT project can be freed, and then:
      * `project_wide=True` (default) frees it whichever agent took it. `uap pie stop` legitimately
        frees the hold a DIFFERENT agent on the same project took with `pie start`, because the
        thing being held (that project's PIE session) is genuinely over;
      * `project_wide=False` frees it only when `agent` is the one that took it.
    Without `project`, only `agent`'s own hold is freed.

    It used to match agent OR project, so an agent token that happened to exist in BOTH projects
    (agents reuse short names) let one project's `pie stop` free the other project's live session.

    `force=True` breaks a hold belonging to anyone -- the break-glass for a session that died
    without releasing and has not yet aged out.
    """
    agent = agent or default_agent_id()
    mine = _safe_project(project) if project is not None else None
    holder = None
    released = False
    _acquire_filelock(MACHINE_SENTINEL)
    try:
        state = _load(MACHINE_SENTINEL)
        holder = state.get("exclusive")
        if holder:
            same_agent = holder.get("agent") == agent
            if force:
                ok = True
            elif mine is None:
                ok = same_agent
            else:
                ok = holder.get("project") == mine and (project_wide or same_agent)
            if ok:
                state["exclusive"] = None
                released = True
                _dequeue(state, agent)
                _save(MACHINE_SENTINEL, state)
    finally:
        _release_filelock(MACHINE_SENTINEL)
    return {"ok": True, "released": released, "scope": "machine", "agent": agent,
            "was_held_by": holder if released else None, "holder": None if released else holder}


def machine_status() -> dict:
    """Who owns this machine's foreground right now (stale holders evicted first, unless their
    project's editor is still in PIE -- see `_machine_pie_probe`)."""
    live = _live_pie_holds()
    clients = _live_game_clients()
    _acquire_filelock(MACHINE_SENTINEL)
    try:
        holder = _machine_holder(live, clients)
        waiters = [_with_labels(w) for w in _load(MACHINE_SENTINEL).get("waiters", [])]
    finally:
        _release_filelock(MACHINE_SENTINEL)
    # Game clients of a project that does NOT hold the machine were started outside uap while
    # another project had it (a launcher script, a double-click). uap cannot undo the launch, so
    # it names them: this is the state "two projects running games at once" looks like.
    hp = (holder or {}).get("project")
    conflicts = {k: v for k, v in (clients or {}).items() if k != hp}
    return {"ok": True, "scope": "machine", "enabled": machine_lock_enabled(),
            "holder": _with_labels(holder), "queue": [str(w.get("agent")) for w in waiters],
            "queue_detail": _queue_detail(waiters), "game_clients": clients,
            "conflicts": conflicts}


def heartbeat_machine(project: str | None, *, agent: str | None = None) -> bool:
    """Refresh `agent`'s own machine hold for `project` (the `lease heartbeat` half that keeps a
    long `2p` / PIE hold alive while it is driven through verbs that never touch the lock)."""
    agent = agent or default_agent_id()
    mine = _safe_project(project)
    _acquire_filelock(MACHINE_SENTINEL)
    try:
        state = _load(MACHINE_SENTINEL)
        ex = state.get("exclusive")
        if not ex or ex.get("agent") != agent or ex.get("project") != mine:
            return False
        ex["heartbeat_at"] = _now()
        _save(MACHINE_SENTINEL, state)
        return True
    finally:
        _release_filelock(MACHINE_SENTINEL)


def track_machine_pids(project: str | None, *, agent: str, pids: list[int]) -> bool:
    """Tie `agent`'s machine hold to these client processes: it now lives exactly as long as any
    of them does (see `_holder_alive`). Only a hold `agent` itself took for `project` -- never a
    teammate's PIE hold it merely passed through, which must not vanish with the clients."""
    mine = _safe_project(project)
    _acquire_filelock(MACHINE_SENTINEL)
    try:
        state = _load(MACHINE_SENTINEL)
        ex = state.get("exclusive")
        if not ex or ex.get("agent") != agent or ex.get("project") != mine:
            return False
        ex["pids"] = sorted({int(p) for p in (ex.get("pids") or [])} | {int(p) for p in pids})
        ex["pid"] = 0
        _save(MACHINE_SENTINEL, state)
        return True
    finally:
        _release_filelock(MACHINE_SENTINEL)


def wait_if_blocked(project: str | None, *, agent: str | None = None,
                    wait: float = DEFAULT_WAIT_CAP) -> dict:
    """For read-only ops: block while ANOTHER agent holds the exclusive lease, then return.

    Fail-open: any coordination error returns immediately so a lease bug can never brick uap.
    """
    agent = agent or default_agent_id()
    deadline = _now() + max(0.0, wait)
    while True:
        try:
            # The holder we are waiting on may just have aged out with its PIE still running.
            reap_stale_pie(project)
            evicted = None
            _acquire_filelock(project)
            try:
                state, evicted = _refresh(project)
                _save(project, state)
            finally:
                _release_filelock(project)
                _after_eviction(project, evicted)
            ex = state.get("exclusive") or _reaper_holder(state)
            if ex is None or ex.get("agent") == agent:
                return {"ok": True, "blocked": False}
            holder = ex
        except Exception:
            return {"ok": True, "blocked": False, "note": "coordination unavailable; proceeding"}
        if _now() >= deadline:
            return {"ok": False, "blocked": True, "holder": holder}
        time.sleep(POLL_SECONDS)
