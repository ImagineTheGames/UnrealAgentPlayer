import time

import unreal_agent_player.coordination as co


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)


def test_exclusive_blocks_second_agent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert co.acquire("proj", "exclusive", reason="rebuild", agent="A", wait=0)["granted"]
    b = co.acquire("proj", "exclusive", reason="pie", agent="B", wait=0)
    assert b.get("busy") and b["holder"]["agent"] == "A"
    co.release("proj", agent="A")
    assert co.acquire("proj", "exclusive", agent="B", wait=0)["granted"]


def test_shared_reads_coexist_but_exclusive_waits(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert co.acquire("proj", "shared", agent="R1", wait=0)["granted"]
    assert co.acquire("proj", "shared", agent="R2", wait=0)["granted"]
    assert co.acquire("proj", "exclusive", agent="W", wait=0).get("busy")  # blocked by readers
    co.release("proj", agent="R1")
    co.release("proj", agent="R2")
    assert co.acquire("proj", "exclusive", agent="W", wait=0)["granted"]
    assert co.acquire("proj", "shared", agent="R1", wait=0).get("busy")  # blocked by writer


def test_stale_holder_evicted_by_ttl(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="DEAD", ttl=1, wait=0)
    state = co._load("proj")
    state["exclusive"]["heartbeat_at"] = time.time() - 999  # ancient
    co._save("proj", state)
    assert co.acquire("proj", "exclusive", agent="FRESH", wait=0)["granted"]


def test_dead_pid_evicted(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(co, "_pid_alive", lambda pid: False)
    co.acquire("proj", "exclusive", agent="GHOST", wait=0)
    assert co.acquire("proj", "exclusive", agent="LIVE", wait=0)["granted"]


def test_wait_if_blocked_passes_when_free(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert co.wait_if_blocked("proj", agent="R", wait=0)["blocked"] is False


def test_wait_if_blocked_times_out_under_exclusive(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="W", wait=0)
    r = co.wait_if_blocked("proj", agent="R", wait=0)
    assert r["blocked"] is True and r["holder"]["agent"] == "W"


def test_generation_bump(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert co.bump_generation("proj") == 1
    assert co.bump_generation("proj") == 2
    assert co.status("proj")["generation"] == 2


def test_release_is_idempotent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "shared", agent="R", wait=0)
    assert co.release("proj", agent="R")["released"] is True
    assert co.release("proj", agent="R")["released"] is False


def test_exclusive_is_reentrant_for_same_agent(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert co.acquire("proj", "exclusive", agent="A", wait=0)["granted"]
    # same agent re-acquiring its own exclusive must not deadlock/busy
    assert co.acquire("proj", "exclusive", agent="A", wait=0)["granted"]


def test_wait_while_rebuild_passes_when_no_rebuild(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", reason="pie", agent="P", wait=0)  # not a rebuild
    assert co.wait_while_rebuild("proj", wait=0)["ok"] is True


def test_wait_while_rebuild_blocks_during_rebuild(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", reason="rebuild", agent="R", wait=0)
    r = co.wait_while_rebuild("proj", wait=0)
    assert r.get("timed_out") and r["holder"]["reason"] == "rebuild"


def test_pid0_hold_survives_liveness_but_expires_on_ttl(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(co, "_pid_alive", lambda pid: False)  # all pids "dead"
    co.acquire("proj", "exclusive", agent="X", pid=0, ttl=999, wait=0)
    # pid==0 -> not evicted by pid-death; another agent is blocked
    assert co.acquire("proj", "exclusive", agent="Y", wait=0).get("busy")
    # ...but a stale heartbeat still reclaims it
    state = co._load("proj")
    state["exclusive"]["heartbeat_at"] = time.time() - 9999
    co._save("proj", state)
    assert co.acquire("proj", "exclusive", agent="Y", wait=0)["granted"]


def test_projects_are_isolated(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("schoolsout", "exclusive", agent="A", wait=0)
    # a different project's editor is unaffected
    assert co.acquire("projectbrokenwings", "exclusive", agent="B", wait=0)["granted"]
