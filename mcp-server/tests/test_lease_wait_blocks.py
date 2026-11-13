"""`lease wait` must BLOCK for the seconds asked, keep FIFO order, and say where it stands.

Regression: after `lease acquire --wait N --max-block M` the original budget N was the only
budget `lease wait --seconds S` honoured. With less than S left (or none) it returned `busy` at
once, and once the budget was gone the queue entry was dropped, so every later `lease wait`
answered `not_queued` instantly. Agents spun, each retry re-queueing at the BACK.
"""
import threading
import time

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player.cli import main


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_RC_PORT", "1")
    monkeypatch.setenv("UAP_EDITOR_LIVENESS", "0")
    monkeypatch.setenv("UAP_GAME_CLIENT_PROBE", "0")
    monkeypatch.setenv("UAP_PIE_REAPER", "0")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.delenv("UAP_MAX_BLOCK", raising=False)
    monkeypatch.setattr(co, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)


def _hold(agent="A"):
    assert co.acquire("demo", "exclusive", reason="pie", agent=agent, pid=0, wait=0)["granted"]


def test_wait_turn_blocks_full_seconds_when_original_budget_is_spent():
    _hold()
    # --wait 0.3 with --max-block 0.2: only 0.1s of budget would be left for the resume.
    r = co.acquire("demo", "exclusive", reason="pie", agent="B", wait=0.3, max_block=0.2)
    assert r["still_waiting"] and r["position"] == 1
    t0 = time.time()
    r = co.wait_turn("demo", agent="B", seconds=0.6)
    assert time.time() - t0 >= 0.55, "wait_turn returned early instead of blocking"
    assert r["busy"] and r["still_waiting"] and r["position"] == 1


def test_lease_wait_after_wait_budget_expired_requeues_and_blocks_not_instant_busy():
    _hold()
    r = co.acquire("demo", "exclusive", reason="pie", agent="B", wait=0.2, max_block=0.2)
    assert r["busy"] and not r.get("still_waiting")          # budget spent, queue entry dropped
    t0 = time.time()
    r = co.wait_turn("demo", agent="B", seconds=0.5)
    assert time.time() - t0 >= 0.45
    assert not r.get("not_queued"), "instant not_queued is the spin bug"
    assert r.get("requeued") and r["still_waiting"]


def test_fifo_order_kept_across_parked_waits_and_grant_goes_to_head():
    _hold()
    co.acquire("demo", "exclusive", reason="pie", agent="B", wait=30, max_block=0.1)
    co.acquire("demo", "exclusive", reason="pie", agent="C", wait=30, max_block=0.1)
    # B resumes repeatedly; its place must never move behind C.
    for _ in range(3):
        assert co.wait_turn("demo", agent="B", seconds=0.1)["position"] == 1
    assert co.wait_turn("demo", agent="C", seconds=0.1)["position"] == 2

    def release_soon():
        time.sleep(0.4)
        co.release("demo", agent="A")
    threading.Thread(target=release_soon).start()
    # C blocks too, but only B (head) may be granted.
    rc = co.wait_turn("demo", agent="C", seconds=1.0)
    assert not rc.get("granted"), "C overtook B"
    assert co.wait_turn("demo", agent="B", seconds=1.0)["granted"]


def test_progress_line_with_position_goes_to_stderr(capsys, monkeypatch):
    monkeypatch.setattr(co, "_POSITION_NOTE_EVERY", 0.0)
    _hold()
    co.acquire("demo", "exclusive", reason="pie", agent="B", wait=0.3)
    err = capsys.readouterr().err
    assert "B waiting" in err and "held by A" in err and "position 1/1" in err


def test_cli_lease_wait_accepts_wait_alias(capsys):
    _hold()
    co.acquire("demo", "exclusive", reason="pie", agent="B", wait=30, max_block=0.1)
    t0 = time.time()
    rc = main(["lease", "wait", "--agent", "B", "--project", "demo", "--wait", "0.5"])
    assert time.time() - t0 >= 0.45
    assert rc == 1
    assert '"still_waiting": true' in capsys.readouterr().out
