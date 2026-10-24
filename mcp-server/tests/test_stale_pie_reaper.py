"""An agent that stops mid-session must not leave PIE running for hours (2026-10-23).

Agent `gpufoliage` was in Project Broken Wings PIE when it hit its turn limit and stopped without
handing off. Its `pie` lease aged out on the 600s TTL, but nothing stopped the session: PIE kept
running for about 1.5 hours, holding the machine-wide lock (a live PIE keeps a stale machine hold
alive), stealing focus and burning the GPU, until the director noticed.

These tests drive the real CLI against a fake editor (RC stubbed) and a fake clock. Each case is
the incident shape, or one of the places the fix must NOT fire: a human's PIE, an agent that is
still working, a rebuild, the escape hatch. No test reaches a real editor.
"""
import json
import threading
import time

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player import cli
from unreal_agent_player.errors import AgentError, ErrorCode

PBW = "ProjectBrokenWings"
SOVR = "SchoolsOut"


class FakeEditor:
    """One project's editor as RC sees it: PIE on/off, when it started, how often it was stopped."""

    def __init__(self, clock, *, elapsed_verb=True):
        self.clock = clock
        self.playing = False
        self.started_at = 0.0
        self.stops = 0
        self.elapsed_verb = elapsed_verb

    def start(self):                    # what StartPIEMode / a human pressing Play does
        self.playing = True
        self.started_at = self.clock["t"]

    def call(self, func, params):
        if func in ("StartPIEMode", "StartPIE"):
            self.start()
            return json.dumps({"ok": True, "mode": "flat"}) if func == "StartPIEMode" else True
        if func in ("IsInPIE", "IsPIEInProgress"):
            return self.playing
        if func == "GetPIEElapsedSeconds":
            if not self.elapsed_verb:
                raise AgentError(ErrorCode.UE_OBJECT_NOT_FOUND,
                                 "Unable to resolve the preset field GetPIEElapsedSeconds")
            return (self.clock["t"] - self.started_at) if self.playing else 0.0
        if func == "StopPIEEx":
            self.stops += 1
            was = self.playing
            self.playing = False
            return json.dumps({"ok": True, "was_playing": was, "cancelled_queued_start": False,
                               "in_progress": False})
        raise AssertionError(f"unexpected RC call {func}")


@pytest.fixture
def clock(monkeypatch):
    c = {"t": 1_800_000_000.0}
    monkeypatch.setattr(co, "_now", lambda: c["t"])
    return c


@pytest.fixture
def editors(monkeypatch, clock):
    """Fake editors for both projects, the CLI's real reaper / machine seams wired to them."""
    eds = {PBW.lower(): FakeEditor(clock), SOVR.lower(): FakeEditor(clock)}

    def fake_rc(func, params, project=None):
        return eds[str(project).lower()].call(func, params)

    monkeypatch.setattr(cli, "_rc_call", fake_rc)
    monkeypatch.setattr(co, "_machine_pie_probe", cli._machine_pie_probe)
    # getattr: these tests must FAIL (not error) on a build without the reaper.
    monkeypatch.setattr(co, "_pie_session_probe", getattr(cli, "_reaper_pie_probe", None),
                        raising=False)
    monkeypatch.setattr(co, "_pie_stopper", getattr(cli, "_reaper_pie_stop", None),
                        raising=False)
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    monkeypatch.delenv("UAP_PIE_REAPER", raising=False)
    monkeypatch.delenv("UAP_PIE_REAP_IDLE", raising=False)
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)
    return eds


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _run(capsys, *argv):
    rc = cli.main(list(argv))
    return rc, _out(capsys)


def _lease_pie(capsys, project, agent):
    rc, out = _run(capsys, "lease", "acquire", "exclusive", "--reason", "pie", "--agent", agent,
                   "--project", project, "--wait", "0")
    assert rc == 0 and out["granted"], out


def _pie_start(capsys, project, agent):
    rc, out = _run(capsys, "pie", "start", "--project", project, "--agent", agent)
    assert rc == 0 and out["playing"], out


def _status(capsys, project):
    rc, out = _run(capsys, "lease", "status", "--project", project)
    assert rc == 0, out
    return out


# ---- the incident ------------------------------------------------------------------------------

def test_an_aged_out_pie_lease_takes_its_pie_session_with_it(capsys, clock, editors):
    """gpufoliage: pie lease, PIE started, then silence past the TTL. The next look stops PIE."""
    pbw = editors["projectbrokenwings"]
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    assert co.machine_status()["holder"]["project"] == "projectbrokenwings"

    clock["t"] += 601
    out = _status(capsys, PBW)

    assert pbw.stops == 1 and not pbw.playing
    assert out["exclusive"] is None
    reap = out["last_reap"]
    assert reap["why"] == "stale_pie_reaped"
    assert reap["cause"] == "lease_ttl"
    assert reap["agent"] == "gpufoliage"
    assert reap["held_reason"] == "pie"
    assert out["last_eviction"]["why"] == "stale_pie_reaped"
    assert out["pie_session"] is None
    # The machine is given back: the other project is no longer locked out by a dead session.
    assert out["machine"]["holder"] is None


def test_the_reaped_agent_is_told_once_on_its_next_call(capsys, clock, editors):
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] += 601
    _status(capsys, PBW)

    rc, out = _run(capsys, "rc", "IsInPIE", "--project", PBW, "--agent", "gpufoliage")
    assert rc == 1
    assert out["lease_evicted"] is True and out["why"] == "stale_pie_reaped"
    rc, out = _run(capsys, "rc", "IsInPIE", "--project", PBW, "--agent", "gpufoliage")
    assert rc == 0 and out["result"] is False


def test_the_lease_is_not_reaped_before_its_ttl(capsys, clock, editors):
    pbw = editors["projectbrokenwings"]
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] += 590
    out = _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing
    assert out["exclusive"]["agent"] == "gpufoliage"


def test_a_waiting_agent_gets_an_editor_without_the_stale_pie(capsys, clock, editors):
    """The next agent's acquire is what notices; PIE is stopped BEFORE it is granted the editor."""
    pbw = editors["projectbrokenwings"]
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] += 601
    _lease_pie(capsys, PBW, "next")
    assert pbw.stops == 1 and not pbw.playing
    assert co.status(PBW)["exclusive"]["agent"] == "next"


# ---- orphan: no holder at all ------------------------------------------------------------------

def test_an_orphan_pie_with_no_lease_is_reaped_after_the_idle_window(capsys, clock, editors):
    """`pie start` without a lease, then the agent stops. Nobody holds anything to age out."""
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "loner")
    clock["t"] += 300
    _status(capsys, PBW)
    assert pbw.playing, "reaped inside the idle window"

    clock["t"] += 301
    # Any uap call against the project may notice -- here another agent's ordinary rc call. The
    # reaper runs BEFORE that call counts as activity, and before it runs.
    rc, out = _run(capsys, "rc", "IsInPIE", "--project", PBW, "--agent", "someone")
    assert pbw.stops == 1
    assert rc == 0 and out["result"] is False
    reap = co.status(PBW)["last_reap"]
    assert reap["why"] == "stale_pie_reaped" and reap["cause"] == "orphan"
    assert reap["pie_started_by"] == "loner"


def test_uap_activity_keeps_an_unleased_session_alive(capsys, clock, editors):
    """An agent driving its PIE with exec / rc alone is working: it must not be reaped."""
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "busy")
    for _ in range(4):
        clock["t"] += 400
        rc, _o = _run(capsys, "rc", "IsInPIE", "--project", PBW, "--agent", "busy")
        assert rc == 0
    _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing


def test_a_call_in_flight_vetoes_the_reaper(capsys, clock, editors):
    """A long `sample` / `input hold` makes no further calls while it runs, yet is driving PIE."""
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "sampler")
    co._acquire_filelock(PBW)
    try:
        st = co._load(PBW)
        st.setdefault("inflight", {})["424242"] = {"verb": "sample", "at": clock["t"]}
        co._save(PBW, st)
    finally:
        co._release_filelock(PBW)
    clock["t"] += 900
    _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing


# ---- never a human's PIE ------------------------------------------------------------------------

def test_a_humans_pie_is_reported_never_stopped(capsys, clock, editors):
    """The holder aged out, but the PIE running is one uap never started (a human pressed Play)."""
    pbw = editors["projectbrokenwings"]
    _lease_pie(capsys, PBW, "gpufoliage")
    pbw.start()                                      # human, no uap record
    clock["t"] += 601
    out = _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing
    assert out["unowned_pie"]["why"] == "unowned_pie_left_running"
    assert "did NOT stop it" in out["unowned_pie"]["message"]
    assert out["last_reap"] is None


def test_a_human_session_after_ours_is_not_mistaken_for_ours(capsys, clock, editors):
    """uap started PIE; a human stopped it in the editor and later pressed Play themselves. The
    record still says uap started a session, but the RUNNING one started much later."""
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "loner")
    clock["t"] += 200
    pbw.playing = False                              # human hits Stop (no uap call)
    clock["t"] += 200
    pbw.start()                                      # ...and Play
    clock["t"] += 700
    out = _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing
    assert out["unowned_pie"] is not None
    assert out["pie_session"] is None                # our record is provably stale: dropped


def test_an_editor_that_cannot_date_its_session_is_never_reaped(capsys, clock, editors):
    """An older plugin without GetPIEElapsedSeconds: uap cannot prove the session is its own."""
    pbw = editors["projectbrokenwings"]
    pbw.elapsed_verb = False
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] += 601
    out = _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing
    assert out["unowned_pie"] is not None


# ---- where it must not fire ---------------------------------------------------------------------

def test_a_live_holder_is_never_reaped(capsys, clock, editors):
    pbw = editors["projectbrokenwings"]
    _lease_pie(capsys, PBW, "worker")
    _pie_start(capsys, PBW, "worker")
    for _ in range(5):
        clock["t"] += 500
        _run(capsys, "lease", "heartbeat", "--agent", "worker", "--project", PBW)
        _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing


def test_never_during_a_rebuild(capsys, clock, editors):
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "loner")
    assert co.acquire(PBW, "exclusive", reason="rebuild", agent="rebuild-1", pid=0,
                      wait=0)["granted"]
    clock["t"] += 601            # idle past the window, but a rebuild owns the editor
    _status(capsys, PBW)
    assert pbw.stops == 0


def test_the_escape_hatch_turns_it_off(capsys, clock, editors, monkeypatch):
    pbw = editors["projectbrokenwings"]
    monkeypatch.setenv("UAP_PIE_REAPER", "0")
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] += 3600
    _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing


def test_a_confirmed_pie_stop_clears_the_record(capsys, clock, editors):
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "tidy")
    assert co.status(PBW)["pie_session"]["agent"] == "tidy"
    rc, _o = _run(capsys, "pie", "stop", "--project", PBW, "--agent", "tidy")
    assert rc == 0
    assert co.status(PBW)["pie_session"] is None
    pbw.start()                                      # a human's session later: not ours
    clock["t"] += 3600
    _status(capsys, PBW)
    assert pbw.stops == 1                            # only the explicit stop above


# ---- the machine: the OTHER project's calls reap it ---------------------------------------------

def test_the_other_projects_pie_start_reaps_a_stale_session_holding_the_machine(
        capsys, clock, editors):
    """The cost that made this urgent: PBW's abandoned PIE held the machine, so School's Out could
    not play. PBW's own agents are gone, so it is School's Out's call that has to notice."""
    pbw, sovr = editors["projectbrokenwings"], editors["schoolsout"]
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] += 601
    rc, out = _run(capsys, "pie", "start", "--project", SOVR, "--agent", "sprayhub")
    assert rc == 0 and out["playing"], out
    assert pbw.stops == 1 and not pbw.playing
    assert sovr.playing
    assert co.machine_status()["holder"]["project"] == "schoolsout"


def test_game_clients_are_reported_never_killed(capsys, clock, editors, monkeypatch):
    """A reaped session's project still has standalone clients up: uap did not necessarily launch
    them, so they are named and left running (and keep the machine on their own)."""
    pbw = editors["projectbrokenwings"]
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    killed = []
    monkeypatch.setattr(co.os, "kill", lambda *a: killed.append(a), raising=False)
    monkeypatch.setattr(co, "_game_client_probe", lambda: {"projectbrokenwings": [4242]})
    clock["t"] += 601
    out = _status(capsys, PBW)
    assert pbw.stops == 1
    assert out["last_reap"]["game_clients_left_running"] == [4242]
    assert killed == []
    holder = out["machine"]["holder"]
    assert holder["agent"] == co.GAME_CLIENTS_AGENT and holder["pids"] == [4242]


# ---- two callers at once ------------------------------------------------------------------------

def test_racing_callers_stop_pie_exactly_once(capsys, clock, editors, monkeypatch):
    """Every uap call may run the reaper. Eight at the same instant: one stop, one record.

    Runs on REAL time: a fake clock far from the lock files' mtimes makes every waiter break the
    filelock as stale (`_FILELOCK_STALE`), which would test nothing. So the session is set up
    701s in the past and the race happens now.
    """
    pbw = editors["projectbrokenwings"]
    clock["t"] = time.time() - 701
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] = time.time()
    monkeypatch.setattr(co, "_now", time.time)

    gate = threading.Barrier(8)
    real_stop = co._pie_stopper
    entered = []

    def slow_stop(project):
        entered.append(project)
        threading.Event().wait(0.2)       # a real confirmed stop takes a moment
        return real_stop(project)

    monkeypatch.setattr(co, "_pie_stopper", slow_stop)
    results = []

    def worker():
        gate.wait()
        results.append(co.reap_stale_pie(PBW))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(entered) == 1
    assert pbw.stops == 1
    assert sum(1 for r in results if r and r.get("why") == "stale_pie_reaped") == 1
    assert co.status(PBW)["last_reap"]["why"] == "stale_pie_reaped"


def test_a_stop_that_is_not_confirmed_is_recorded_and_retried(capsys, clock, editors, monkeypatch):
    pbw = editors["projectbrokenwings"]
    _lease_pie(capsys, PBW, "gpufoliage")
    _pie_start(capsys, PBW, "gpufoliage")
    clock["t"] += 601
    real_stop = co._pie_stopper
    monkeypatch.setattr(co, "_pie_stopper",
                        lambda project: {"stopped": False, "error": "PIE still in progress"})
    out = _status(capsys, PBW)
    assert out["last_reap"]["why"] == "stale_pie_reap_failed"
    assert out["pie_session"] is not None and pbw.playing
    monkeypatch.setattr(co, "_pie_stopper", real_stop)
    clock["t"] += 16                                 # past the per-project probe interval
    out = _status(capsys, PBW)
    assert out["last_reap"]["why"] == "stale_pie_reaped" and not pbw.playing


def test_a_session_superseded_mid_probe_is_left_alone(capsys, clock, editors, monkeypatch):
    """Somebody took the lease (or started a new session) between the probe and the stop."""
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "loner")
    clock["t"] += 601
    monkeypatch.setattr(co, "_claim_still_ours", lambda *a: False)
    out = _status(capsys, PBW)
    assert pbw.stops == 0 and pbw.playing
    assert out["last_reap"] is None and out["unowned_pie"] is None
    assert out.get("reaping") is None                # the claim was given back


def test_a_second_pie_start_onto_a_running_session_keeps_it_reapable(capsys, clock, editors):
    """`pie start` twice: the editor keeps the FIRST session. Its start must still match."""
    pbw = editors["projectbrokenwings"]
    _pie_start(capsys, PBW, "twice")
    first_started = pbw.started_at
    clock["t"] += 120
    pbw.start = lambda: None                         # already playing: the request is a no-op
    _pie_start(capsys, PBW, "twice")
    assert pbw.started_at == first_started
    clock["t"] += 601
    out = _status(capsys, PBW)
    assert pbw.stops == 1 and out["last_reap"]["why"] == "stale_pie_reaped"


_RACE_WORKER = r'''
import json, os, pathlib, sys, time
import unreal_agent_player.coordination as co
d = pathlib.Path(sys.argv[1])

def probe(project):
    f = d / "playing"
    if not f.exists():
        return {"in_progress": False, "started_at": None}
    return {"in_progress": True, "started_at": float(f.read_text())}

def stop(project):
    (d / f"stop-{os.getpid()}").write_text("x")
    time.sleep(0.3)                       # a real confirmed stop takes a moment
    try:
        (d / "playing").unlink()
    except FileNotFoundError:
        pass
    return {"stopped": True}

co._pie_session_probe = probe
co._pie_stopper = stop
while not (d / "go").exists():
    time.sleep(0.002)
print(json.dumps(co.reap_stale_pie("ProjectBrokenWings")))
'''


def test_racing_processes_stop_pie_exactly_once(tmp_path, monkeypatch):
    """The same race across real processes, which is what concurrent agents actually are."""
    import subprocess
    import sys

    monkeypatch.delenv("UAP_PIE_REAPER", raising=False)
    monkeypatch.delenv("UAP_PIE_REAP_IDLE", raising=False)
    now = time.time()
    co.note_pie_started(PBW, agent="loner", requested_at=now - 700, confirmed_at=now - 698)
    co._acquire_filelock(PBW)
    try:
        st = co._load(PBW)
        st["last_uap_at"] = now - 700
        co._save(PBW, st)
    finally:
        co._release_filelock(PBW)
    shared = tmp_path / "editor"
    shared.mkdir()
    (shared / "playing").write_text(str(now - 699))
    script = tmp_path / "worker.py"
    script.write_text(_RACE_WORKER, encoding="utf-8")
    procs = [subprocess.Popen([sys.executable, str(script), str(shared)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(6)]
    time.sleep(1.5)                       # let every worker import and reach the gate
    (shared / "go").write_text("go")
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    results = [json.loads(o.strip().splitlines()[-1]) for o, _e in outs]

    assert len(list(shared.glob("stop-*"))) == 1, results
    assert sum(1 for r in results if r and r.get("why") == "stale_pie_reaped") == 1
    assert co.status(PBW)["last_reap"]["why"] == "stale_pie_reaped"
