"""`uap pie start` must not hand back a live-but-unattended editor.

The incident (2026-08-28, three times in one day, across different agents): `pie start` returned
the instant the session was QUEUED. Agents invented a waiting strategy of their own -- typically a
background watcher -- and ENDED THEIR TURN. PIE then ran live with nobody at the controls; in one
case a Project Broken Wings aircraft flew unattended into a building.

A hint in the response was not enough, and that is the part worth remembering. `queued: true,
confirmed: false, next: "uap pie wait <seconds>"` shipped that same morning and all three incidents
happened after it. Those fields were accurate and still wrong: they imply an ASYNC JOB, so an agent
did the correct thing for async work over an operation that takes 1-5 SECONDS. The default had to
change, and the async vocabulary had to leave the default path with it -- keeping it would preserve
the misleading shape after the behaviour was fixed.

These pin: the default blocks and reports `playing`/`waited_seconds` (the shape `pie stop` already
uses), `--no-wait` still exists for a genuine fire-and-forget caller, and a start that never comes
up FAILS loudly instead of acking.
"""

import json

import pytest

from unreal_agent_player import cli


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


@pytest.fixture
def clock(monkeypatch):
    """Fake wall clock: every sleep advances it, so a 60s timeout costs no real time."""
    state = {"now": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(cli.time, "sleep", lambda s: state.__setitem__("now", state["now"] + s))
    return state


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


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


_STARTED = json.dumps({"ok": True, "mode": "flat"})


# --- the new default --------------------------------------------------------------------

def test_start_blocks_until_the_play_world_is_live(monkeypatch, capsys, clock):
    """The world is created some frames after the request, so the verb must keep asking. Returning
    at the request layer while the caller asserts at the completion layer is the whole bug."""
    live = iter([False, False, True])
    seen = _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": lambda: next(live)})
    assert cli.main(["pie", "start"]) == 0
    body = _out(capsys)
    assert body["ok"] is True and body["playing"] is True
    assert seen.count("IsInPIE") == 3          # it kept asking until the answer changed
    assert body["waited_seconds"] > 0          # ...and it did not return instantly


def test_the_blocking_result_carries_no_async_vocabulary(monkeypatch, capsys, clock):
    """`queued`/`confirmed: false` read as "here is your ticket, come back later" -- which is what
    agents acted on. On the path where the world IS live they are not merely redundant, they are
    the misleading shape, so they must be absent."""
    _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    assert cli.main(["pie", "start"]) == 0
    body = _out(capsys)
    assert "queued" not in body and "confirmed" not in body and "next" not in body


def test_the_blocking_result_reads_like_pie_stop(monkeypatch, capsys, clock):
    """One vocabulary for both halves: `stop` says stopped + waited_seconds, so `start` says
    playing + waited_seconds. `waited_seconds` also quietly teaches that this is a SHORT
    synchronous call, and makes an abnormally long start visible instead of invisible."""
    _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    cli.main(["pie", "start"])
    start = _out(capsys)
    _stub(monkeypatch, {"StopPIEEx": json.dumps({"ok": True}), "IsPIEInProgress": False})
    cli.main(["pie", "stop"])
    stop = _out(capsys)
    assert {"playing", "waited_seconds"} <= set(start)
    assert {"stopped", "waited_seconds"} <= set(stop)


def test_vr_preview_waits_too(monkeypatch, capsys, clock):
    """An unattended VR Preview session is the same incident with a headset attached."""
    live = iter([False, True])
    seen = _stub(monkeypatch, {"StartPIEMode": json.dumps({"ok": True, "mode": "vr"}),
                               "IsInPIE": lambda: next(live)})
    assert cli.main(["pie", "start", "--mode", "vr"]) == 0
    assert _out(capsys)["playing"] is True
    assert seen.count("IsInPIE") == 2


def test_start_and_wait_share_one_notion_of_live(monkeypatch, capsys, clock):
    """Two implementations of "is PIE live" would drift, and a caller acting on the weaker one is
    this whole class of bug. Both go through IsInPIE -- the LIVE-world verb, not IsPIEInProgress,
    which also reads true for a session that is merely queued."""
    _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    assert cli.main(["pie", "start"]) == 0
    seen = _stub(monkeypatch, {"IsInPIE": True})
    assert cli.main(["pie", "wait", "5"]) == 0
    assert seen == ["IsInPIE"]
    assert _out(capsys)["playing"] is True


# --- the timeout ------------------------------------------------------------------------

def test_a_start_that_never_comes_up_fails_loudly(monkeypatch, capsys, clock):
    """Never a cheerful ok. Name what did not happen, why it matters (the editor is not idle and
    the session may still come up later), and what to do about it."""
    _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": False})
    assert cli.main(["pie", "start", "--timeout", "3"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["playing"] is False
    err = body["error"]
    assert "IsInPIE" in err                       # names the signal that did not turn true
    assert "may still" in err and "end a turn" in err
    assert "uap pie stop" in err                  # the exact remedy


def test_the_timeout_path_keeps_the_queued_vocabulary(monkeypatch, capsys, clock):
    """Here `queued`/`confirmed:false` are TRUE -- a start really is outstanding and may still
    create a play world after this returns. The vocabulary is not banned, it is confined to where
    it describes reality."""
    _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": False})
    assert cli.main(["pie", "start", "--timeout", "3"]) == 1
    body = _out(capsys)
    assert body["queued"] is True and body["confirmed"] is False


def test_the_default_timeout_is_bounded_and_overridable(monkeypatch, capsys, clock):
    _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": False})
    monkeypatch.setenv("UAP_PIE_START_TIMEOUT", "4")
    assert cli.main(["pie", "start"]) == 1
    assert _out(capsys)["waited_seconds"] <= 4.5
    assert cli._pie_start_timeout() == 4.0
    monkeypatch.delenv("UAP_PIE_START_TIMEOUT")
    assert cli._pie_start_timeout() == 60.0


def test_a_refused_start_never_waits(monkeypatch, capsys, clock):
    """No headset, no editor world: the start FAILED, so there is nothing to wait for. Polling
    IsInPIE for a minute here would bury the real reason under a timeout message."""
    seen = _stub(monkeypatch, {"StartPIEMode": json.dumps(
        {"ok": False, "mode": "vr", "error": "no HMD connected; connect the headset"})})
    assert cli.main(["pie", "start", "--mode", "vr"]) == 1
    assert seen == ["StartPIEMode"]
    assert "no HMD connected" in _out(capsys)["error"]


# --- the opt-out ------------------------------------------------------------------------

def test_no_wait_still_returns_the_moment_the_session_is_queued(monkeypatch, capsys, clock):
    """Fire-and-forget survives for a caller that genuinely has work to do while PIE comes up --
    it just is not what you get by default any more."""
    seen = _stub(monkeypatch, {"StartPIEMode": _STARTED})
    assert cli.main(["pie", "start", "--no-wait"]) == 0
    body = _out(capsys)
    assert seen == ["StartPIEMode"]               # nothing was polled
    assert body["queued"] is True and body["confirmed"] is False
    assert "uap pie wait" in body["next"]


def test_no_wait_says_not_to_end_a_turn_on_it(monkeypatch, capsys, clock):
    """The opt-out is the one path that can still leave PIE coming up behind you, so it carries
    the rule the incident was about."""
    _stub(monkeypatch, {"StartPIEMode": _STARTED})
    assert cli.main(["pie", "start", "--no-wait"]) == 0
    warning = _out(capsys)["warning"]
    assert "END YOUR TURN" in warning and "uap pie stop" in warning


# --- the docs half ----------------------------------------------------------------------

def test_help_says_the_start_blocks_and_that_a_turn_never_ends_in_pie():
    """`uap help` is the catalog agents read before acting. Both halves have to be here: the
    default now waits, and a turn never ends with PIE running."""
    catalog = cli._HELP_CATALOG
    assert "BLOCK until the play world is live" in catalog
    assert "NEVER END A TURN WITH PIE RUNNING" in catalog
    assert "--no-wait" in catalog
