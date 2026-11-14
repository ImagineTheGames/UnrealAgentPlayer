"""Rebuild broker -- ONE editor restart serves every agent's pending code change.

Why (School's Out VR throughput analysis 2026-11-13, item 5.1): every agent with a header change
ran the project's restart script (Restart-Editor.ps1) for itself. Six editor kills in 41 minutes,
71 restart attempts in a day, six editor sessions that never ran a single PIE -- and every kill
ends the PIE session of whoever holds the editor and sends every verifier to the back of a 9-13
deep lease queue. The relink inside a restart costs the same whether it picks up one agent's code
or six, so one restart can serve all of them.

So agents no longer restart the editor. They file a request and (optionally) wait for its result:

    uap rebuild request --agent <tok> --task "<ticket> <title>" [--reason "..."]
        records a pending request and returns at once. If no runner is alive for the project,
        this call starts one (detached; at most one per project per machine, lock-protected).
    uap rebuild wait --agent <tok> [--seconds N]
        blocks until a rebuild that STARTED AFTER your latest request has FINISHED, then returns
        its result: ok, or failed + the failure note path. `still_waiting` (exit 1) when the
        seconds run out first -- call it again; nothing is lost.
    uap rebuild status
        pending requests, the runner, the last runs.

The runner (`uap rebuild serve`, started by `request`) is the only thing that runs the restart:
  * a batching window: it fires `window` seconds (default 240, $UAP_REBUILD_WINDOW) after the
    first pending request -- or after the previous rebuild finished, whichever is later, so two
    restarts never land back to back -- and at once when the editor is not running at all;
  * it serves EVERY pending request with ONE run of the restart command, then marks them served.
    Requests that arrive while a rebuild runs wait for the next batch (their window starts at once
    and is measured from the end of the run, see above);
  * an answer that leaves the editor untouched and retryable (2 lease held, 3 PIE live, 4 DLL
    holders / build lock busy) is not a result: the batch stays pending and is retried after
    $UAP_REBUILD_RETRY seconds (default 60), up to `MAX_DEFERRALS` times, then failed;
  * it exits when nothing is pending. A runner that died is replaced by the next `request` or
    `wait` (liveness = pid alive AND heartbeat fresh, so a reused pid is never mistaken for it).

The restart command is the project's own script. Default: `<project root>/Restart-Editor.ps1`
(project root = the directory of $UAP_UPROJECT, which the project's uap.ps1 launcher pins), run
with `-LeaseWaitSeconds 3600` and $UAP_AGENT_ID = `rebuild-broker`. That token comes back to the
lease as `requested_by`, and `coordination._rebuild_first` puts that waiter at the HEAD of the
queue: it waits for the CURRENT holder (a running PIE is never preempted) but not behind every
queued verifier. $UAP_REBUILD_COMMAND overrides the command for other projects.

State: `<reports>/.leases/<project>.rebuild.json`, read-modify-written under its own O_EXCL lock
(`<project>.rebuild.lock`), same discipline as the lease files. Run logs:
`<reports>/.leases/rebuild-logs/`. Pure stdlib.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

from unreal_agent_player import coordination as _coord

# requested_by on the runner's restart lease; coordination._rebuild_first puts it at the head.
BROKER_AGENT = _coord.BROKER_AGENT

DEFAULT_WINDOW = 240.0          # 4 min batching window (analysis: 3-5 min)
DEFAULT_RETRY = 60.0            # after a "not now" answer (lease held / PIE live / DLL holders)
MAX_DEFERRALS = 6
RUNNER_STALE = 120.0            # a runner whose heartbeat is older than this is dead
CLAIM_TTL = 30.0                # a "starting" claim older than this belonged to a spawn that failed
POLL = 2.0                      # `wait` re-check interval
SERVE_POLL = 5.0                # runner re-check interval (editor-down probe, new requests)
HEARTBEAT_EVERY = 5.0
LEASE_WAIT_SECONDS = 3600       # Restart-Editor -LeaseWaitSeconds: only the current holder is ahead
_KEEP_RUNS = 30
_KEEP_SERVED_SECONDS = 24 * 3600.0

# Restart-Editor.ps1 exit codes (header of that script).
EXIT_OK = 0
EXIT_BUILD_FAILED = 1          # failed after the kill: editor DOWN
EXIT_LEASE_HELD = 2            # editor untouched
EXIT_PIE_LIVE = 3              # editor untouched
EXIT_BLOCKED = 4               # editor untouched (DLL holders, build lock)
EXIT_LAUNCH_FAILED = 5         # editor DOWN
EXIT_SLOW = 6                  # relaunched, still loading, left running
EXIT_PREFLIGHT_FAILED = 7      # compile preflight failed: editor NOT touched, still running
DEFER_EXITS = (EXIT_LEASE_HELD, EXIT_PIE_LIVE, EXIT_BLOCKED)


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return max(0.0, float(raw)) if raw else default
    except ValueError:
        return default


def window_seconds() -> float:
    return _env_seconds("UAP_REBUILD_WINDOW", DEFAULT_WINDOW)


def retry_seconds() -> float:
    return _env_seconds("UAP_REBUILD_RETRY", DEFAULT_RETRY)


# --- state file -----------------------------------------------------------------------------

def _key(project: str | None) -> str:
    return _coord._safe_project(project)


def _state_path(project: str | None) -> pathlib.Path:
    return _coord._leases_dir() / f"{_key(project)}.rebuild.json"


def _lock_path(project: str | None) -> pathlib.Path:
    return _coord._leases_dir() / f"{_key(project)}.rebuild.lock"


def logs_dir() -> pathlib.Path:
    d = _coord._leases_dir() / "rebuild-logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextlib.contextmanager
def _locked(project: str | None):
    """The broker state's mutex: O_EXCL lockfile, broken when older than the lease stale limit.

    Every critical section here is a sub-millisecond read-modify-write, exactly like the lease
    file's, so the same rules apply: wait briefly, break a wedged lock, never proceed unlocked.
    """
    lp = _lock_path(project)
    token = f"{os.getpid()}:{uuid.uuid4().hex}"
    deadline = _now() + _coord._FILELOCK_TIMEOUT
    while True:
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, token.encode())
            finally:
                os.close(fd)
            break
        except (FileExistsError, PermissionError):
            try:
                if _now() - lp.stat().st_mtime > _coord._FILELOCK_STALE:
                    lp.unlink()
                    continue
            except OSError:
                pass
            if _now() > deadline:
                raise _coord.LeaseUnavailable(
                    f"could not take the rebuild-broker lock {lp} within "
                    f"{_coord._FILELOCK_TIMEOUT:.0f}s; refusing to change the request queue "
                    f"without it.") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        for _ in range(_coord._SHARING_RETRIES):
            try:
                if lp.read_text(encoding="utf-8").strip() == token:
                    lp.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(_coord._SHARING_RETRY_SLEEP)
            except OSError:
                break


def _blank() -> dict:
    return {"requests": [], "runs": [], "runner": None}


def _load(project: str | None) -> dict:
    p = _state_path(project)
    for attempt in range(5):
        try:
            raw = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _blank()
        try:
            st = json.loads(raw)
        except ValueError:
            time.sleep(0.02)
            continue
        if isinstance(st, dict):
            st.setdefault("requests", [])
            st.setdefault("runs", [])
            st.setdefault("runner", None)
            return st
    raise _coord.LeaseStateUnreadable(
        f"rebuild-broker state {p} exists but could not be parsed. Refusing to treat it as empty "
        f"-- that would drop every pending request. Delete it only if no rebuild is pending.")


def _save(project: str | None, st: dict) -> None:
    p = _state_path(project)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        for attempt in range(_coord._SHARING_RETRIES):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == _coord._SHARING_RETRIES - 1:
                    raise
                time.sleep(_coord._SHARING_RETRY_SLEEP)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


# --- runner liveness ------------------------------------------------------------------------

def _runner_alive(r: dict | None) -> bool:
    """A runner counts as alive while its pid lives AND its heartbeat is fresh, or while a spawn
    claim is younger than CLAIM_TTL (the spawned process has not registered yet)."""
    if not r:
        return False
    now = _now()
    if r.get("state") == "starting" and not r.get("pid"):
        return now - float(r.get("claimed_at") or 0) < CLAIM_TTL
    if now - float(r.get("heartbeat_at") or 0) > RUNNER_STALE:
        return False
    return _coord._pid_alive(int(r.get("pid") or 0))


def _pending(st: dict) -> list[dict]:
    return [r for r in st.get("requests", []) if not r.get("served_by")]


def _last_finished(st: dict) -> float:
    done = [float(r.get("finished_at") or 0) for r in st.get("runs", [])
            if r.get("result") in ("ok", "failed")]
    return max(done) if done else 0.0


def fires_at(st: dict, window: float | None = None) -> float | None:
    """When the current batch fires (absent a down editor or a retry). None = nothing pending."""
    pend = _pending(st)
    if not pend:
        return None
    w = window_seconds() if window is None else window
    first = min(float(r.get("requested_at") or 0) for r in pend)
    return max(first, _last_finished(st)) + w


def _prune(st: dict) -> None:
    now = _now()
    st["requests"] = [r for r in st.get("requests", [])
                      if not r.get("served_by")
                      or now - float(r.get("requested_at") or 0) < _KEEP_SERVED_SECONDS]
    st["runs"] = st.get("runs", [])[-_KEEP_RUNS:]


# --- the restart command --------------------------------------------------------------------

class NoRebuildCommand(RuntimeError):
    pass


def project_root() -> pathlib.Path | None:
    up = os.environ.get("UAP_UPROJECT", "").strip()
    if up:
        return pathlib.Path(up).parent
    return None


def rebuild_command() -> list[str] | str:
    """The command a batch runs. $UAP_REBUILD_COMMAND (one command line) wins; otherwise the
    project's Restart-Editor.ps1 next to $UAP_UPROJECT. Raises NoRebuildCommand when neither."""
    env = os.environ.get("UAP_REBUILD_COMMAND", "").strip()
    if env:
        return env
    root = project_root()
    if root is not None:
        script = root / "Restart-Editor.ps1"
        if script.is_file():
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(script), "-LeaseWaitSeconds", str(LEASE_WAIT_SECONDS)]
    raise NoRebuildCommand(
        "no rebuild command: run uap through the project's uap.ps1 (it pins $UAP_UPROJECT, and "
        "the broker runs Restart-Editor.ps1 next to it) or set $UAP_REBUILD_COMMAND.")


def failure_note_paths() -> dict:
    """Where Restart-Editor.ps1 explains a failure (Saved/Logs under the project root)."""
    root = project_root()
    if root is None:
        return {}
    logs = root / "Saved" / "Logs"
    return {"down": logs / "RestartEditor-LastFailure.txt",
            "preflight": logs / "RestartEditor-PreflightFailure.txt"}


_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _creationflags(breakaway: bool) -> int:
    if os.name != "nt":
        return 0
    # CREATE_NO_WINDOW, not DETACHED_PROCESS: the runner gets a hidden console that its children
    # (powershell, UBT, the countdown jobs) inherit, so none of them ever opens -- or activates --
    # a console window on the owner's desktop.
    f = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    return f | _CREATE_BREAKAWAY_FROM_JOB if breakaway else f


def run_command(cmd, env: dict, log_path: pathlib.Path, heartbeat) -> int:
    """Run one restart, output to `log_path`, calling `heartbeat()` every few seconds.
    Replaced by tests (the mocked runner)."""
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, env=env,
                                creationflags=_creationflags(False),
                                shell=isinstance(cmd, str) and os.name != "nt")
        nxt = 0.0
        while proc.poll() is None:
            if _now() >= nxt:
                heartbeat()
                nxt = _now() + HEARTBEAT_EVERY
            time.sleep(0.5)
        return int(proc.returncode)


def spawn_runner(project: str | None) -> int | None:
    """Start `uap rebuild serve` for `project`, detached from the caller (an agent's tool call
    ends long before a rebuild does). Returns the pid. Replaced by tests."""
    cmd = [sys.executable, "-m", "unreal_agent_player.cli", "rebuild", "serve"]
    if project:
        cmd += ["--project", project]
    extra = {} if os.name == "nt" else {"start_new_session": True}
    with open(logs_dir() / f"{_key(project)}-runner.log", "ab") as log:
        for breakaway in (True, False):
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, close_fds=True,
                                     creationflags=_creationflags(breakaway), **extra)
                return p.pid
            except OSError:
                # A job object that forbids breakaway refuses the flag (access denied). Without it
                # the runner may die with the caller's job; the next `rebuild wait` replaces it.
                if not breakaway:
                    raise
    return None


def _claim_runner(st: dict) -> bool:
    """Under the lock: mark a runner as starting if none is alive. True = the caller must spawn."""
    if _runner_alive(st.get("runner")):
        return False
    st["runner"] = {"pid": 0, "state": "starting", "claimed_at": _now(), "heartbeat_at": _now()}
    return True


def _ensure_runner(project: str | None, claimed: bool) -> dict:
    if not claimed:
        return {"runner_started": False}
    try:
        pid = spawn_runner(project)
        return {"runner_started": True, "runner_pid": pid}
    except Exception as exc:          # never leave a claim that blocks the next spawn for 30s
        with _locked(project):
            st = _load(project)
            r = st.get("runner") or {}
            if r.get("state") == "starting" and not r.get("pid"):
                st["runner"] = None
                _save(project, st)
        return {"runner_started": False, "runner_error": f"could not start the runner: {exc}"}


# --- verbs -----------------------------------------------------------------------------------

def request(project: str | None, *, agent: str, task: str = "", reason: str = "") -> dict:
    """File a rebuild request. Returns at once; starts the runner when none is alive."""
    if not agent:
        return {"ok": False, "error": "--agent <token> is required (or $UAP_AGENT_ID): "
                                      "`uap rebuild wait` finds your request by it."}
    try:
        rebuild_command()
    except NoRebuildCommand as exc:
        return {"ok": False, "error": str(exc)}
    rec = {"id": uuid.uuid4().hex[:12], "agent": agent, "task": _coord.sanitize_label(task),
           "reason": _coord.sanitize_label(reason), "requested_at": _now(), "served_by": None}
    with _locked(project):
        st = _load(project)
        _prune(st)
        st["requests"].append(rec)
        claimed = _claim_runner(st)
        _save(project, st)
        pend = len(_pending(st))
        fa = fires_at(st)
        runner = dict(st.get("runner") or {})
    res = {"ok": True, "requested": True, "request_id": rec["id"], "agent": agent,
           "pending": pend, "runner_state": runner.get("state"),
           "fires_in_s": max(0, round(fa - _now())) if fa else None,
           "window_s": round(window_seconds()),
           "next": f"uap rebuild wait --agent {agent} --seconds 110 (or run it in the "
                   f"background with --seconds 3600)",
           "hint": ("one restart serves every pending request: it fires after the batching "
                    "window (sooner if the editor is down) and waits for the current editor "
                    "holder, never preempting a running PIE. Do NOT run Restart-Editor.ps1 "
                    "yourself.")}
    res.update(_ensure_runner(project, claimed))
    return res


def _run_for(st: dict, run_id: str) -> dict | None:
    return next((r for r in st.get("runs", []) if r.get("id") == run_id), None)


def _result_body(req: dict, run: dict) -> dict:
    ok = run.get("result") == "ok"
    body = {"ok": ok, "done": True, "result": run.get("result"), "request_id": req.get("id"),
            "run_id": run.get("id"), "exit_code": run.get("exit_code"),
            "requested_at": req.get("requested_at"), "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"), "served": len(run.get("batch") or []),
            "editor_up": run.get("editor_up"), "log": run.get("log")}
    for k in ("note", "note_excerpt", "warning", "error"):
        if run.get(k):
            body[k] = run[k]
    if not ok:
        body["hint"] = ("the rebuild that covered your request FAILED. Read `note` (file, line and "
                        "the owning CL). editor_up:true = the compile preflight stopped it and "
                        "nobody lost the editor; fix the build (or get that CL's owner to), then "
                        "`uap rebuild request` again.")
    return body


def wait(project: str | None, *, agent: str, seconds: float = 110.0,
         poll: float = POLL) -> dict:
    """Block until a rebuild that started after `agent`'s latest request has finished."""
    if not agent:
        return {"ok": False, "error": "--agent <token> is required (or $UAP_AGENT_ID)."}
    deadline = _now() + max(0.0, seconds)
    started = _now()
    while True:
        with _locked(project):
            st = _load(project)
            mine = [r for r in st.get("requests", []) if r.get("agent") == agent]
            req = max(mine, key=lambda r: float(r.get("requested_at") or 0)) if mine else None
            claimed = False
            if req is not None and req.get("served_by"):
                run = _run_for(st, req["served_by"])
            else:
                run = None
                if req is not None:
                    claimed = _claim_runner(st)     # the runner died: replace it
                    if claimed:
                        _save(project, st)
            runner = dict(st.get("runner") or {})
            fa = fires_at(st)
            pend = len(_pending(st))
        if req is None:
            return {"ok": False, "done": False, "not_requested": True, "agent": agent,
                    "error": "no rebuild request from this agent. `uap rebuild request --agent "
                             "<tok> --task \"...\"` first."}
        if req.get("served_by"):
            if run is None:
                return {"ok": False, "done": True, "result": "unknown",
                        "request_id": req.get("id"), "run_id": req.get("served_by"),
                        "error": "served by a run that is no longer on record"}
            return _result_body(req, run)
        extra = _ensure_runner(project, claimed) if claimed else {}
        if _now() >= deadline:
            body = {"ok": False, "done": False, "still_waiting": True, "agent": agent,
                    "request_id": req.get("id"), "pending": pend,
                    "runner_state": runner.get("state"),
                    "waited_seconds": round(_now() - started, 1),
                    "resume": f"uap rebuild wait --agent {agent} --seconds 110",
                    "hint": "your request stays queued; call wait again (or run it in the "
                            "background with a long --seconds). Nothing is lost by returning."}
            if runner.get("state") == "running":
                body["running_since"] = runner.get("run_started_at")
            elif fa:
                body["fires_in_s"] = max(0, round(fa - _now()))
            if runner.get("retry_at"):
                body["retry_in_s"] = max(0, round(float(runner["retry_at"]) - _now()))
                body["last_deferral"] = runner.get("last_deferral")
            body.update(extra)
            return body
        _sleep(max(0.05, min(poll, deadline - _now())))


def status(project: str | None) -> dict:
    with _locked(project):
        st = _load(project)
    runner = st.get("runner")
    fa = fires_at(st)
    return {"ok": True, "project": _key(project),
            "pending": [{k: r.get(k) for k in ("id", "agent", "task", "reason", "requested_at")}
                        for r in _pending(st)],
            "runner": runner, "runner_alive": _runner_alive(runner),
            "fires_in_s": max(0, round(fa - _now())) if fa else None,
            "window_s": round(window_seconds()),
            "last_runs": [{k: r.get(k) for k in ("id", "result", "exit_code", "started_at",
                                                 "finished_at", "editor_up", "note", "log")}
                          | {"served": len(r.get("batch") or [])}
                          for r in st.get("runs", [])[-5:]]}


# --- the runner ------------------------------------------------------------------------------

def _classify(code: int) -> tuple[str, dict]:
    """Restart exit code -> (result, extra). result: ok | failed | deferred."""
    notes = failure_note_paths()
    if code == EXIT_OK:
        return "ok", {"editor_up": True}
    if code == EXIT_SLOW:
        return "ok", {"editor_up": True,
                      "warning": "the build succeeded and the editor was relaunched, but it had "
                                 "not finished loading when the restart gave up waiting (exit 6)."}
    if code in DEFER_EXITS:
        return "deferred", {}
    if code == EXIT_PREFLIGHT_FAILED:
        return "failed", {"editor_up": True, "note": notes.get("preflight")}
    if code in (EXIT_BUILD_FAILED, EXIT_LAUNCH_FAILED):
        return "failed", {"editor_up": False, "note": notes.get("down")}
    return "failed", {"editor_up": None, "error": f"restart exited {code}"}


def _excerpt(path) -> str:
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[:16])


def _editor_down(project: str | None) -> bool:
    try:
        return _coord._editor_probe(project) is False
    except Exception:
        return False


def serve(project: str | None, *, window: float | None = None, retry: float | None = None,
          poll: float | None = None, run=None, max_deferrals: int = MAX_DEFERRALS) -> dict:
    """The coalescing runner. Returns when nothing is pending (or another runner is alive)."""
    run = run or run_command
    poll = SERVE_POLL if poll is None else poll
    me = os.getpid()
    with _locked(project):
        st = _load(project)
        r = st.get("runner")
        if r and r.get("pid") and int(r["pid"]) != me and _runner_alive(r):
            return {"ok": True, "served_runs": 0, "already_running": r.get("pid")}
        st["runner"] = {"pid": me, "state": "window", "started_at": _now(),
                        "heartbeat_at": _now(), "deferrals": 0}
        _save(project, st)
    runs_done = 0

    def _beat(**fields) -> None:
        with _locked(project):
            s = _load(project)
            rr = s.get("runner") or {}
            if int(rr.get("pid") or 0) != me:
                return                      # replaced (we were presumed dead): stop writing
            rr["heartbeat_at"] = _now()
            rr.update(fields)
            s["runner"] = rr
            _save(project, s)

    try:
        while True:
            # The editor probe reads the process list: never inside the lock, and only when there
            # is something to serve.
            with _locked(project):
                has_pending = bool(_pending(_load(project)))
            down = _editor_down(project) if has_pending else False
            with _locked(project):
                st = _load(project)
                rr = st.get("runner") or {}
                if int(rr.get("pid") or 0) != me:
                    return {"ok": True, "served_runs": runs_done, "replaced": True}
                pend = _pending(st)
                if not pend:
                    st["runner"] = None
                    _prune(st)
                    _save(project, st)
                    return {"ok": True, "served_runs": runs_done}
                fa = fires_at(st, window)
                retry_at = float(rr.get("retry_at") or 0)
                now = _now()
                due = (retry_at and now >= retry_at) or (not retry_at and (down or now >= fa))
                if not due:
                    nxt = retry_at or fa
                    rr.update({"state": "retry" if retry_at else "window",
                               "fires_at": nxt, "heartbeat_at": now})
                    st["runner"] = rr
                    _save(project, st)
                    batch = None
                    nap = max(0.05, min(poll, nxt - now))
                else:
                    try:
                        cmd = rebuild_command()
                    except NoRebuildCommand as exc:
                        cmd, err = None, str(exc)
                    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-" \
                             f"{uuid.uuid4().hex[:6]}"
                    batch = [q["id"] for q in pend]
                    log = logs_dir() / f"{_key(project)}-{run_id}.log"
                    rec = {"id": run_id, "started_at": now, "finished_at": None,
                           "batch": batch, "result": "running", "exit_code": None,
                           "log": str(log), "trigger": ("retry" if retry_at else
                                                        "editor_down" if down else "window"),
                           "agents": sorted({q.get("agent") for q in pend})}
                    st["runs"].append(rec)
                    rr.update({"state": "running", "run_id": run_id, "run_started_at": now,
                               "heartbeat_at": now, "fires_at": None})
                    st["runner"] = rr
                    _save(project, st)
            if batch is None:
                _sleep(nap)
                continue
            if cmd is None:
                code, extra_err = -1, err
            else:
                env = dict(os.environ)
                env["UAP_AGENT_ID"] = BROKER_AGENT
                tasks = [q.get("task") or q.get("agent") for q in pend]
                env["UAP_TASK"] = _coord.sanitize_label(
                    f"rebuild broker: {len(batch)} request(s): " + ", ".join(tasks))
                try:
                    code, extra_err = run(cmd, env, log, _beat), None
                except Exception as exc:
                    code, extra_err = -1, f"could not run the restart: {exc}"
            result, extra = _classify(code)
            if extra_err:
                extra["error"] = extra_err
            with _locked(project):
                st = _load(project)
                rr = st.get("runner") or {}
                rec = _run_for(st, run_id) or {}
                rec.update({"finished_at": _now(), "exit_code": code})
                if result == "deferred":
                    defers = int(rr.get("deferrals") or 0) + 1
                    if defers >= max_deferrals:
                        result = "failed"
                        extra = {"editor_up": True,
                                 "error": f"gave up after {defers} attempts that each left the "
                                          f"editor untouched (last exit {code}: lease held / PIE "
                                          f"live / DLL holders). Read the log."}
                        rr["deferrals"] = 0
                        rr.pop("retry_at", None)
                    else:
                        rr["deferrals"] = defers
                        rr["retry_at"] = _now() + (retry_seconds() if retry is None else retry)
                        rr["last_deferral"] = {"exit_code": code, "log": str(log)}
                        rec["result"] = "deferred"
                if result != "deferred":
                    rec["result"] = result
                    rec.update(extra)
                    if rec.get("note"):
                        rec["note"] = str(rec["note"])
                        rec["note_excerpt"] = _excerpt(rec["note"])
                    for q in st.get("requests", []):
                        if q.get("id") in batch:
                            q["served_by"] = run_id
                    rr["deferrals"] = 0
                    rr.pop("retry_at", None)
                    rr.pop("last_deferral", None)
                    runs_done += 1
                rr.update({"state": "window", "run_id": None, "heartbeat_at": _now()})
                st["runner"] = rr
                _prune(st)
                _save(project, st)
    except BaseException:
        # A dying runner must not leave a claim that blocks its replacement until it goes stale.
        with contextlib.suppress(Exception), _locked(project):
            st = _load(project)
            rr = st.get("runner") or {}
            if int(rr.get("pid") or 0) == me:
                st["runner"] = None
                _save(project, st)
        raise
