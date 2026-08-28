"""Slate USER INDEX targeting for injected input, and the loud refusal that replaces its
silent discard.

The defect (ClickUp 86ak7kay9, found live on Project Broken Wings): driving an analog stick
through `uap` moved a Slate analog cursor exactly zero pixels. No error, no warning -- the
crosshair was pixel-identical before and after and `GetMousePosition()` never changed, so it
read as the feature under test being broken and nearly became a bug filed against a working
fix.

Cause: `FAnalogCursor::IsRelevantInput()` is `GetOwnerUserIndex() == InputEvent.GetUserIndex()`
(engine AnalogCursor.cpp:192), and our Slate-path injections stamped user index 0
unconditionally. The event was discarded before the cursor ever ran. This is NOT the already
documented `InjectKey` trap -- that one is the wrong LAYER (below the pre-processor chain);
this is the right layer with the wrong USER.

What is pinned here:
  1. the default path is unchanged and sends no SlateUser at all (older plugin copies, and
     "" is what the plugin reads as "resolve it yourself / keep the viewport route");
  2. --user selects the SLATE route and says so in the result, because the difference between
     the two routes is otherwise invisible from outside -- which is what made the original
     defect look like a product bug;
  3. an index nothing is registered on is REFUSED, loudly, with all three parts of the house
     failure-message rule, and with zero side effects.
"""

import json
import re
from pathlib import Path

import pytest

from unreal_agent_player import cli

REPO = Path(__file__).resolve().parents[2]
AGENT_INPUT_CPP = REPO / "Plugin/Source/UnrealAgentPlayerRuntime/Private/AgentInput.cpp"


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


def _stub_rc(monkeypatch, table):
    seen = []

    def fake(func, params, project=None):
        seen.append((func, params, project))
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val(params) if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --- default routing is untouched --------------------------------------------------------

def test_axis_without_user_sends_no_slate_user(monkeypatch, capsys):
    """RemoteControl builds the argument struct ZERO-INITIALISED, so a SlateUser we always
    sent (or an int32 the caller omitted, arriving as 0 -- a VALID index) would silently move
    every existing call onto the Slate route. Omitted must mean omitted."""
    seen = _stub_rc(monkeypatch, {"HoldAxis": json.dumps(
        {"ok": True, "key": "Gamepad_LeftX", "value": 1.0, "seconds": 1.0,
         "pressed": True, "route": "viewport"})})
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0"]) == 0
    assert "SlateUser" not in seen[0][1]
    assert _out(capsys)["route"] == "viewport"


# --- targeting a user -------------------------------------------------------------------

def test_user_flag_targets_a_slate_user_and_reports_the_slate_route(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"HoldAxis": json.dumps(
        {"ok": True, "key": "Gamepad_LeftX", "value": 1.0, "seconds": 2.0,
         "pressed": True, "route": "slate", "user_index": 2})})
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0",
                     "--seconds", "2", "--user", "2"]) == 0
    # A STRING on the wire: see the zero-init note above.
    assert seen[0][1]["SlateUser"] == "2"
    body = _out(capsys)
    assert body["route"] == "slate", "the caller must be able to SEE which layer was driven"
    assert body["user_index"] == 2


def test_user_zero_is_still_an_explicit_request(monkeypatch, capsys):
    """0 is a real user index, not "unset". If --user 0 were dropped as falsy the call would
    fall back to the viewport route and report success while the cursor never moved -- the
    original defect, reintroduced one layer up."""
    seen = _stub_rc(monkeypatch, {"HoldAxis": json.dumps(
        {"ok": True, "key": "Gamepad_LeftY", "value": -1.0, "seconds": 1.0,
         "pressed": True, "route": "slate", "user_index": 0})})
    assert cli.main(["input", "axis", "Gamepad_LeftY", "-1.0", "--user", "0"]) == 0
    assert seen[0][1]["SlateUser"] == "0"


def test_status_reports_the_route_of_a_live_hold(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"GetHeldInput": json.dumps({"ok": True, "held": [
        {"key": "Gamepad_LeftX", "analog": True, "value": 1.0, "remaining_seconds": 1.4,
         "down": True, "route": "slate", "user_index": 0}]})})
    assert cli.main(["input", "status"]) == 0
    held = _out(capsys)["held"][0]
    assert held["route"] == "slate" and held["user_index"] == 0


# --- the loud refusal --------------------------------------------------------------------

_REFUSAL = (
    "Slate has no registered user 7, so an event stamped with that index is DISCARDED before "
    "any handler runs -- FAnalogCursor::IsRelevantInput() is GetOwnerUserIndex() == "
    "InputEvent.GetUserIndex() (engine AnalogCursor.cpp:192), and every Slate handler that "
    "filters by user does the same. Registered Slate users right now: 0 (focus: SViewport). "
    "Do NOT just drop the user index and retry: with no index this call takes the "
    "game-viewport route, which never enters the Slate pre-processor chain at all, so an "
    "analog/virtual cursor still sees nothing and the call still reports ok. Re-run with "
    "--user <N> using an index from that list -- the one that OWNS the pre-processor you are "
    "driving (for a single local player that is 0)."
)


def test_an_unlistened_user_index_is_refused_not_discarded(monkeypatch, capsys):
    """The heart of the ticket. A silent discard is worse than a missing verb: there is no 404
    for anyone to notice, so the tool reports success and the reader blames the product."""
    _stub_rc(monkeypatch, {"HoldAxis": json.dumps(
        {"ok": False, "key": "Gamepad_LeftX", "pressed": False, "error": _REFUSAL})})
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0", "--user", "7"]) == 1
    body = _out(capsys)
    assert body["ok"] is False
    assert body["pressed"] is False, "a refused call must have zero side effects"
    assert body["error"] == _REFUSAL, "the CLI relays the plugin's reason, never invents one"


def test_refusal_has_all_three_parts_of_the_house_rule():
    """agentplayertest.md, "Adding a verb? What a good failure message contains": name the
    capability/mismatch, say why the plausible substitute is wrong, give the exact remedy.
    Part 2 is the one people drop, and it is the one that matters most here -- the obvious
    "just leave --user off" retry lands on the viewport route, which cannot reach a Slate
    pre-processor at all and answers ok."""
    assert "AnalogCursor.cpp:192" in _REFUSAL              # 1: named, and citable
    assert "DISCARDED" in _REFUSAL
    assert "Do NOT just drop the user index" in _REFUSAL   # 2: the wrong substitute
    assert "never enters the Slate pre-processor chain" in _REFUSAL
    assert "Re-run with --user <N>" in _REFUSAL            # 3: the remedy
    assert "Registered Slate users right now" in _REFUSAL  # ...with the values to use


# --- the refusal is authored in the plugin, so pin it there ------------------------------

def test_plugin_source_carries_that_refusal_verbatim():
    """The wording lives in C++ where pytest cannot execute it, so assert on the source. This
    is what stops the message decaying back into a bare `return false`."""
    src = AGENT_INPUT_CPP.read_text(encoding="utf-8")
    for fragment in (
        "DISCARDED before any handler runs",
        "AnalogCursor.cpp:192",
        "Do NOT just drop the user index and retry",
        "never enters the Slate ",
        "Re-run with --user <N>",
    ):
        assert fragment in src, fragment


def test_every_slate_path_injection_resolves_its_user():
    """Audit pinned as a test: no Slate event may be constructed with a literal user index.
    Each of these used to pass a hardcoded 0 -- FAnalogInputEvent, FKeyEvent and (via the
    7-arg ctor, which hardcodes it inside the ENGINE) FPointerEvent."""
    src = AGENT_INPUT_CPP.read_text(encoding="utf-8")
    for ctor in ("FAnalogInputEvent Evt(", "FKeyEvent Evt(", "FPointerEvent Evt("):
        assert ctor in src, ctor
    # The user slot on each is now (uint32)User, never a literal.
    assert not re.search(r"FAnalogInputEvent Evt\(Key, App\.GetModifierKeys\(\), 0,", src)
    assert not re.search(r"FKeyEvent Evt\(Key, App\.GetModifierKeys\(\), 0,", src)
    assert src.count("(uint32)User") >= 4


# --- MCP tool layer ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_tools_send_slate_user_only_when_asked(httpx_mock):
    from unreal_agent_player.tools.input import input_axis, input_gamepad
    from unreal_agent_player.transport import RemoteControlClient

    httpx_mock.add_response(json={"ReturnValue": True})
    httpx_mock.add_response(json={"ReturnValue": True})
    httpx_mock.add_response(json={"ReturnValue": True})
    rc = RemoteControlClient()

    plain = await input_axis(rc=rc, py_exec=None, axis_name="Gamepad_LeftX", value=1.0)
    assert plain["route"] == "viewport"
    assert b"SlateUser" not in httpx_mock.get_requests()[0].content

    targeted = await input_axis(rc=rc, py_exec=None, axis_name="Gamepad_LeftX", value=1.0,
                                slate_user=0)
    assert targeted["route"] == "slate"
    body = json.loads(httpx_mock.get_requests()[1].content)
    assert body["parameters"]["SlateUser"] == "0"

    await input_gamepad(rc=rc, py_exec=None, button="LeftStickX", pressed=True,
                        analog=1.0, slate_user=1)
    body = json.loads(httpx_mock.get_requests()[2].content)
    assert body["parameters"]["SlateUser"] == "1"
    await rc.aclose()
