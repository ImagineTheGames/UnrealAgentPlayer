"""`uap input action <IA path> <value>`: Enhanced Input InjectInputForAction.

Why it exists: uap key injection (hold/axis) stops at the PlayerController's key mappings, so the
School's Out VR pawn's jump (a Blueprint event on the MotionControllerThumbRight_Y action, then
ASOVRCharacter::HandleStickAxis) could not be driven by any injected key. On a headset OpenXR
calls UEnhancedInputLocalPlayerSubsystem::InjectInputForAction directly; this verb does the same.
Pinned here: the wire call (object, function, parameters), --client passthrough, the plugin's own
refusal relayed verbatim, and that the C++ side really calls InjectInputForAction.
"""

import json
from pathlib import Path

import pytest

from unreal_agent_player import cli

REPO = Path(__file__).resolve().parents[2]
LIB_CPP = REPO / "Plugin/Source/UnrealAgentPlayerRuntime/Private/UAPInputActionLibrary.cpp"
BUILD_CS = REPO / "Plugin/Source/UnrealAgentPlayerRuntime/UnrealAgentPlayerRuntime.Build.cs"


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _stub(monkeypatch, reply):
    seen = []

    def fake(object_path, func, params, project=None, *, needs):
        seen.append((object_path, func, params))
        return reply

    monkeypatch.setattr(cli, "_rc_object_json", fake)
    return seen


def test_action_calls_the_library_cdo_with_path_and_value(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"ok": True, "action": "X", "value": 1.0, "error": ""})
    rc = cli.main(["input", "action", "/Game/VRE/Input/Actions/MotionControllerThumbRight_Y", "1"])
    assert rc == 0
    obj, func, params = seen[0]
    assert obj == "/Script/UnrealAgentPlayerRuntime.Default__UAPInputActionLibrary"
    assert func == "InjectInputAction"
    assert params["ActionPath"] == "/Game/VRE/Input/Actions/MotionControllerThumbRight_Y"
    assert params["Value"] == 1.0
    assert params["Client"] == ""
    assert _out(capsys)["ok"] is True


def test_value_defaults_to_one(monkeypatch):
    seen = _stub(monkeypatch, {"ok": True})
    cli.main(["input", "action", "/Game/A/IA_X"])
    assert seen[0][2]["Value"] == 1.0


def test_zero_value_is_sent_as_zero(monkeypatch):
    seen = _stub(monkeypatch, {"ok": True})
    cli.main(["input", "action", "/Game/A/IA_X", "0"])
    assert seen[0][2]["Value"] == 0.0


def test_plugin_refusal_is_relayed_not_invented(monkeypatch, capsys):
    _stub(monkeypatch, {"ok": False, "error": "no UInputAction at /Game/Nope.Nope"})
    rc = cli.main(["input", "action", "/Game/Nope"])
    body = _out(capsys)
    assert rc != 0
    assert body["ok"] is False
    assert body["error"] == "no UInputAction at /Game/Nope.Nope"


def test_client_selector_is_forwarded(monkeypatch):
    seen = _stub(monkeypatch, {"ok": True})
    cli.main(["input", "action", "/Game/A/IA_X", "1", "--client", "1"])
    assert seen[0][2]["Client"] == "1"


def test_cpp_really_injects_for_the_action():
    src = LIB_CPP.read_text(encoding="utf-8")
    assert "InjectInputForAction(" in src
    assert "UEnhancedInputLocalPlayerSubsystem" in src
    assert '"EnhancedInput"' in BUILD_CS.read_text(encoding="utf-8")
