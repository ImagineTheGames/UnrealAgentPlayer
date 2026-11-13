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


# --- per-client hands [17tm466zwx5] ---------------------------------------------------------
# Before this, InjectXRControllerPose / ClearXRControllerOverride / SetXRAimEmulation /
# GetXRControllerState took no Client and the plugin kept ONE global hand state, so `uap xr pose`
# moved the hand on EVERY PIE player. These pin the CLI half: the client rides all four verbs
# (and the whole grip/trigger path), and an older plugin without the parameter is refused
# instead of silently driving the wrong (or every) client.

def _fake_rc(monkeypatch, sent):
    class FakeRC:
        def __init__(self, *a, **k):
            pass

        async def call_preset(self, func, params, *a, **k):
            sent.append((func, dict(params)))
            if func in ("InjectXRControllerPose", "ClearXRControllerOverride"):
                return True
            if func == "GetHeldInput":
                return json.dumps({"ok": True, "held": []})
            return json.dumps({"ok": True, "key": params.get("AxisKeyName") or params.get("KeyName"),
                               "pressed": True, "client": 1, "aim_emulation": True, "hands": {}})

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "RemoteControlClient", FakeRC)
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    monkeypatch.setattr(cli, "_CLIENT", [None])


@pytest.mark.parametrize("argv,func", [
    (["xr", "pose", "right", "1", "2", "3"], "InjectXRControllerPose"),
    (["xr", "clear", "left"], "ClearXRControllerOverride"),
    (["xr", "aim", "on"], "SetXRAimEmulation"),
    (["xr", "state"], "GetXRControllerState"),
])
def test_client_rides_every_xr_pose_verb(monkeypatch, capsys, argv, func):
    sent = []
    _fake_rc(monkeypatch, sent)
    assert cli.main(argv + ["--client", "1"]) == 0
    calls = [p for f, p in sent if f == func]
    assert calls and all(p["Client"] == "1" for p in calls), (func, sent)


def test_no_client_flag_leaves_the_xr_pose_calls_without_a_client(monkeypatch, capsys):
    """Omitted = the plugin's default client (client 0), so the wire is exactly as before."""
    sent = []
    _fake_rc(monkeypatch, sent)
    assert cli.main(["xr", "pose", "right", "1", "2", "3"]) == 0
    assert next(p for f, p in sent if f == "InjectXRControllerPose").get("Client") is None


def test_grip_with_client_holds_on_that_client_only(monkeypatch, capsys):
    sent = []
    _fake_rc(monkeypatch, sent)
    assert cli.main(["xr", "grip", "right", "--seconds", "1", "--client", "1"]) == 0
    holds = [p for f, p in sent if f in ("HoldAxis", "HoldKey")]
    assert holds and all(p.get("Client") == "1" for p in holds), sent


def test_old_plugin_without_client_on_pose_is_refused_not_applied_everywhere(monkeypatch, capsys):
    sent = []
    _fake_rc(monkeypatch, sent)
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: {
        "InjectXRControllerPose": {"Hand": "x", "Position": "x", "Orientation": "x", "bTracked": "x"}})
    rc, body = _run(["xr", "pose", "right", "1", "2", "3", "--client", "1", "--project", "SchoolsOut"],
                    capsys)
    assert rc == 1 and body["ok"] is False
    assert not [1 for f, _ in sent if f == "InjectXRControllerPose"]


# --- the HEAD (HMD) of a PIE client -----------------------------------------------------------
# A PIE client never sends a tracked head, so the server's copy of that player's head sits at the
# floor. `xr pose head` rides InjectXRControllerPose with Hand=Head; an older plugin has no Head in
# its enum (and might coerce the name to Left), so the CLI refuses unless GetXRControllerState
# reports a head block.

def _head_fake(monkeypatch, sent, head_aware=True):
    class FakeRC:
        def __init__(self, *a, **k):
            pass

        async def call_preset(self, func, params, *a, **k):
            sent.append((func, dict(params)))
            if func in ("InjectXRControllerPose", "ClearXRControllerOverride"):
                return True
            if func == "GetXRControllerState":
                body = {"ok": True, "client": 1, "aim_emulation": False, "hands": {}}
                if head_aware:
                    body["head"] = {"active": False}
                return json.dumps(body)
            return json.dumps({"ok": True})

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "RemoteControlClient", FakeRC)
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    monkeypatch.setattr(cli, "_CLIENT", [None])


def test_head_pose_sends_head_with_client_and_rotation(monkeypatch, capsys):
    sent = []
    _head_fake(monkeypatch, sent)
    assert cli.main(["xr", "pose", "head", "10", "-20", "170", "--rotation", "-5,90,0",
                     "--client", "1"]) == 0
    _func, params = [c for c in sent if c[0] == "InjectXRControllerPose"][-1]
    assert params["Hand"] == "Head" and params["Client"] == "1"
    assert params["Position"] == {"X": 10.0, "Y": -20.0, "Z": 170.0}
    assert params["Orientation"] == {"Pitch": -5.0, "Yaw": 90.0, "Roll": 0.0}
    assert params["bTracked"] is True


def test_head_pose_without_client_sends_no_client(monkeypatch, capsys):
    sent = []
    _head_fake(monkeypatch, sent)
    assert cli.main(["xr", "pose", "head", "0", "0", "170"]) == 0
    assert "Client" not in [c for c in sent if c[0] == "InjectXRControllerPose"][-1][1]


def test_head_pose_refused_on_a_plugin_without_head(monkeypatch, capsys):
    sent = []
    _head_fake(monkeypatch, sent, head_aware=False)
    rc = cli.main(["xr", "pose", "head", "0", "0", "170", "--client", "1"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and out["ok"] is False
    assert "head" in json.dumps(out).lower()
    assert not [c for c in sent if c[0] == "InjectXRControllerPose"], "must not inject on a plugin without Head"


def test_head_pose_to_a_missing_client_is_the_plugins_refusal_not_skew(monkeypatch, capsys):
    class FakeRC:
        def __init__(self, *a, **k):
            pass

        async def call_preset(self, func, params, *a, **k):
            if func == "GetXRControllerState":
                return json.dumps({"ok": False, "error": "no PIE client 7 (valid: 0, 1)"})
            return False                       # InjectXRControllerPose refuses a missing client

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "RemoteControlClient", FakeRC)
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    monkeypatch.setattr(cli, "_CLIENT", [None])
    rc = cli.main(["xr", "pose", "head", "0", "0", "170", "--client", "7"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and out["ok"] is False
    assert "no such PIE client" in out["error"] and "plugin has no" not in out["error"]


def test_hand_pose_does_not_need_head_support(monkeypatch, capsys):
    sent = []
    _head_fake(monkeypatch, sent, head_aware=False)
    assert cli.main(["xr", "pose", "left", "1", "2", "3"]) == 0
    assert not [c for c in sent if c[0] == "GetXRControllerState"]


def test_head_rotation_must_be_three_numbers(monkeypatch, capsys):
    sent = []
    _head_fake(monkeypatch, sent)
    rc = cli.main(["xr", "pose", "head", "0", "0", "170", "--rotation", "1,2"])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and "PITCH,YAW,ROLL" in out["error"]


def test_clear_head_and_all(monkeypatch, capsys):
    sent = []
    _head_fake(monkeypatch, sent)
    assert cli.main(["xr", "clear", "head", "--client", "1"]) == 0
    assert [p["Hand"] for f, p in sent if f == "ClearXRControllerOverride"] == ["Head"]
    sent.clear()
    assert cli.main(["xr", "clear", "all"]) == 0
    assert [p["Hand"] for f, p in sent if f == "ClearXRControllerOverride"] == ["Left", "Right", "Head"]


def test_default_clear_stays_hands_only(monkeypatch, capsys):
    sent = []
    _head_fake(monkeypatch, sent)
    assert cli.main(["xr", "clear"]) == 0
    assert [p["Hand"] for f, p in sent if f == "ClearXRControllerOverride"] == ["Left", "Right"]
