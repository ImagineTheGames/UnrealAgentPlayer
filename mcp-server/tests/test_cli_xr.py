"""`uap xr ...` -- verbs for the injected VR controllers.

Before these existed the plugin could already place a fake Quest controller
(InjectXRControllerPose), but only MCP, a hand-written `rc` JSON struct or editor Python could
reach it, so agents verifying a grab used the flat pawn instead and hand poses shipped wrong.
What is pinned here is the wire shape each verb sends, because a struct field misnamed on the
wire is silently zero-initialised by RemoteControl -- the call answers ok and the hand goes to
the origin.
"""

import json

import pytest

from unreal_agent_player import cli


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")


def _wire(monkeypatch, answers: dict | None = None):
    calls: list[tuple[str, dict]] = []

    def fake(func, params, project=None):
        calls.append((func, params))
        if answers and func in answers:
            return answers[func]
        if func in ("InjectXRControllerPose", "ClearXRControllerOverride"):
            return True
        if func == "HoldAxis":
            return json.dumps({"ok": True, "key": params["AxisKeyName"], "pressed": True})
        if func == "HoldKey":
            return json.dumps({"ok": True, "key": params["KeyName"], "pressed": True})
        if func == "GetHeldInput":
            return json.dumps({"ok": True, "held": []})
        if func == "SetXRAimEmulation":
            return json.dumps({"ok": True, "aim_emulation": params["bEnabled"]})
        if func == "GetXRControllerState":
            return json.dumps({"ok": True, "aim_emulation": False, "hands": {}})
        raise AssertionError(f"unexpected RC call {func}")

    monkeypatch.setattr(cli, "_rc_call", fake)
    return calls


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return rc, json.loads(out[-1])


def test_pose_sends_named_struct_fields(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc, body = _run(["xr", "pose", "left", "32", "-14", "140", "--pitch", "45", "--roll", "-5"],
                    capsys)
    assert rc == 0 and body["ok"] and body["applied"]
    func, params = calls[-1]
    assert func == "InjectXRControllerPose"
    assert params == {"Hand": "Left", "Position": {"X": 32.0, "Y": -14.0, "Z": 140.0},
                      "Orientation": {"Pitch": 45.0, "Yaw": 0.0, "Roll": -5.0},
                      "bTracked": True}


def test_pose_false_is_a_failure(monkeypatch, capsys):
    _wire(monkeypatch, {"InjectXRControllerPose": False})
    rc, body = _run(["xr", "pose", "right", "1", "2", "3"], capsys)
    assert rc == 1 and body["ok"] is False and "error" in body


def test_clear_defaults_to_both_hands(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc, _body = _run(["xr", "clear"], capsys)
    assert rc == 0
    assert [p["Hand"] for f, p in calls if f == "ClearXRControllerOverride"] == ["Left", "Right"]


def test_grip_is_an_input_axis_hold_on_the_hand_key(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc, body = _run(["xr", "grip", "left", "--seconds", "4"], capsys)
    assert rc == 0, body
    _func, params = [c for c in calls if c[0] == "HoldAxis"][-1]
    assert params["AxisKeyName"] == "OculusTouch_Left_Grip_Axis"
    assert params["Value"] == 1.0 and params["Seconds"] == 4.0
    assert "SlateUser" not in params      # viewport route, like `input axis` without --user
    assert body["ends_in_seconds"] == 4.0


def test_trigger_key_and_value(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc, _ = _run(["xr", "trigger", "right", "0.5", "--seconds", "1"], capsys)
    assert rc == 0
    params = [c for c in calls if c[0] == "HoldAxis"][-1][1]
    assert params["AxisKeyName"] == "OculusTouch_Right_Trigger_Axis" and params["Value"] == 0.5


def test_aim_on_without_delta_sends_zero_meaning_quest_default(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc, body = _run(["xr", "aim", "on"], capsys)
    assert rc == 0 and body["aim_emulation"] is True
    params = calls[-1][1]
    assert params["GripToAimLocation"] == {"X": 0.0, "Y": 0.0, "Z": 0.0}
    assert params["GripToAimRotation"] == {"Pitch": 0.0, "Yaw": 0.0, "Roll": 0.0}


def test_aim_with_explicit_delta(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    _run(["xr", "aim", "on", "--loc", "7", "0", "-6", "--rot", "-55", "0", "0"], capsys)
    params = calls[-1][1]
    assert params["bEnabled"] is True
    assert params["GripToAimLocation"] == {"X": 7.0, "Y": 0.0, "Z": -6.0}
    assert params["GripToAimRotation"]["Pitch"] == -55.0


def test_aim_off_and_state(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    _run(["xr", "aim", "off"], capsys)
    assert calls[-1] == ("SetXRAimEmulation", calls[-1][1]) and calls[-1][1]["bEnabled"] is False
    rc, body = _run(["xr", "state"], capsys)
    assert rc == 0 and calls[-1][0] == "GetXRControllerState" and body["aim_emulation"] is False


def test_old_plugin_without_aim_verb_is_named_as_skew(monkeypatch, capsys):
    def fake(func, params, project=None):
        raise cli.AgentError(cli.ErrorCode.UE_UNREACHABLE,
                             "Remote Control preset call returned 404: Unable to resolve")

    monkeypatch.setattr(cli, "_rc_call", fake)
    rc, body = _run(["xr", "aim", "on"], capsys)
    assert rc == 1
    assert "SetXRAimEmulation" in json.dumps(body)


def test_xr_is_lease_and_machine_guarded():
    assert "xr" in cli._LEASE_GUARDED
    assert "xr" in cli._MACHINE_GUARDED


def test_grip_also_holds_the_click_like_a_real_controller(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc, body = _run(["xr", "trigger", "left", "--seconds", "0.3"], capsys)
    assert rc == 0, body
    keys = [c[1].get("KeyName") for c in calls if c[0] == "HoldKey"]
    assert keys == ["OculusTouch_Left_Trigger_Click"]
    assert body["key"] == "OculusTouch_Left_Trigger_Click"


def test_no_click_and_light_squeeze_hold_only_the_axis(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    _run(["xr", "grip", "right", "--seconds", "1", "--no-click"], capsys)
    _run(["xr", "grip", "right", "0.3", "--seconds", "1", "--overlap"], capsys)
    assert not [c for c in calls if c[0] == "HoldKey"]
