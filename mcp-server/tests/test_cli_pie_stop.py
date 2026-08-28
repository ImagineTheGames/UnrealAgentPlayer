"""`uap pie stop` must not answer ok:true until PIE is actually gone.

The bug this pins (found live on Project Broken Wings, 2026-08-28): `pie stop` returned
`{"ok": true, "result": true}` almost immediately while PIE kept running. A PIE start is QUEUED
work -- the editor tick creates the play world one or more frames after the request -- and the
engine's end-play request is a no-op unless that world already exists. A stop landing in the gap
did nothing, the queued start brought PIE up ~4s later, and the caller had been told it succeeded.

Two halves are under test here: the plugin SERIALISES (StopPIEEx cancels the queued start) and the
CLI CONFIRMS (polls IsPIEInProgress -- live OR queued -- until it reads false, bounded).
"""

import json

import pytest

from unreal_agent_player import cli
from unreal_agent_player.errors import AgentError, ErrorCode


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


@pytest.fixture
def clock(monkeypatch):
    """Fake wall clock: every sleep advances it, so a 30s timeout costs no real time."""
    state = {"now": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(cli.time, "sleep", lambda s: state.__setitem__("now", state["now"] + s))
    return state


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


_RC_404 = 'Remote Control preset call returned 404: "Unable to resolve the preset field."'


def _stop_ex(**over):
    body = {"ok": True, "was_playing": True, "cancelled_queued_start": False, "in_progress": True}
    body.update(over)
    return json.dumps(body)


def _stub(monkeypatch, table):
    seen = []

    def fake(func, params, project=None):
        seen.append(func)
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val() if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _missing_verb():
    def raise_404():
        raise AgentError(ErrorCode.UE_UNREACHABLE, _RC_404, recoverable=False)
    return raise_404


# --- the confirmed stop -----------------------------------------------------------------

def test_stop_waits_for_the_teardown_before_reporting_success(monkeypatch, capsys, clock):
    """Teardown happens on a later editor tick. The verb must poll through it, not ack the
    request -- ok:true has to mean the world is gone."""
    progress = iter([True, True, True, False])
    seen = _stub(monkeypatch, {"StopPIEEx": _stop_ex(), "IsPIEInProgress": lambda: next(progress)})
    assert cli.main(["pie", "stop"]) == 0
    body = _out(capsys)
    assert body["ok"] is True and body["stopped"] is True
    assert body["confirmed_with"] == "IsPIEInProgress"
    assert seen.count("IsPIEInProgress") == 4          # it kept asking until the answer changed
    assert body["waited_seconds"] > 0                  # ...and it did not return instantly


def test_stop_that_never_completes_fails_instead_of_acking(monkeypatch, capsys, clock):
    """The failure mode being fixed: an ok:true stop that did not stop. On timeout the verb must
    say so -- an agent that believes it releases the lease onto a live editor."""
    _stub(monkeypatch, {"StopPIEEx": _stop_ex(), "IsPIEInProgress": True})
    assert cli.main(["pie", "stop", "--timeout", "5"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["stopped"] is False
    assert "NOT free" in body["error"] and "release the editor lease" in body["error"]
    assert clock["now"] >= 5                            # it really waited out the timeout


def test_stop_reports_that_it_cancelled_a_queued_start(monkeypatch, capsys, clock):
    """The serialisation half: a start that is still QUEUED is cancelled, not raced. Surfacing it
    is what makes the fix verifiable live."""
    _stub(monkeypatch, {"StopPIEEx": _stop_ex(cancelled_queued_start=True, was_playing=False,
                                              in_progress=False),
                        "IsPIEInProgress": False})
    assert cli.main(["pie", "stop"]) == 0
    body = _out(capsys)
    assert body["cancelled_queued_start"] is True and body["stopped"] is True


def test_stop_relays_a_refusal_from_the_editor(monkeypatch, capsys, clock):
    """The verb exists and refused (a real failure). Do not poll, do not claim a stop."""
    _stub(monkeypatch, {"StopPIEEx": json.dumps({"ok": False, "error": "no GEditor"})})
    assert cli.main(["pie", "stop"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["error"] == "no GEditor" and body["stopped"] is False


def test_stop_transport_failure_is_not_mistaken_for_skew(monkeypatch, capsys, clock):
    """A dead editor is not an old plugin -- only an unresolvable-field 404 is skew."""
    def dead():
        raise AgentError(ErrorCode.UE_UNREACHABLE, "Could not reach Remote Control at :30010")
    _stub(monkeypatch, {"StopPIEEx": dead})
    assert cli.main(["pie", "stop"]) == 1
    assert "Could not reach" in _out(capsys)["error"]


# --- CLI/plugin skew: the older, weaker stop --------------------------------------------

def test_stop_falls_back_to_the_legacy_verb_and_says_the_check_is_weaker(monkeypatch, capsys,
                                                                        clock):
    """An older plugin copy has only the bool StopPIE, which cannot cancel a queued start, and
    only IsInPIE, which cannot see one. Same question, weaker guarantee -- say so rather than
    passing the heuristic off as the exact check."""
    _stub(monkeypatch, {"StopPIEEx": _missing_verb(), "StopPIE": True, "IsInPIE": False})
    assert cli.main(["pie", "stop"]) == 0
    body = _out(capsys)
    assert body["stopped"] is True
    assert body["degraded"] is True and body["via"] == "StopPIE"
    assert body["confirmed_with"] == "IsInPIE"
    assert "rebuild" in body["note"]


def test_degraded_stop_does_not_believe_a_single_clear_reading(monkeypatch, capsys, clock):
    """IsInPIE reads false while a start is merely QUEUED -- that one reading IS the bug. On the
    degraded path the answer must hold for a settle window before it is believed."""
    _stub(monkeypatch, {"StopPIEEx": _missing_verb(), "StopPIE": True, "IsInPIE": False})
    assert cli.main(["pie", "stop"]) == 0
    assert _out(capsys)["stopped"] is True
    assert clock["now"] >= cli._pie_stop_settle()


def test_degraded_stop_restops_a_queued_start_that_lands_afterwards(monkeypatch, capsys, clock):
    """The exact live sequence, against a plugin too old to cancel the queued start: stop, PIE
    reads clear, then the queued start creates the world. The stop must catch it, not return."""
    readings = iter([False, False, True, True] + [False] * 40)
    seen = _stub(monkeypatch, {"StopPIEEx": _missing_verb(), "StopPIE": True,
                               "IsInPIE": lambda: next(readings)})
    assert cli.main(["pie", "stop"]) == 0
    body = _out(capsys)
    assert body["stopped"] is True and body["restops"] == 1
    assert seen.count("StopPIE") == 2                   # the original stop plus the re-stop


# --- start is deliberately the other shape ----------------------------------------------

def test_only_the_opt_out_start_labels_itself_as_a_queued_ack(monkeypatch, capsys):
    """`pie start` used to return immediately and say so -- `queued: true, confirmed: false` plus
    the verb that confirms it. That labelling was correct and insufficient: it shipped the same
    morning agents left PIE running unattended three times (see test_cli_pie_start_waits.py), so
    the DEFAULT now blocks and the ack shape is confined to the explicit `--no-wait` opt-out,
    where a ticket really is outstanding."""
    _stub(monkeypatch, {"StartPIEMode": json.dumps({"ok": True, "mode": "flat"})})
    assert cli.main(["pie", "start", "--no-wait"]) == 0
    body = _out(capsys)
    assert body["queued"] is True and body["confirmed"] is False
    assert "pie wait" in body["next"]


def test_wait_asks_whether_the_world_is_LIVE_not_merely_queued(monkeypatch, capsys):
    """The one place the narrower verb is the right question: a queued-but-not-created session is
    exactly what `wait` must keep waiting through."""
    seen = _stub(monkeypatch, {"IsInPIE": True})
    assert cli.main(["pie", "wait", "5"]) == 0
    assert seen == ["IsInPIE"] and _out(capsys)["playing"] is True
