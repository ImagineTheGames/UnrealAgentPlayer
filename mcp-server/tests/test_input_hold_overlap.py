"""Consecutive `input hold` calls must not overlap in silence [17tm466fz3p].

`uap input hold <Key> --seconds N` returns before the hold expires. That part is deliberate --
the hold runs IN-ENGINE precisely so game state can be read WHILE it is held, and the CLI
round-trip is ~1s, so a blocking call could never see the middle of a short window. `--wait`
has always been there for the other case.

What was NOT deliberate is what happened next. The plugin keeps one hold per FKey
(`UAPFindOrAddHold`, AgentInput.cpp) and a second `hold` on a DIFFERENT key simply starts
alongside the first, so a caller following AGENTS.md as "hold W for 3s, then hold A for 3s"
got 3 seconds of W+A and a pawn that kept moving after it believed the hold had ended. It
contaminated a session: a pawn drifted ~3500 cm off the plaza and the run was discarded. And
it reads as the pawn being odd, not as a tool fault, because every call reported ok.

Two changes are under test here:
  * the result says WHEN the hold ends (`ends_in_seconds` / `ends_at_epoch`), so the end is
    knowable from the answer instead of having to be remembered by the caller;
  * a hold that would overlap one still running is REFUSED, unless `--overlap` says the caller
    meant them simultaneous (two stick axes, which is a real thing).

The overlap check is two-stage on purpose, and both stages are tested: an on-disk ledger (a
hold outlives the process that started it, so there is nowhere else to put this) decides
whether a conflict is even possible, and only then is one `GetHeldInput` paid for to check
engine ground truth. The ledger can be stale in the safe direction -- PIE stopped, a
FlushPressedKeys, another agent's `input release` -- and refusing on a hold that is not really
there would be a false failure of exactly the kind this file exists to prevent.
"""

import json
import time

import pytest

from unreal_agent_player import cli


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Own reports root, so the hold ledger (which lives beside the leases) is per-test."""
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")


def _wire(monkeypatch, held: list | None = None):
    """Route RC at canned answers and record every call, so an EXTRA round-trip is visible."""
    seen: list[str] = []

    def fake(func, params, project=None):
        seen.append(func)
        if func in ("HoldKey", "HoldAxis"):
            return json.dumps({"ok": True, "key": params.get("KeyName")
                               or params.get("AxisKeyName"),
                               "seconds": params.get("Seconds"), "pressed": True,
                               "route": "viewport"})
        if func == "GetHeldInput":
            return json.dumps({"ok": True, "held": held or []})
        if func == "ReleaseHeldInput":
            return json.dumps({"ok": True, "released": 1})
        raise AssertionError(f"unexpected RC call {func}")

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_a_first_hold_is_cheap_and_says_when_it_ends(capsys, monkeypatch):
    """Positive control for the instrument: nothing is held, so the hold goes through, and it
    costs NO extra round-trip -- the ledger answers that without asking the editor."""
    seen = _wire(monkeypatch)
    assert cli.main(["input", "hold", "W", "--seconds", "3"]) == 0
    body = _out(capsys)
    assert body["ok"] is True and body["pressed"] is True
    assert body["ends_in_seconds"] == 3.0
    assert body["ends_at_epoch"] > time.time() + 2.5
    assert "STILL HELD" in body["note"]
    assert seen == ["HoldKey"]           # no GetHeldInput: no reason to suspect a conflict


def test_a_second_hold_on_another_key_while_the_first_runs_is_refused(capsys, monkeypatch):
    """The named negative, and the defect itself. Two holds back to back used to both succeed
    and both run; the second is now refused, loudly, naming what is still down."""
    _wire(monkeypatch)
    assert cli.main(["input", "hold", "W", "--seconds", "3"]) == 0
    capsys.readouterr()
    seen = _wire(monkeypatch, held=[{"key": "W", "analog": False, "value": 1.0,
                                    "remaining_seconds": 2.4, "route": "viewport",
                                    "down": True}])
    assert cli.main(["input", "hold", "A", "--seconds", "3"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["pressed"] is False
    assert body["overlap_refused"] is True
    assert "W (2.40s left" in body["error"] and "--overlap" in body["error"]
    assert "HoldKey" not in seen         # a refused hold presses NOTHING
    assert seen == ["GetHeldInput"]


def test_overlap_is_allowed_when_asked_for(capsys, monkeypatch):
    """Two stick axes at once is a real request, not a mistake -- it just has to be said."""
    _wire(monkeypatch)
    assert cli.main(["input", "axis", "Gamepad_LeftY", "1.0", "--seconds", "3"]) == 0
    capsys.readouterr()
    seen = _wire(monkeypatch, held=[{"key": "Gamepad_LeftY", "analog": True, "value": 1.0,
                                     "remaining_seconds": 2.1, "route": "viewport",
                                     "down": True}])
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0", "--seconds", "3",
                     "--overlap"]) == 0
    assert _out(capsys)["ok"] is True
    assert "HoldAxis" in seen
    assert "GetHeldInput" not in seen    # --overlap skips the check entirely


def test_re_issuing_the_same_key_is_a_re_assert_not_a_conflict(capsys, monkeypatch):
    """`UAPFindOrAddHold` finds the existing entry and pushes its EndRealTime out. Extending a
    hold is what the verb is for, so refusing it would break the normal use to fix the abnormal
    one."""
    _wire(monkeypatch)
    assert cli.main(["input", "hold", "W", "--seconds", "3"]) == 0
    capsys.readouterr()
    seen = _wire(monkeypatch, held=[{"key": "W", "analog": False, "value": 1.0,
                                    "remaining_seconds": 2.0, "route": "viewport",
                                    "down": True}])
    assert cli.main(["input", "hold", "W", "--seconds", "5"]) == 0
    assert _out(capsys)["ok"] is True
    assert seen == ["HoldKey"]           # same key -> not even a suspicion to confirm


def test_a_stale_ledger_does_not_refuse_a_hold_that_would_not_overlap(capsys, monkeypatch):
    """The ledger is advisory. PIE stopping, a FlushPressedKeys or another agent's `input
    release` all end a hold without this process hearing about it, and the engine is the ground
    truth. When they disagree the hold goes through and the ledger is dropped, so one stale
    entry cannot keep refusing every later hold."""
    _wire(monkeypatch)
    assert cli.main(["input", "hold", "W", "--seconds", "30"]) == 0
    capsys.readouterr()
    seen = _wire(monkeypatch, held=[])   # engine says nothing is held
    assert cli.main(["input", "hold", "A", "--seconds", "1"]) == 0
    assert _out(capsys)["ok"] is True
    assert seen == ["GetHeldInput", "HoldKey"]
    # And the stale W entry is gone, so it cannot keep charging a confirmation call forever:
    # the next hold only suspects the A it just started, not the W that never ended.
    led = json.loads(cli._hold_ledger_path(None).read_text(encoding="utf-8"))
    assert list(led) == ["a"]


def test_wait_clears_the_hold_so_the_next_one_is_not_refused(capsys, monkeypatch):
    """`--wait` blocks for the duration, so by the time it returns nothing is held -- the whole
    point of it. A caller that waits must not then be told it is overlapping itself."""
    _wire(monkeypatch)
    assert cli.main(["input", "hold", "W", "--seconds", "0.05", "--wait"]) == 0
    body = _out(capsys)
    assert body["waited"] is True and body["ends_in_seconds"] == 0.0
    seen = _wire(monkeypatch)
    assert cli.main(["input", "hold", "A", "--seconds", "1"]) == 0
    assert _out(capsys)["ok"] is True
    assert seen == ["HoldKey"]


def test_release_clears_the_ledger(capsys, monkeypatch):
    """The documented recovery (`uap input release`) has to clear the CLI's own record too, or
    it would fix the engine and leave the tool refusing on a hold that is gone."""
    _wire(monkeypatch)
    assert cli.main(["input", "hold", "W", "--seconds", "30"]) == 0
    capsys.readouterr()
    _wire(monkeypatch)
    assert cli.main(["input", "release"]) == 0
    capsys.readouterr()
    seen = _wire(monkeypatch)
    assert cli.main(["input", "hold", "A", "--seconds", "1"]) == 0
    assert _out(capsys)["ok"] is True
    assert seen == ["HoldKey"]


def test_an_expired_ledger_entry_is_not_a_conflict(capsys, monkeypatch):
    """Time alone resolves most of these: a 0.05s hold is over before the next call lands, and
    that must not cost a round-trip either."""
    _wire(monkeypatch)
    assert cli.main(["input", "hold", "W", "--seconds", "0.05"]) == 0
    capsys.readouterr()
    time.sleep(0.12)
    seen = _wire(monkeypatch)
    assert cli.main(["input", "hold", "A", "--seconds", "1"]) == 0
    assert _out(capsys)["ok"] is True
    assert seen == ["HoldKey"]
