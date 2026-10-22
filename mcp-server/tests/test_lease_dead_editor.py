"""A pie/level lease is void when the editor it protects is gone.

2026-10-22: the PBW editor died (out of memory) while agent `respawnfix` held an exclusive `pie`
lease. The queued Restart-Editor could not relaunch it until the lease aged out on its 600s TTL --
and the holder kept refreshing that TTL with its own `uap` calls. These tests pin the fix and,
just as importantly, every case where it must NOT fire.

The process probe is always faked: a test must never depend on (or touch) a real editor.
"""
import json
import os
import time

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player import cli as cli_mod
from unreal_agent_player.cli import main


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    monkeypatch.delenv("UAP_EDITOR_LIVENESS", raising=False)
    monkeypatch.setenv("UAP_RC_PORT", "1")        # lease release must never reach a real editor
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)


def _probe(monkeypatch, answer):
    """Pin what the dead-editor probe says; returns the list of projects it was asked about."""
    asked = []

    def fake(project):
        asked.append(project)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(co, "_editor_probe", fake)
    return asked


def _hold(project, reason, agent="HOLDER"):
    return co.acquire(project, "exclusive", reason=reason, agent=agent, pid=0, wait=0)


# ---- eviction: the bug ----------------------------------------------------------------------

def test_pie_lease_is_voided_when_the_editor_is_gone(monkeypatch):
    _probe(monkeypatch, False)
    assert _hold("projectbrokenwings", "pie")["granted"]
    # Someone else asks: the dead editor's lease must not block them.
    r = co.acquire("projectbrokenwings", "exclusive", reason="rebuild", agent="R", wait=0)
    assert r["granted"], r


def test_level_lease_is_voided_too(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "level:/Game/Maps/Utah")
    assert co.status("projectbrokenwings")["exclusive"] is None


def test_eviction_is_recorded_for_status(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="respawnfix")
    st = co.status("projectbrokenwings")
    last = st["last_eviction"]
    assert last["agent"] == "respawnfix"
    assert last["why"] == "editor_not_running"
    assert last["held_reason"] == "pie"
    assert abs(last["evicted_at"] - time.time()) < 5
    assert "lease_evicted: editor_not_running" in last["message"]


def test_status_itself_evicts(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie")
    assert co.status("projectbrokenwings")["exclusive"] is None


def test_wait_if_blocked_evicts_and_passes(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="HOLDER")
    r = co.wait_if_blocked("projectbrokenwings", agent="OTHER", wait=0)
    assert r["blocked"] is False


def test_evicted_holder_gets_one_clear_notice(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="respawnfix")
    co.status("projectbrokenwings")
    note = co.take_eviction_notice("projectbrokenwings", agent="respawnfix")
    assert note["why"] == "editor_not_running"
    # ...consumed on read, and a stranger never gets someone else's note.
    assert co.take_eviction_notice("projectbrokenwings", agent="respawnfix") is None
    assert co.take_eviction_notice("projectbrokenwings", agent="someone-else") is None
    # lease status keeps showing it though.
    assert co.status("projectbrokenwings")["last_eviction"]["agent"] == "respawnfix"


def test_reacquiring_clears_a_pending_notice(monkeypatch):
    asked = _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="A")
    co.status("projectbrokenwings")
    _probe(monkeypatch, True)
    assert _hold("projectbrokenwings", "pie", agent="A")["granted"]
    assert co.take_eviction_notice("projectbrokenwings", agent="A") is None
    assert asked


def test_release_and_heartbeat_report_the_eviction(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="A")
    co.status("projectbrokenwings")
    hb = co.heartbeat("projectbrokenwings", agent="A")
    assert hb["refreshed"] == 0 and hb["lease_evicted"]["why"] == "editor_not_running"
    _hold("projectbrokenwings", "pie", agent="B")
    co.status("projectbrokenwings")
    rel = co.release("projectbrokenwings", agent="B")
    assert rel["released"] is False and rel["lease_evicted"]["agent"] == "B"


def test_eviction_also_frees_the_machine_foreground_lock(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="A")
    assert co.acquire_machine("projectbrokenwings", reason="pie", agent="A", pid=0,
                              wait=0)["acquired"]
    co.status("projectbrokenwings")
    assert co.machine_status()["holder"] is None
    # ...and the OTHER project is not left waiting on a session that no longer exists.
    assert co.acquire_machine("schoolsout", reason="pie", agent="S", wait=0)["acquired"]


def test_eviction_does_not_touch_the_other_projects_machine_hold(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="A")
    co.acquire_machine("schoolsout", reason="pie", agent="S", pid=0, wait=0)
    co.status("projectbrokenwings")
    assert co.machine_status()["holder"]["project"] == "schoolsout"


# ---- must NOT evict -------------------------------------------------------------------------

def test_pie_lease_on_a_running_editor_is_never_evicted(monkeypatch):
    _probe(monkeypatch, True)
    _hold("projectbrokenwings", "pie", agent="A")
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "A"
    assert co.acquire("projectbrokenwings", "exclusive", reason="pie", agent="B",
                      wait=0).get("busy")
    assert co.status("projectbrokenwings")["last_eviction"] is None


def test_unknown_probe_never_evicts(monkeypatch):
    _probe(monkeypatch, None)
    _hold("projectbrokenwings", "pie", agent="A")
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "A"


def test_probe_error_never_evicts(monkeypatch):
    _probe(monkeypatch, RuntimeError("process list unavailable"))
    _hold("projectbrokenwings", "pie", agent="A")
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "A"
    assert co.acquire("projectbrokenwings", "exclusive", reason="pie", agent="B",
                      wait=0).get("busy")


def test_rebuild_lease_is_never_evicted_for_a_dead_editor(monkeypatch):
    """The editor being down IS a rebuild's job."""
    asked = _probe(monkeypatch, False)
    _hold("projectbrokenwings", "rebuild", agent="RB")
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "RB"
    co.acquire("projectbrokenwings", "exclusive", reason="relaunch", agent="RB", pid=0, wait=0)
    assert co.status("projectbrokenwings")["exclusive"]["reason"] == "relaunch"
    assert asked == []                       # never even probed


def test_other_reasons_are_not_editor_bound(monkeypatch):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "input", agent="A")
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "A"
    co.release("projectbrokenwings", agent="A")
    _hold("projectbrokenwings", "", agent="A")
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "A"


def test_mid_rebuild_is_never_evicted(monkeypatch):
    """A rebuild/relaunch hold on the project means the editor being down is the plan."""
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="A")
    state = co._load("projectbrokenwings")
    state["shared"].append({"agent": "RB", "reason": "rebuild", "pid": 0,
                            "acquired_at": time.time(), "heartbeat_at": time.time(), "ttl": 999})
    co._save("projectbrokenwings", state)
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "A"


def test_the_machine_scope_is_never_probed(monkeypatch):
    asked = _probe(monkeypatch, False)
    co.acquire_machine("projectbrokenwings", reason="pie:start", agent="A", pid=0, wait=0)
    assert co.machine_status()["holder"]["agent"] == "A"
    assert asked == []


# ---- rebuild goes first when the editor is down ---------------------------------------------

def _waiters(project):
    return [w["agent"] for w in co._load(project)["waiters"]]


def _queue_up(project, *entries):
    state = co._load(project)
    for agent, reason in entries:
        co._enqueue(state, agent, "exclusive", reason)
    co._save(project, state)


def test_rebuild_waiter_goes_ahead_of_pie_waiters_when_the_editor_is_down(monkeypatch):
    _probe(monkeypatch, True)
    _hold("projectbrokenwings", "pie", agent="HOLDER")
    _queue_up("projectbrokenwings", ("P1", "pie"), ("L1", "level:/x"), ("RB", "rebuild"))
    assert _waiters("projectbrokenwings") == ["P1", "L1", "RB"]
    _probe(monkeypatch, False)
    co.status("projectbrokenwings")
    assert _waiters("projectbrokenwings") == ["RB", "P1", "L1"]


def test_with_the_editor_up_the_queue_stays_fifo(monkeypatch):
    _probe(monkeypatch, True)
    _hold("projectbrokenwings", "pie", agent="HOLDER")
    _queue_up("projectbrokenwings", ("P1", "pie"), ("RB", "rebuild"))
    co.status("projectbrokenwings")
    assert _waiters("projectbrokenwings") == ["P1", "RB"]


def test_unknown_editor_state_keeps_fifo(monkeypatch):
    _probe(monkeypatch, None)
    _hold("projectbrokenwings", "pie", agent="HOLDER")
    _queue_up("projectbrokenwings", ("P1", "pie"), ("RB", "rebuild"))
    co.status("projectbrokenwings")
    assert _waiters("projectbrokenwings") == ["P1", "RB"]


def test_queued_rebuild_gets_the_lease_before_an_earlier_pie_waiter(monkeypatch):
    """The 2026-10-22 shape end to end: dead editor, stale pie holder, pie waiter queued first."""
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="respawnfix")
    _queue_up("projectbrokenwings", ("P1", "pie"))
    r = co.acquire("projectbrokenwings", "exclusive", reason="rebuild", agent="rebuild-1",
                   wait=0)
    assert r["granted"], r
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "rebuild-1"
    assert _waiters("projectbrokenwings") == ["P1"]


def test_rebuild_priority_does_not_probe_when_there_is_nothing_to_reorder(monkeypatch):
    asked = _probe(monkeypatch, False)
    _hold("projectbrokenwings", "rebuild", agent="RB")
    _queue_up("projectbrokenwings", ("RB2", "rebuild"), ("P1", "pie"))
    co.status("projectbrokenwings")
    assert asked == []


# ---- the CLI surface ------------------------------------------------------------------------

class _FakeExec:
    ran = 0

    def __init__(self, **kw):
        pass

    def exec_python(self, code):
        type(self).ran += 1
        return {"success": True, "result": "ok", "output": []}


def test_evicted_holders_next_call_says_why_then_the_one_after_proceeds(monkeypatch, capsys):
    _FakeExec.ran = 0
    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)
    _probe(monkeypatch, True)
    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "respawnfix",
                 "--project", "projectbrokenwings", "--wait", "0"]) == 0
    capsys.readouterr()
    _probe(monkeypatch, False)                       # the editor dies

    rc = main(["exec", "print(1)", "--project", "projectbrokenwings", "--agent", "respawnfix"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and _FakeExec.ran == 0
    assert out["lease_evicted"] is True and out["why"] == "editor_not_running"
    assert "lease_evicted: editor_not_running" in out["error"]

    # Told once: the next call goes through (it has no lease to be blocked by any more).
    assert main(["exec", "print(1)", "--project", "projectbrokenwings",
                 "--agent", "respawnfix"]) == 0
    assert _FakeExec.ran == 1


def test_other_agents_are_not_blocked_by_the_dead_editors_lease(monkeypatch):
    _FakeExec.ran = 0
    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)
    _probe(monkeypatch, False)
    assert _hold("projectbrokenwings", "pie", agent="respawnfix")["granted"]
    assert main(["exec", "print(1)", "--project", "projectbrokenwings", "--agent", "other"]) == 0
    assert _FakeExec.ran == 1


def test_live_editor_still_blocks_other_agents_through_the_cli(monkeypatch):
    _FakeExec.ran = 0
    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)
    _probe(monkeypatch, True)
    _hold("projectbrokenwings", "pie", agent="respawnfix")
    assert main(["exec", "print(1)", "--project", "projectbrokenwings", "--agent", "other"]) == 1
    assert _FakeExec.ran == 0


def test_lease_status_shows_the_eviction(monkeypatch, capsys):
    _probe(monkeypatch, False)
    _hold("projectbrokenwings", "pie", agent="respawnfix")
    assert main(["lease", "status", "--project", "projectbrokenwings"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["exclusive"] is None
    assert out["last_eviction"]["why"] == "editor_not_running"
    assert out["last_eviction"]["agent"] == "respawnfix"


# ---- the real probe, against a faked process list -------------------------------------------

PBW = r'"E:\Engine\Binaries\Win64\UnrealEditor.exe" "E:\Games\ProjectBrokenWings\ProjectBrokenWings.uproject" -log'
SOVR = r'"E:\Engine\Binaries\Win64\UnrealEditor.exe" "E:\Games\SchoolsOutVR\SchoolsOut.uproject"'
PBW_GAME = (r'"E:\Engine\Binaries\Win64\UnrealEditor.exe" "E:\Games\ProjectBrokenWings'
            r'\ProjectBrokenWings.uproject" -game -windowed')


@pytest.fixture
def procs(monkeypatch):
    """Fake the process list the real probe reads. Returns the list to fill + a call counter."""
    monkeypatch.setattr(co, "_editor_probe", co.editor_running)   # undo conftest's stub
    co._EDITOR_CACHE.clear()
    state = {"list": [], "calls": 0, "raise": None}

    def fake():
        state["calls"] += 1
        if state["raise"]:
            raise state["raise"]
        return list(state["list"])
    monkeypatch.setattr(co, "_enumerate_editors", fake)
    yield state
    co._EDITOR_CACHE.clear()


def _p(cmdline, pid=100):
    return {"pid": pid, "exe": r"E:\Engine\Binaries\Win64\UnrealEditor.exe", "cmdline": cmdline}


def test_probe_sees_its_own_projects_editor(procs):
    procs["list"] = [_p(PBW)]
    assert co.editor_running("ProjectBrokenWings") is True


def test_probe_matches_the_project_case_insensitively(procs):
    procs["list"] = [_p(PBW.replace("ProjectBrokenWings.uproject", "PROJECTBROKENWINGS.UPROJECT"))]
    assert co.editor_running("projectbrokenwings") is True
    co._EDITOR_CACHE.clear()
    assert co.editor_running("PROJECTBrokenWings") is True


def test_probe_is_false_when_no_editor_runs(procs):
    procs["list"] = []
    assert co.editor_running("projectbrokenwings") is False


def test_the_other_projects_editor_never_counts(procs):
    procs["list"] = [_p(SOVR)]
    assert co.editor_running("projectbrokenwings") is False
    assert co.editor_running("schoolsout") is True
    co._EDITOR_CACHE.clear()
    procs["list"] = [_p(PBW)]
    assert co.editor_running("schoolsout") is False


def test_both_editors_open_each_project_sees_only_its_own(procs):
    procs["list"] = [_p(PBW, 1), _p(SOVR, 2)]
    assert co.editor_running("projectbrokenwings") is True
    assert co.editor_running("schoolsout") is True


def test_a_standalone_game_client_is_not_the_editor(procs):
    procs["list"] = [_p(PBW_GAME)]
    assert co.editor_running("projectbrokenwings") is False


def test_a_game_client_does_not_hide_the_real_editor(procs):
    procs["list"] = [_p(PBW_GAME, 1), _p(PBW, 2)]
    assert co.editor_running("projectbrokenwings") is True


def test_project_name_is_matched_exactly_not_by_substring(procs):
    procs["list"] = [_p(r'"E:\Engine\UnrealEditor.exe" "E:\g\ProjectBrokenWingsSandbox.uproject"')]
    assert co.editor_running("projectbrokenwings") is False


def test_path_with_spaces_and_forward_slashes(procs):
    procs["list"] = [_p('"C:/Program Files/UE/UnrealEditor.exe" '
                        '"D:/My Games/ProjectBrokenWings/ProjectBrokenWings.uproject"')]
    assert co.editor_running("projectbrokenwings") is True


def test_unreadable_command_line_is_unknown_not_dead(procs):
    """An editor we cannot identify (protected / elevated) might be this project's."""
    procs["list"] = [_p(None)]
    assert co.editor_running("projectbrokenwings") is None


def test_a_readable_match_wins_over_an_unreadable_neighbour(procs):
    procs["list"] = [_p(None, 1), _p(PBW, 2)]
    assert co.editor_running("projectbrokenwings") is True


def test_enumeration_failure_is_unknown(procs):
    procs["raise"] = OSError("snapshot failed")
    assert co.editor_running("projectbrokenwings") is None


def test_unknown_project_is_unknown(procs):
    procs["list"] = [_p(PBW)]
    assert co.editor_running("") is None
    assert co.editor_running(None) is None
    assert co.editor_running(co.MACHINE_SENTINEL) is None


def test_liveness_can_be_switched_off(procs, monkeypatch):
    procs["list"] = []
    monkeypatch.setenv("UAP_EDITOR_LIVENESS", "0")
    assert co.editor_running("projectbrokenwings") is None


def test_probe_result_is_cached_so_polling_stays_cheap(procs):
    procs["list"] = [_p(PBW)]
    for _ in range(20):
        assert co.editor_running("projectbrokenwings") is True
    assert procs["calls"] == 1


def test_a_failed_probe_is_not_cached(procs):
    procs["raise"] = OSError("transient")
    assert co.editor_running("projectbrokenwings") is None
    procs["raise"] = None
    procs["list"] = [_p(PBW)]
    assert co.editor_running("projectbrokenwings") is True


def test_cache_expires(procs, monkeypatch):
    procs["list"] = [_p(PBW)]
    assert co.editor_running("projectbrokenwings") is True
    real = co._now()
    monkeypatch.setattr(co, "_now", lambda: real + co._EDITOR_UP_CACHE_SECONDS + 1)
    procs["list"] = []
    assert co.editor_running("projectbrokenwings") is False


def test_end_to_end_with_the_real_probe_and_a_faked_process_list(procs):
    """The wiring, not the seam: eviction driven by editor_running itself."""
    procs["list"] = [_p(SOVR)]                       # only the OTHER project's editor is up
    assert co.acquire("projectbrokenwings", "exclusive", reason="pie", agent="A", pid=0,
                      wait=0)["granted"]
    assert co.status("projectbrokenwings")["exclusive"] is None
    co._EDITOR_CACHE.clear()
    procs["list"] = [_p(PBW)]                        # now ours is up
    assert co.acquire("projectbrokenwings", "exclusive", reason="pie", agent="A", pid=0,
                      wait=0)["granted"]
    assert co.status("projectbrokenwings")["exclusive"]["agent"] == "A"


@pytest.mark.skipif(os.name != "nt", reason="Windows process enumeration")
def test_real_enumeration_runs_and_is_read_only():
    """Smoke test of the ctypes path against this machine's real process list (read-only)."""
    found = co._enumerate_editors()
    assert isinstance(found, list)
    for p in found:
        assert p["pid"] > 0 and "unrealeditor" in (p["exe"] or "unrealeditor").lower()
