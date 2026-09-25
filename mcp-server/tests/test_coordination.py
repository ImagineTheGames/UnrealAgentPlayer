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


# --- FIFO wait queue (ClickUp 17tm466ft2n) ----------------------------------------------------
# An agent waited through two full 900s caps and lost the lease both times to agents that asked
# later. Each call was correct; the emergent behaviour was starvation. These pin the ordering.


def _queue(project):
    return [w["agent"] for w in co._load(project).get("waiters", [])]


def _register_waiter(project, agent, mode="exclusive", since=None):
    """Put `agent` in the queue the way a blocked `acquire --wait` would."""
    state = co._load(project)
    co._enqueue(state, agent, mode, "test")
    if since is not None:
        for w in state["waiters"]:
            if w["agent"] == agent:
                w["since"] = since
    co._save(project, state)


def test_longest_waiter_is_granted_first(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    for name in ("w1", "w2", "w3"):
        _register_waiter("proj", name)
    assert _queue("proj") == ["w1", "w2", "w3"]
    co.release("proj", agent="HOLDER")
    # A latecomer must NOT jump the whole queue, even though the lease is free right now.
    assert co.acquire("proj", "exclusive", agent="latecomer", wait=0).get("busy")
    # ...and neither may the second waiter.
    assert co.acquire("proj", "exclusive", agent="w2", wait=0).get("busy")
    assert co.acquire("proj", "exclusive", agent="w1", wait=0)["granted"]


def test_busy_reports_queue_position_and_who_is_ahead(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    for name in ("w1", "w2"):
        _register_waiter("proj", name)
    res = co.acquire("proj", "exclusive", agent="w3", wait=0)
    assert res["busy"]
    assert res["queue_position"] == 3
    assert res["queue_ahead"] == ["w1", "w2"]
    assert res["queue_length"] == 3


def test_timed_out_waiter_leaves_the_queue(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    assert co.acquire("proj", "exclusive", agent="GIVER_UP", wait=0).get("busy")
    # It stood in the queue for the duration of its wait, and gave the slot back on the way out.
    assert _queue("proj") == []


def test_dead_waiter_does_not_block_the_queue(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    _register_waiter("proj", "ZOMBIE")
    _register_waiter("proj", "ALIVE")
    state = co._load("proj")
    state["waiters"][0]["heartbeat_at"] = time.time() - 9999   # stopped polling
    co._save("proj", state)
    co.release("proj", agent="HOLDER")
    assert co.acquire("proj", "exclusive", agent="ALIVE", wait=0)["granted"]


def test_waiter_whose_process_is_gone_is_evicted(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    _register_waiter("proj", "ZOMBIE")
    state = co._load("proj")
    state["waiters"][0]["pid"] = 2_000_000_000        # no such process
    co._save("proj", state)
    co.release("proj", agent="HOLDER")
    assert co.acquire("proj", "exclusive", agent="NEWCOMER", wait=0)["granted"]


def test_holder_reacquiring_never_queues_behind_its_own_waiters(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    _register_waiter("proj", "w1")
    assert co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)["granted"]


def test_shared_run_at_the_head_goes_together(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    _register_waiter("proj", "r1", mode="shared")
    _register_waiter("proj", "r2", mode="shared")
    co.release("proj", agent="HOLDER")
    assert co.acquire("proj", "shared", agent="r2", wait=0)["granted"]
    assert co.acquire("proj", "shared", agent="r1", wait=0)["granted"]


def test_exclusive_waiter_ahead_still_blocks_shared_traffic(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    _register_waiter("proj", "REBUILDER", mode="exclusive")
    co.release("proj", agent="HOLDER")
    # Without this, a stream of shared readers starves a queued rebuild forever.
    assert co.acquire("proj", "shared", agent="reader", wait=0).get("busy")
    assert co.acquire("proj", "exclusive", agent="REBUILDER", wait=0)["granted"]


def test_ttl_ageout_still_reclaims_an_abandoned_hold_with_waiters_queued(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="ABANDONED", pid=0, ttl=1, wait=0)
    state = co._load("proj")
    state["exclusive"]["heartbeat_at"] = time.time() - 9999
    co._save("proj", state)
    assert co.acquire("proj", "exclusive", agent="NEXT", wait=0)["granted"]


def test_explicit_release_still_breaks_an_abandoned_lease(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="GHOST", pid=0, wait=0)
    assert co.release("proj", agent="GHOST")["released"]
    assert co.acquire("proj", "exclusive", agent="NEXT", wait=0)["granted"]


def test_status_names_the_queue_in_order(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    co.acquire("proj", "exclusive", agent="HOLDER", pid=0, wait=0)
    for name in ("first", "second"):
        _register_waiter("proj", name)
    assert co.status("proj")["queue"] == ["first", "second"]
