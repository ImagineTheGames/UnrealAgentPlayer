"""Two projects ran PIE at the same time under the machine-wide lock (2026-10-22).

Live evidence, reconstructed from both editors' logs and the agents' own uap output:

  22:04:47  School's Out (`sprayhub`) `pie stop` gives the machine back.
  22:04:48  Project Broken Wings (`headon`) was first in the machine queue: takes it, PIE starts.
  ~22:04:50 sprayhub's `pie start` joins the queue behind PBW and polls every 2 s...
  22:05:37  ...and School's Out PIE STARTS, with PBW still in PIE (it stops at 22:05:52).
            Nothing on the PBW side released anything in that window.

`acquire_machine` can only return while another project holds the lock by raising, and the CLI
caught every exception and ran the verb anyway ("fail-open"). On Windows the lock's own file
operations raise routinely under contention: `os.open(O_EXCL)` on the lockfile gets EACCES while a
competing release is mid-delete, and `os.replace` onto the state file gets WinError 5 whenever a
reader has it open -- which both editors' overlays do once a second. One such error in ~24 polls
and School's Out's PIE started with NO machine record, so PBW's next `pie start` was granted too,
and so on until PBW's `fullmapclicks` held the machine while School's Out was still playing.

The other suspects are pinned here as well, each with the incident shape it would have had.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player import cli
from unreal_agent_player.errors import AgentError, ErrorCode

_STARTED = json.dumps({"ok": True, "mode": "flat"})
_STOPPED = json.dumps({"ok": True, "was_playing": True, "cancelled_queued_start": False,
                       "in_progress": True})


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _stub_rc(monkeypatch, table):
    """RC table; a callable value is called with the target project. Anything else is a bug."""
    seen = []

    def fake(func, params, project=None):
        seen.append(func)
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val(project) if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _pie_start(monkeypatch, project, agent):
    _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    return cli.main(["pie", "start", "--project", project, "--agent", agent])


def _holder():
    return co.machine_status()["holder"]


# --- root cause: a transient lock error turned "busy" into "go" ---------------------------

def test_a_transient_lock_error_while_queued_never_starts_pie_unlocked(monkeypatch, capsys):
    """The incident itself: School's Out holds the machine for PIE; PBW's `pie start` hits one
    transient Windows sharing error on the machine lock. It must stay blocked, not start PIE."""
    assert _pie_start(monkeypatch, "SchoolsOut", "sprayhub") == 0
    capsys.readouterr()

    real = co._acquire_filelock
    hits = {"n": 0}

    def flaky(project):
        if isinstance(project, co._MachineScope) and hits["n"] == 0:
            hits["n"] += 1
            raise PermissionError(13, "Permission denied", "_machine.lock")
        return real(project)

    monkeypatch.setattr(co, "_acquire_filelock", flaky)
    seen = _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True,
                                  "IsPIEInProgress": True})
    rc = cli.main(["pie", "start", "--project", "ProjectBrokenWings", "--agent", "fullmapclicks"])
    assert hits["n"] == 1, "the injected error never fired; the test proves nothing"
    assert "StartPIEMode" not in seen, "PIE started in PBW while School's Out held the machine"
    assert rc == 1
    body = _out(capsys)
    assert body["ok"] is False and body["scope"] == "machine"
    assert _holder()["agent"] == "sprayhub"


def test_an_unreadable_machine_lock_refuses_instead_of_running_unlocked(monkeypatch, capsys):
    """When the lock cannot be decided at all, the foreground verb must not run. The escape hatch
    for a genuinely broken lock is $UAP_MACHINE_LOCK=0, a decision a human makes on purpose."""
    def unreadable(*a, **k):
        raise co.LeaseStateUnreadable("lease state _machine.json exists but could not be read")

    monkeypatch.setattr(co, "acquire_machine", unreadable)
    seen = _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    rc = cli.main(["pie", "start", "--project", "ProjectBrokenWings", "--agent", "A"])
    assert "StartPIEMode" not in seen
    assert rc == 1
    body = _out(capsys)
    assert body["coordination_unsafe"] is True and body["scope"] == "machine"
    assert "UAP_MACHINE_LOCK" in body["hint"]


def test_the_filelock_rides_out_a_delete_pending_lockfile(monkeypatch):
    """Windows answers O_CREAT|O_EXCL with EACCES, not EEXIST, while another process's release of
    the lockfile is still completing. That is "busy, try again", not a reason to give up."""
    real_open = os.open
    fails = {"n": 2}

    def racing_open(path, flags, *a, **k):
        if str(path).endswith(".lock") and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(co.os, "open", racing_open)
    co._acquire_filelock(co.MACHINE_SENTINEL)
    try:
        assert fails["n"] == 0
        assert co._lock_path(co.MACHINE_SENTINEL).exists()
    finally:
        co._release_filelock(co.MACHINE_SENTINEL)


def test_save_rides_out_a_reader_holding_the_state_file(monkeypatch):
    """`os.replace` onto a file another process has open without FILE_SHARE_DELETE fails with
    WinError 5. Each editor's overlay reads `_machine.json` like that once a second."""
    real_replace = os.replace
    fails = {"n": 2}

    def busy_replace(src, dst):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(13, "Access is denied", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(co.os, "replace", busy_replace)
    co._save(co.MACHINE_SENTINEL, {**co._blank(), "generation": 7})
    assert fails["n"] == 0
    assert co._load(co.MACHINE_SENTINEL)["generation"] == 7


_STRESS_WORKER = textwrap.dedent("""
    import collections, json, sys, time
    from unreal_agent_player import coordination as c
    errs = collections.Counter(); n = 0; end = time.time() + float(sys.argv[1])
    while time.time() < end:
        try:
            c._acquire_filelock(c.MACHINE_SENTINEL)
            try:
                st = c._load(c.MACHINE_SENTINEL); st["generation"] = n
                c._save(c.MACHINE_SENTINEL, st)
            finally:
                c._release_filelock(c.MACHINE_SENTINEL)
            n += 1
        except Exception as e:
            errs[type(e).__name__] += 1
    print(json.dumps({"ops": n, "errors": errs}))
""")


@pytest.mark.skipif(os.name != "nt", reason="the sharing violations are Windows semantics")
def test_real_processes_and_an_overlay_reader_never_break_the_lock(tmp_path):
    """The real race, not a mock: several processes doing what a waiting acquire does, plus a
    reader opening `_machine.json` the way the editor overlay does. Every critical section must
    complete; before the fix a large share raised PermissionError."""
    root = tmp_path / "reports"
    (root / ".leases").mkdir(parents=True)
    script = tmp_path / "worker.py"
    script.write_text(_STRESS_WORKER, encoding="utf-8")
    env = {**os.environ, "UAP_REPORTS_DIR": str(root)}
    state = root / ".leases" / "_machine.json"
    stop = threading.Event()

    def overlay_reader():
        while not stop.is_set():
            try:
                with open(state, "rb") as f:    # Python's open: no FILE_SHARE_DELETE, like UE's
                    f.read()
            except OSError:
                pass
            time.sleep(0.005)                   # ~200 Hz; the real overlay reads at 1 Hz

    reader = threading.Thread(target=overlay_reader, daemon=True)
    reader.start()
    try:
        procs = [subprocess.Popen([sys.executable, str(script), "4"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
        results = [json.loads(p.communicate(timeout=120)[0].strip().splitlines()[-1])
                   for p in procs]
    finally:
        stop.set()
        reader.join(5)
    errors = {}
    for r in results:
        for k, v in r["errors"].items():
            errors[k] = errors.get(k, 0) + v
    assert sum(r["ops"] for r in results) > 0
    assert errors == {}, f"coordination raised under real contention: {errors}"


# --- suspect 1: a same-project one-call verb dropping the sticky PIE hold --------------------

def test_a_same_project_click_never_drops_the_sticky_pie_hold(monkeypatch, capsys):
    """sprayhub's last verb was a `click`. A pass-through call must not release a hold it did not
    take, whether it comes from the PIE starter itself or a teammate. (Ruled out; pinned.)"""
    assert _pie_start(monkeypatch, "SchoolsOut", "sprayhub") == 0
    monkeypatch.setattr(cli, "_click", lambda args: 0)
    for agent in ("sprayhub", "teammate"):
        assert cli.main(["click", "Options", "--project", "SchoolsOut", "--agent", agent]) == 0
    holder = _holder()
    assert holder["agent"] == "sprayhub" and holder["reason"] == "pie:start"
    assert holder["pid"] == 0                     # still the sticky session hold


# --- suspect 2: `lease release` by one agent freeing another agent's machine hold ------------

def test_lease_release_by_a_non_holder_keeps_the_holders_machine_turn(monkeypatch, capsys):
    """A releases nothing it does not hold. With the editor unreachable the PIE check cannot
    refuse, and the release used to free the machine for the whole project anyway."""
    def unreachable(project):
        raise AgentError(ErrorCode.UE_UNREACHABLE, "editor busy")

    _stub_rc(monkeypatch, {"IsPIEInProgress": unreachable, "IsInPIE": unreachable})
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "holder",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    assert cli.main(["lease", "release", "--agent", "bystander", "--project", "SchoolsOut"]) == 0
    capsys.readouterr()
    assert _holder()["agent"] == "holder"
    assert co.status("SchoolsOut")["exclusive"]["agent"] == "holder"


def test_the_project_lease_holder_releasing_still_frees_the_projects_hold(monkeypatch, capsys):
    """...while the agent that DOES hold the project's exclusive lease frees the project's machine
    hold even if a teammate's `pie start` took it: that session is the lease holder's."""
    _stub_rc(monkeypatch, {"IsPIEInProgress": False})
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "level", "--agent", "owner",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    co.release_machine(force=True)
    co.acquire_machine("SchoolsOut", reason="pie:start", agent="teammate", pid=0, wait=0)
    assert cli.main(["lease", "release", "--agent", "owner", "--project", "SchoolsOut"]) == 0
    assert _holder() is None


# --- suspect 3: eviction / release crossing projects through an agent-name match --------------

def test_a_pie_stop_in_one_project_never_frees_the_other_projects_hold(monkeypatch, capsys):
    """`release_machine` matched on agent OR project, so the same token in both projects (agents
    reuse short tokens) let PBW's `pie stop` free School's Out's live session."""
    assert _pie_start(monkeypatch, "SchoolsOut", "fixer") == 0
    _stub_rc(monkeypatch, {"StopPIEEx": _STOPPED, "IsPIEInProgress": False})
    assert cli.main(["pie", "stop", "--project", "ProjectBrokenWings", "--agent", "fixer"]) == 0
    capsys.readouterr()
    assert _holder()["project"] == "schoolsout"


def test_a_dead_editor_eviction_never_frees_the_other_projects_hold(monkeypatch):
    """PR #15 voids a pie lease whose editor died and frees that project's machine hold. It must
    never reach the other project's hold, whatever the agent tokens are."""
    co.acquire_machine("SchoolsOut", reason="pie", agent="fixer", pid=0, wait=0)
    co.acquire("ProjectBrokenWings", "exclusive", reason="pie", agent="fixer", pid=0, wait=0)
    monkeypatch.setattr(co, "_editor_probe",
                        lambda p: str(p).lower() != "projectbrokenwings")
    co.status("ProjectBrokenWings")
    assert co.status("ProjectBrokenWings")["exclusive"] is None     # the eviction did run
    assert _holder()["project"] == "schoolsout"


# --- suspect 4: a sticky hold ageing out under a live PIE session ----------------------------

def test_a_live_pie_session_keeps_its_machine_hold_past_the_ttl(monkeypatch, capsys):
    """An agent driving a long PIE session with exec/rc only never refreshes the machine hold. Past
    the TTL the other project used to be granted while this PIE was still running."""
    monkeypatch.setattr(co, "_machine_pie_probe", cli._machine_pie_probe)
    assert _pie_start(monkeypatch, "SchoolsOut", "sprayhub") == 0
    capsys.readouterr()
    later = time.time() + co.MACHINE_TTL + 60
    monkeypatch.setattr(co, "_now", lambda: later)
    seen = _stub_rc(monkeypatch, {
        "StartPIEMode": _STARTED, "IsInPIE": True,
        "IsPIEInProgress": lambda project: str(project).lower() == "schoolsout"})
    rc = cli.main(["pie", "start", "--project", "ProjectBrokenWings", "--agent", "fullmapclicks"])
    assert "StartPIEMode" not in seen
    assert rc == 1
    assert _holder()["agent"] == "sprayhub"
    assert _holder()["heartbeat_at"] == later      # the live session refreshed it


def test_a_stale_hold_with_no_pie_running_still_ages_out(monkeypatch, capsys):
    """The probe only KEEPS a hold whose editor is really in PIE. An abandoned one still frees."""
    monkeypatch.setattr(co, "_machine_pie_probe", cli._machine_pie_probe)
    assert _pie_start(monkeypatch, "SchoolsOut", "gone") == 0
    later = time.time() + co.MACHINE_TTL + 60
    monkeypatch.setattr(co, "_now", lambda: later)
    seen = _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True,
                                  "IsPIEInProgress": False})
    assert cli.main(["pie", "start", "--project", "ProjectBrokenWings", "--agent", "next"]) == 0
    assert "StartPIEMode" in seen
    assert _holder()["agent"] == "next"


# --- 6: never sit on the project lease while waiting for the machine --------------------------

def _fast_clock(monkeypatch):
    """Sleeping advances a fake clock, so a wait loop finishes instantly; returns the sleeps."""
    base = time.time()
    offset = {"t": 0.0}
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        offset["t"] += s

    monkeypatch.setattr(co, "_now", lambda: base + offset["t"])
    monkeypatch.setattr(co.time, "sleep", fake_sleep)
    return sleeps


def test_a_one_call_verb_holding_the_project_lease_does_not_wait_on_the_machine(monkeypatch,
                                                                              capsys):
    """sprayhub held School's Out's lease and sat in the machine queue on a `click`, with four
    teammates queued behind its lease. A lease holder denied the machine gives the lease back."""
    monkeypatch.setenv("UAP_LEASE_WAIT", "900")
    sleeps = _fast_clock(monkeypatch)
    co.acquire_machine("ProjectBrokenWings", reason="pie", agent="fullmapclicks", pid=0, wait=0)
    co.acquire("SchoolsOut", "exclusive", reason="2p", agent="blockplayer", pid=0, wait=0)
    clicked = []
    monkeypatch.setattr(cli, "_click", lambda args: clicked.append(1) or 0)
    _stub_rc(monkeypatch, {"IsPIEInProgress": False})
    rc = cli.main(["click", "Options", "--project", "SchoolsOut", "--agent", "blockplayer"])
    assert rc == 1 and not clicked
    assert not [s for s in sleeps if s >= co.POLL_SECONDS], "sat in the machine poll loop"
    body = _out(capsys)
    assert body["busy"] is True and body["blocked_by"] == "fullmapclicks"
    assert body["project_lease_released"] is True
    assert co.status("SchoolsOut")["exclusive"] is None


def test_a_lease_holder_mid_pie_keeps_its_lease_but_still_does_not_wait(monkeypatch, capsys):
    """With its own PIE live, handing the project lease on would give a teammate a running session
    (what `lease release` refuses). Keep it, fail fast, and say how to get out."""
    monkeypatch.setenv("UAP_LEASE_WAIT", "900")
    sleeps = _fast_clock(monkeypatch)
    co.acquire_machine("ProjectBrokenWings", reason="pie", agent="fullmapclicks", pid=0, wait=0)
    co.acquire("SchoolsOut", "exclusive", reason="pie", agent="sprayhub", pid=0, wait=0)
    monkeypatch.setattr(cli, "_click", lambda args: 0)
    _stub_rc(monkeypatch, {"IsPIEInProgress": True})
    rc = cli.main(["click", "Options", "--project", "SchoolsOut", "--agent", "sprayhub"])
    assert rc == 1
    assert not [s for s in sleeps if s >= co.POLL_SECONDS]
    body = _out(capsys)
    assert body["project_lease_released"] is False and body["pie_live"] is True
    assert "pie stop" in body["hint"]
    assert co.status("SchoolsOut")["exclusive"]["agent"] == "sprayhub"


def test_an_agent_without_a_lease_still_waits_its_turn(monkeypatch, capsys):
    """Waiting is still right for a caller that blocks nobody's project lease."""
    monkeypatch.setenv("UAP_LEASE_WAIT", "10")
    sleeps = _fast_clock(monkeypatch)
    co.acquire_machine("ProjectBrokenWings", reason="pie", agent="fullmapclicks", pid=0, wait=0)
    monkeypatch.setattr(cli, "_click", lambda args: 0)
    assert cli.main(["click", "Options", "--project", "SchoolsOut", "--agent", "free"]) == 1
    assert [s for s in sleeps if s >= co.POLL_SECONDS], "did not wait at all"


def test_pie_stop_is_never_blocked_by_the_other_projects_hold(monkeypatch, capsys):
    """Stopping PIE gives the foreground back; queueing it behind the other project only keeps
    both sessions running longer."""
    co.acquire_machine("ProjectBrokenWings", reason="pie", agent="fullmapclicks", pid=0, wait=0)
    seen = _stub_rc(monkeypatch, {"StopPIEEx": _STOPPED, "IsPIEInProgress": False})
    assert cli.main(["pie", "stop", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    assert "StopPIEEx" in seen
    assert _holder()["agent"] == "fullmapclicks"


# --- 7: the generated launcher must not leak its project into the next launcher ---------------

def _powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(os.name != "nt" or _powershell() is None, reason="needs Windows PowerShell")
def test_launchers_in_one_session_each_target_their_own_project(tmp_path):
    """PBW's launcher then School's Out's in ONE PowerShell process sent the second call to PBW:
    the first left $env:UAP_PROJECT behind and the second only set it when empty."""
    repo = pathlib.Path(__file__).resolve().parents[2]
    home = tmp_path / "uaphome"
    (home / "mcp-server").mkdir(parents=True)
    (home / "mcp-server" / "pyproject.toml").write_text("", encoding="utf-8")
    venv = home / "mcp-server" / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    site = next(venv.glob("Lib/site-packages"))
    pkg = site / "unreal_agent_player"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cli.py").write_text(
        "import os, sys\n"
        "print('TARGET=%s ARGS=%s' % (os.environ.get('UAP_PROJECT'), ' '.join(sys.argv[1:])))\n",
        encoding="utf-8")

    ps = _powershell()
    launchers = {}
    for name in ("Alpha", "Beta"):
        proj = tmp_path / name
        proj.mkdir()
        (proj / f"{name}.uproject").write_text("{}", encoding="utf-8")
        res = subprocess.run([ps, "-NoProfile", "-File", str(repo / "Install-AgentTest.ps1"),
                              "-ProjectRoot", str(proj), "-Python", "C:/fake/python.exe"],
                             capture_output=True, text=True, check=False)
        assert res.returncode == 0, res.stderr
        launchers[name] = proj / "uap.ps1"

    session = textwrap.dedent(f"""
        $ErrorActionPreference = 'Continue'
        Remove-Item Env:UAP_PROJECT -ErrorAction SilentlyContinue
        & '{launchers['Alpha']}' status
        & '{launchers['Beta']}' status
        "AFTER=[$env:UAP_PROJECT]"
        $env:UAP_PROJECT = 'CallerValue'
        & '{launchers['Beta']}' status
        "RESTORED=[$env:UAP_PROJECT]"
        & '{launchers['Beta']}' status --project Alpha
    """)
    script = tmp_path / "session.ps1"
    script.write_text(session, encoding="ascii")
    env = {**os.environ, "UAP_HOME": str(home)}
    env.pop("UAP_PROJECT", None)
    res = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                         capture_output=True, text=True, env=env, check=False)
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    assert lines == [
        "TARGET=Alpha ARGS=status",
        "TARGET=Beta ARGS=status",
        "AFTER=[]",
        "TARGET=Beta ARGS=status",
        "RESTORED=[CallerValue]",
        "TARGET=Beta ARGS=status --project Alpha",   # per-call override: the CLI's --project
    ], res.stdout + res.stderr
