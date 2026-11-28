"""A HUNG editor (process alive, Remote Control unreachable) must not trap the lease queue.

2026-11-13, after a DXGI DEVICE_HUNG: an agent held an exclusive `pie` lease on a dead editor and
four `relaunch-preflight` (Restart-Editor) waiters sat behind pie waiters, so nobody could recover
without releasing by hand. The process-gone case was already handled (test_lease_dead_editor.py);
these pin the process-alive case: relaunch jumps the queue, no pie is granted on a dead editor, and
a pie lease held on one is released after a grace. Every probe is faked.
"""
import time

import pytest

import unreal_agent_player.coordination as co

PROJ = "demo"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_RC_PORT", "1")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.setenv("UAP_PIE_REAPER", "0")
    monkeypatch.setenv("UAP_RC_DOWN_GRACE", "0.2")
    monkeypatch.setenv("UAP_RC_EVICT_GRACE", "2.0")
    monkeypatch.setenv("UAP_RC_CONFIRM_SPACING", "0")
    monkeypatch.setenv("UAP_RC_STREAK_GAP", "1.0")
    monkeypatch.setattr(co, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(co, "_RC_UP_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(co, "_RC_DOWN_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(co, "_editor_probe", lambda project: True)   # process IS running


class Rc:
    """Controllable Remote Control probe."""
    def __init__(self, monkeypatch, answer=False):
        self.answer = answer
        monkeypatch.setattr(co, "_rc_probe", lambda project: self.answer)


def _wait_down():
    time.sleep(0.3)         # past the 0.2s down grace


def _queue():
    return [w["agent"] for w in co.status(PROJ)["waiters"]]


def _ask(agent, reason, wait=0, **kw):
    return co.acquire(PROJ, "exclusive", reason=reason, agent=agent, pid=0, wait=wait, **kw)


def test_pie_is_not_granted_on_an_unreachable_editor(monkeypatch):
    Rc(monkeypatch, False)
    # First sighting: inside the grace, a stalled-but-alive editor is not yet "down".
    assert _ask("early", "pie")["granted"]
    co.release(PROJ, agent="early")
    _wait_down()
    r = _ask("late", "pie")
    assert not r["granted"] and r["busy"] and r["editor_down"], r
    assert "relaunch" in r["hint"] and r["rc_down_seconds"] >= 0
    assert co.status(PROJ)["exclusive"] is None, "a pie lease was granted on a dead editor"


def test_livecoding_and_level_are_refused_too(monkeypatch):
    Rc(monkeypatch, False)
    _ask("x", "pie"); co.release(PROJ, agent="x")
    _wait_down()
    for reason in ("livecoding", "level:/Game/Maps/A", "2p"):
        assert _ask("y", reason).get("editor_down"), reason


def test_shared_reads_are_not_gated(monkeypatch):
    Rc(monkeypatch, False)
    _ask("x", "pie"); co.release(PROJ, agent="x")
    _wait_down()
    assert co.acquire(PROJ, "shared", reason="read", agent="r", wait=0)["granted"]


def test_relaunch_is_granted_on_an_unreachable_editor(monkeypatch):
    Rc(monkeypatch, False)
    _ask("x", "pie"); co.release(PROJ, agent="x")
    _wait_down()
    assert _ask("rb", "relaunch-preflight")["granted"]


def test_relaunch_jumps_ahead_of_earlier_pie_waiters(monkeypatch):
    rc = Rc(monkeypatch, True)
    assert _ask("holder", "pie")["granted"]
    for a in ("p1", "p2", "lc"):
        _ask(a, "pie" if a != "lc" else "livecoding", wait=30, max_block=0.01)
    _ask("rl1", "relaunch-preflight", wait=30, max_block=0.01)
    _ask("rl2", "relaunch-preflight", wait=30, max_block=0.01)
    assert _queue() == ["p1", "p2", "lc", "rl1", "rl2"]          # up editor: arrival order
    rc.answer = False
    _ask("rl2", "relaunch-preflight", wait=30, max_block=0.01)    # first sighting of the hang
    _wait_down()
    # Any waiter's poll then reorders: relaunch waiters go to the head, FIFO within groups.
    _ask("rl2", "relaunch-preflight", wait=30, max_block=0.01)
    assert _queue() == ["rl1", "rl2", "p1", "p2", "lc"], _queue()


def test_fifo_is_kept_while_the_editor_is_up(monkeypatch):
    Rc(monkeypatch, True)
    assert _ask("holder", "pie")["granted"]
    _ask("p1", "pie", wait=30, max_block=0.01)
    _ask("rl", "relaunch-preflight", wait=30, max_block=0.01)
    _ask("p2", "pie", wait=30, max_block=0.01)
    # An up editor does not reorder anything: arrival order stands (p1, rl, p2).
    assert _queue() == ["p1", "rl", "p2"]


def test_pie_lease_on_a_hung_editor_is_released_after_the_grace(monkeypatch):
    rc = Rc(monkeypatch, True)
    assert _ask("tooltips", "pie")["granted"]
    rc.answer = False
    _ask("rl", "relaunch-preflight", wait=30, max_block=0.01)     # notices: down_since set
    assert co.status(PROJ)["exclusive"]["agent"] == "tooltips", "evicted inside the grace"
    for _ in range(4):                                # a continuous watch, each reading < gap
        time.sleep(0.55)
        _ask("rl", "relaunch-preflight", wait=30, max_block=0.01)
    r = _ask("rl", "relaunch-preflight", wait=0.5, max_block=0.4)
    assert r["granted"], r
    ev = co.status(PROJ)["last_eviction"]
    assert ev["why"] == co.EVICT_EDITOR_UNRESPONSIVE and ev["agent"] == "tooltips"
    note = co.take_eviction_notice(PROJ, agent="tooltips")
    assert note and "relaunch" in note["message"].lower()
    # The eviction names its evidence.
    assert ev["evidence"]["unreachable_readings"] >= 3, ev
    assert "consecutive" in note["message"] and "probes" in note["message"]


def test_a_brief_stall_that_recovers_never_evicts_or_refuses(monkeypatch):
    rc = Rc(monkeypatch, True)
    assert _ask("holder", "pie")["granted"]
    rc.answer = False
    _ask("w", "pie", wait=30, max_block=0.01)
    time.sleep(0.1)                           # inside the 0.2s grace
    rc.answer = True                          # level finished loading
    _ask("w", "pie", wait=30, max_block=0.01)
    time.sleep(0.8)
    _ask("w", "pie", wait=30, max_block=0.01)
    assert co.status(PROJ)["exclusive"]["agent"] == "holder"
    assert "rc_down_since" not in co._load(PROJ)


def test_pie_is_granted_again_once_the_editor_answers(monkeypatch):
    rc = Rc(monkeypatch, False)
    _ask("x", "pie"); co.release(PROJ, agent="x")
    _wait_down()
    assert _ask("a", "pie").get("editor_down")
    rc.answer = True
    assert _ask("a", "pie")["granted"]


def test_queued_pie_waiter_keeps_its_place_and_is_told_editor_down(monkeypatch):
    Rc(monkeypatch, False)
    _ask("x", "pie"); co.release(PROJ, agent="x")
    _wait_down()
    r = _ask("a", "pie", wait=5, max_block=0.2)
    assert r["still_waiting"] and r["editor_down"] and r["position"] == 1
    assert _queue() == ["a"]


def test_undecidable_probe_changes_nothing(monkeypatch):
    Rc(monkeypatch, None)
    assert _ask("holder", "pie")["granted"]
    time.sleep(0.8)
    r = _ask("w", "pie")
    assert r["busy"] and not r.get("editor_down")
    assert co.status(PROJ)["exclusive"]["agent"] == "holder"


def test_stale_down_since_plus_one_lone_failure_never_evicts(monkeypatch):
    """2026-11-28 (job 45d5874f): a sighting made while the editor stalled stayed stored after
    the editor recovered (the holder's own calls were answered, nobody probed True), and the next
    lone unreachable reading counted as 121 s of continuous silence and voided a live lease."""
    rc = Rc(monkeypatch, True)
    assert _ask("verify", "pie")["granted"]
    rc.answer = False
    _ask("w", "pie", wait=30, max_block=0.01)          # stall seen once: down_since set
    rc.answer = None                                   # recovered, but no waiter probed it
    time.sleep(2.5)                                    # past the evict grace AND the streak gap
    rc.answer = False                                  # one new transient failure
    _ask("w", "pie", wait=30, max_block=0.01)
    _ask("rl", "relaunch-preflight", wait=0.3, max_block=0.1)
    assert co.status(PROJ)["exclusive"]["agent"] == "verify", co.status(PROJ)
    assert co.status(PROJ).get("last_eviction") is None


def test_eviction_needs_repeat_failures_not_one(monkeypatch):
    """Deterministic (fake clock): the evict grace has passed after reading 1, yet only the
    third continuous unreachable reading voids the lease."""
    clock = [1000.0]
    def _tick():
        clock[0] += 0.002           # keeps acquire's own wait loop finite
        return clock[0]
    monkeypatch.setattr(co, "_now", _tick)
    monkeypatch.setenv("UAP_RC_STREAK_GAP", "100")
    rc = Rc(monkeypatch, True)
    assert _ask("verify", "pie")["granted"]
    rc.answer = False
    _ask("w", "pie", wait=30, max_block=0.01)          # reading 1
    clock[0] += 60
    _ask("w", "pie", wait=30, max_block=0.01)          # reading 2
    clock[0] += 60                                     # 120s down, past the 2s grace
    assert co.status(PROJ)["exclusive"]["agent"] == "verify"
    _ask("w", "pie", wait=30, max_block=0.01)          # reading 3 confirms
    ev = co.status(PROJ)["last_eviction"]
    assert ev and ev["why"] == co.EVICT_EDITOR_UNRESPONSIVE
    assert ev["evidence"]["unreachable_readings"] == 3


def test_an_answer_resets_the_streak():
    st = {}
    for _ in range(3):
        co._note_rc(st, False)
    assert st["rc_down_since"] and st["rc_down_last"]
    co._note_rc(st, True)
    assert "rc_down_count" not in st and "rc_down_since" not in st and st["rc_last_up"]
