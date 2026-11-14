"""Rebuild broker: one coalesced restart serves every pending request.

The restart command is MOCKED everywhere except `test_run_command_real_process`, which runs a
trivial Python child (never a restart script). The runner is driven in-process (`serve`) against a
fake clock, so no test waits out a real batching window and no editor is touched.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from unreal_agent_player import cli
from unreal_agent_player import coordination as coord
from unreal_agent_player import rebuild_broker as rb

P = "BrokerProj"


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(float(s), 0.01)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(rb, "_now", c.now)
    monkeypatch.setattr(rb, "_sleep", c.sleep)
    monkeypatch.setattr(rb, "SERVE_POLL", 60.0)      # fewer fake-clock iterations, same logic
    return c


@pytest.fixture
def proj_root(tmp_path, monkeypatch):
    """A project root with a Restart-Editor.ps1, pinned like uap.ps1 pins it."""
    root = tmp_path / "Proj"
    (root / "Saved" / "Logs").mkdir(parents=True)
    (root / "Restart-Editor.ps1").write_text("# never run by these tests\n", encoding="utf-8")
    monkeypatch.setenv("UAP_UPROJECT", str(root / "BrokerProj.uproject"))
    monkeypatch.delenv("UAP_REBUILD_COMMAND", raising=False)
    monkeypatch.delenv("UAP_REBUILD_WINDOW", raising=False)
    monkeypatch.delenv("UAP_REBUILD_RETRY", raising=False)
    return root


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(rb, "spawn_runner", lambda project: calls.append(project) or 4242)
    return calls


class FakeRestart:
    """The mocked restart: returns the scripted exit codes in order (last one repeats) and can run
    a hook mid-run (to file a request while a rebuild is running)."""

    def __init__(self, clock: Clock, codes=(0,), duration: float = 90.0, during=None):
        self.clock, self.codes, self.duration, self.during = clock, list(codes), duration, during
        self.calls = []

    def __call__(self, cmd, env, log, heartbeat):
        self.calls.append({"cmd": cmd, "env": env, "at": self.clock.t, "log": str(log)})
        heartbeat()
        if self.during:
            hook, self.during = self.during, None
            self.clock.t += 1          # the late request arrives a moment after the run started
            hook()
        self.clock.t += self.duration
        code = self.codes[min(len(self.calls) - 1, len(self.codes) - 1)]
        return code


def _state():
    return json.loads(rb._state_path(P).read_text(encoding="utf-8"))


# --- request -------------------------------------------------------------------------------

def test_request_needs_agent(proj_root, spawns, clock):
    res = rb.request(P, agent="", task="t")
    assert res["ok"] is False and "--agent" in res["error"]
    assert spawns == []


def test_request_needs_a_command(monkeypatch, tmp_path, spawns, clock):
    monkeypatch.delenv("UAP_UPROJECT", raising=False)
    monkeypatch.delenv("UAP_REBUILD_COMMAND", raising=False)
    res = rb.request(P, agent="a1", task="t")
    assert res["ok"] is False and "no rebuild command" in res["error"]
    assert not rb._state_path(P).exists()


def test_request_records_and_starts_one_runner(proj_root, spawns, clock):
    r1 = rb.request(P, agent="a1", task="17tm1 header change")
    r2 = rb.request(P, agent="a2", task="17tm2 other change")
    assert r1["ok"] and r1["runner_started"] is True
    assert r2["ok"] and r2["runner_started"] is False          # the claim is still fresh
    assert spawns == [P]
    st = _state()
    assert [q["agent"] for q in st["requests"]] == ["a1", "a2"]
    assert all(q["served_by"] is None for q in st["requests"])
    assert r2["pending"] == 2 and r1["fires_in_s"] == 240


def test_default_command_is_the_projects_restart_script(proj_root):
    cmd = rb.rebuild_command()
    assert cmd[0] == "powershell" and cmd[cmd.index("-File") + 1] == str(proj_root / "Restart-Editor.ps1")
    assert cmd[cmd.index("-LeaseWaitSeconds") + 1] == "3600"


def test_env_command_overrides(proj_root, monkeypatch):
    monkeypatch.setenv("UAP_REBUILD_COMMAND", "my-restart --now")
    assert rb.rebuild_command() == "my-restart --now"


# --- the runner ------------------------------------------------------------------------------

def test_one_restart_serves_every_pending_request(proj_root, spawns, clock):
    t0 = clock.t
    rb.request(P, agent="a1", task="one")
    clock.t += 30
    rb.request(P, agent="a2", task="two")
    clock.t += 30
    rb.request(P, agent="a3", task="three")
    fake = FakeRestart(clock)
    out = rb.serve(P, run=fake)
    assert out == {"ok": True, "served_runs": 1}
    assert len(fake.calls) == 1                                 # ONE restart for three requests
    call = fake.calls[0]
    assert call["at"] >= t0 + 240                               # the window from the FIRST request
    assert call["env"]["UAP_AGENT_ID"] == coord.BROKER_AGENT     # -> requested_by on its lease
    assert "3 request(s)" in call["env"]["UAP_TASK"]
    st = _state()
    run_id = st["runs"][-1]["id"]
    assert {q["served_by"] for q in st["requests"]} == {run_id}
    assert st["runner"] is None                                 # runner exits when nothing pends
    for a in ("a1", "a2", "a3"):
        w = rb.wait(P, agent=a, seconds=0)
        assert w["ok"] is True and w["done"] and w["result"] == "ok" and w["run_id"] == run_id
        assert w["served"] == 3 and w["started_at"] >= w["requested_at"]


def test_editor_down_fires_at_once(proj_root, spawns, clock, monkeypatch):
    monkeypatch.setattr(coord, "_editor_probe", lambda project: False)
    t0 = clock.t
    rb.request(P, agent="a1", task="x")
    fake = FakeRestart(clock)
    rb.serve(P, run=fake)
    assert fake.calls[0]["at"] - t0 < 1
    assert _state()["runs"][-1]["trigger"] == "editor_down"


def test_request_during_a_rebuild_goes_to_the_next_batch(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="first")
    fake = FakeRestart(clock, duration=100,
                       during=lambda: rb.request(P, agent="late", task="arrived mid-rebuild"))
    out = rb.serve(P, run=fake)
    assert out["served_runs"] == 2 and len(fake.calls) == 2
    st = _state()
    first, second = st["runs"][-2], st["runs"][-1]
    by = {q["agent"]: q["served_by"] for q in st["requests"]}
    assert by["a1"] == first["id"] and by["late"] == second["id"]
    # A rebuild that started BEFORE the late request never counts for it ...
    late = next(q for q in st["requests"] if q["agent"] == "late")
    assert first["started_at"] < late["requested_at"] <= second["started_at"]
    # ... and the second restart is not back to back: a full window after the first finished.
    assert second["started_at"] >= first["finished_at"] + 240
    assert rb.wait(P, agent="late", seconds=0)["run_id"] == second["id"]


def test_preflight_failure_reports_note_and_editor_up(proj_root, spawns, clock):
    note = proj_root / "Saved" / "Logs" / "RestartEditor-PreflightFailure.txt"
    note.write_text("Restart-Editor.ps1 compile PREFLIGHT FAILED ... STILL RUNNING.\n"
                    "  Foo.cpp(12): error C2664: bad\n      opened in: CL 5170 by x\n",
                    encoding="utf-8")
    rb.request(P, agent="a1", task="x")
    rb.serve(P, run=FakeRestart(clock, codes=(7,)))
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["ok"] is False and w["done"] and w["result"] == "failed" and w["exit_code"] == 7
    assert w["editor_up"] is True and w["note"] == str(note)
    assert "Foo.cpp(12): error C2664" in w["note_excerpt"] and "CL 5170" in w["note_excerpt"]
    assert "hint" in w


def test_failed_after_kill_points_at_the_down_note(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")
    rb.serve(P, run=FakeRestart(clock, codes=(1,)))
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["result"] == "failed" and w["editor_up"] is False
    assert w["note"].endswith("RestartEditor-LastFailure.txt")


def test_slow_boot_is_ok_with_a_warning(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")
    rb.serve(P, run=FakeRestart(clock, codes=(6,)))
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["ok"] is True and w["result"] == "ok" and "warning" in w


@pytest.mark.parametrize("code", [2, 3, 4])
def test_untouched_answers_are_retried_not_reported(proj_root, spawns, clock, code):
    rb.request(P, agent="a1", task="x")
    fake = FakeRestart(clock, codes=(code, 0))
    rb.serve(P, run=fake, retry=60)
    assert len(fake.calls) == 2
    assert fake.calls[1]["at"] >= fake.calls[0]["at"] + 90 + 60   # duration + retry delay
    st = _state()
    assert [r["result"] for r in st["runs"]] == ["deferred", "ok"]
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["ok"] is True and w["run_id"] == st["runs"][-1]["id"]


def test_gives_up_after_max_deferrals(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")
    fake = FakeRestart(clock, codes=(2,))
    rb.serve(P, run=fake, retry=10, max_deferrals=3)
    assert len(fake.calls) == 3
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["result"] == "failed" and "gave up after 3" in w["error"]


def test_runner_exits_if_another_is_alive(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")
    with rb._locked(P):
        st = rb._load(P)
        st["runner"] = {"pid": os.getppid(), "state": "window", "heartbeat_at": clock.t}
        rb._save(P, st)
    fake = FakeRestart(clock)
    out = rb.serve(P, run=fake)
    assert out["already_running"] == os.getppid() and fake.calls == []


def test_runner_crash_clears_its_claim(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")

    def boom(*a):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        rb.serve(P, run=boom)
    assert _state()["runner"] is None


def test_command_error_fails_the_batch(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")

    def boom(*a):
        raise OSError("powershell not found")

    rb.serve(P, run=boom)
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["result"] == "failed" and "powershell not found" in w["error"]


# --- wait ------------------------------------------------------------------------------------

def test_wait_without_request(proj_root, clock):
    w = rb.wait(P, agent="nobody", seconds=0)
    assert w["ok"] is False and w["not_requested"] is True


def test_wait_still_waiting_keeps_the_request(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")
    w = rb.wait(P, agent="a1", seconds=5)
    assert w["ok"] is False and w["still_waiting"] is True and w["done"] is False
    assert "uap rebuild wait --agent a1" in w["resume"]
    assert 0 <= w["fires_in_s"] <= 240
    assert _state()["requests"][0]["served_by"] is None


def test_wait_replaces_a_dead_runner(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")
    assert spawns == [P]
    with rb._locked(P):
        st = rb._load(P)
        st["runner"] = {"pid": 999_999_999, "state": "window", "heartbeat_at": clock.t - 1000}
        rb._save(P, st)
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["still_waiting"] and w.get("runner_started") is True
    assert spawns == [P, P]


def test_wait_returns_the_latest_requests_result(proj_root, spawns, clock):
    rb.request(P, agent="a1", task="x")
    rb.serve(P, run=FakeRestart(clock))
    clock.t += 10
    rb.request(P, agent="a1", task="x again")          # new code after that rebuild
    w = rb.wait(P, agent="a1", seconds=0)
    assert w["still_waiting"] is True                     # the earlier run does NOT count


# --- lease: the broker's restart goes to the head of the queue, never past the holder --------

def test_broker_restart_jumps_the_queue_but_waits_for_the_holder():
    proj = "QueueProj"
    assert coord.acquire(proj, "exclusive", reason="pie", agent="holder", wait=0)["granted"]
    # A verifier queues (parked) behind the holder.
    v = coord.acquire(proj, "exclusive", reason="pie", agent="verifier", wait=300, max_block=0)
    assert v["still_waiting"] is True
    # The broker's restart arrives later: it waits for the holder (never preempts) ...
    b = coord.acquire(proj, "exclusive", reason="relaunch-preflight", agent="rebuild-1",
                      requested_by=coord.BROKER_AGENT, wait=0)
    assert b["granted"] is False
    st = coord.status(proj)
    assert st["exclusive"]["agent"] == "holder"
    coord.release(proj, agent="holder")
    # ... and then goes BEFORE the verifier that queued earlier.
    b = coord.acquire(proj, "exclusive", reason="relaunch-preflight", agent="rebuild-1",
                      requested_by=coord.BROKER_AGENT, wait=0)
    assert b["granted"] is True
    assert coord.status(proj)["queue"] == ["verifier"]


def test_ordinary_relaunch_keeps_fifo_while_the_editor_is_up():
    proj = "FifoProj"
    assert coord.acquire(proj, "exclusive", reason="pie", agent="holder", wait=0)["granted"]
    coord.acquire(proj, "exclusive", reason="pie", agent="verifier", wait=300, max_block=0)
    coord.release(proj, agent="holder")
    b = coord.acquire(proj, "exclusive", reason="relaunch-preflight", agent="rebuild-2",
                      requested_by="some-agent", wait=0)
    assert b["granted"] is False                           # not the broker: no queue jump


# --- CLI ---------------------------------------------------------------------------------------

def test_cli_request_wait_status(proj_root, spawns, capsys, clock):
    assert cli.main(["rebuild", "request", "--project", P, "--agent", "cli-a",
                     "--task", "17tm4670a3d test"]) == 0
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["requested"] is True and body["agent"] == "cli-a"
    assert cli.main(["rebuild", "wait", "--project", P, "--agent", "cli-a", "--seconds", "0"]) == 1
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["still_waiting"] is True
    assert cli.main(["rebuild", "status", "--project", P]) == 0
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [q["agent"] for q in body["pending"]] == ["cli-a"]
    assert body["pending"][0]["task"] == "17tm4670a3d test"


def test_cli_wait_ok_exit_zero(proj_root, spawns, capsys, clock):
    rb.request(P, agent="cli-b", task="x")
    rb.serve(P, run=FakeRestart(clock))
    assert cli.main(["rebuild", "wait", "--project", P, "--agent", "cli-b", "--seconds", "0"]) == 0
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["result"] == "ok"


def test_help_names_the_broker(capsys):
    cli.main(["help", "rebuild"])
    out = capsys.readouterr().out
    assert "uap rebuild request" in out and "uap rebuild wait" in out


# --- the real process plumbing (no restart script: a trivial Python child) --------------------

def test_run_command_real_process(tmp_path):
    beats = []
    log = tmp_path / "run.log"
    code = rb.run_command([sys.executable, "-c", "print('restart says hi'); raise SystemExit(7)"],
                          dict(os.environ), log, lambda: beats.append(1))
    assert code == 7 and beats
    assert "restart says hi" in log.read_text(encoding="utf-8", errors="replace")


def test_spawn_runner_real_process_exits_when_idle(tmp_path):
    """The detached runner really starts, finds nothing pending, and exits cleanly."""
    pid = rb.spawn_runner(P)
    assert pid
    deadline = time.time() + 60
    while time.time() < deadline and coord._pid_alive(pid):
        time.sleep(0.2)
    assert not coord._pid_alive(pid)
    log = (rb.logs_dir() / f"{rb._key(P)}-runner.log").read_text(encoding="utf-8", errors="replace")
    assert '"served_runs": 0' in log
