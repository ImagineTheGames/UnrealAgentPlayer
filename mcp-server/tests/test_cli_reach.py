"""release-by-name, `uap xr release`, `uap xr reach`, `uap screenshot --head-at/--head`.

Found by verify job febe8869 (Rocket Club): releasing the left trigger dropped the right grip, the
right-hand grab on a cap missed for want of a hand-to-item alignment, and a VR-pawn screenshot
showed the floor.
"""

import json

import pytest

from unreal_agent_player import cli, xr_reach


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _wire(monkeypatch):
    calls = []

    def fake(func, params, project=None):
        calls.append((func, params))
        if func == "ReleaseHeldInput":
            return json.dumps({"ok": True, "key": params["KeyName"], "released": 1})
        if func == "InjectXRControllerPose":
            return True
        if func == "GetXRControllerState":
            return json.dumps({"ok": True, "head": {}})
        raise AssertionError(func)

    monkeypatch.setattr(cli, "_rc_call", fake)
    return calls


# --- release ---------------------------------------------------------------------------

def test_two_hand_hold_releasing_one_key_sends_only_that_key(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    assert cli.main(["input", "release", "OculusTouch_Left_Trigger_Axis"]) == 0
    assert calls == [("ReleaseHeldInput", {"KeyName": "OculusTouch_Left_Trigger_Axis"})]


def test_bare_release_is_refused_and_sends_nothing(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    assert cli.main(["input", "release"]) == 1
    assert calls == []
    assert "--all" in _out(capsys)["error"]


def test_release_all_is_explicit(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    assert cli.main(["input", "release", "--all"]) == 0
    assert calls == [("ReleaseHeldInput", {"KeyName": ""})]


def test_xr_release_names_one_hand_control_axis_and_click_only(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    assert cli.main(["xr", "release", "left", "trigger"]) == 0
    assert [c[1]["KeyName"] for c in calls] == ["OculusTouch_Left_Trigger_Axis",
                                                "OculusTouch_Left_Trigger_Click"]
    assert all("Right" not in c[1]["KeyName"] and c[1]["KeyName"] != "" for c in calls)


# --- reach -----------------------------------------------------------------------------

class _FakeHand:
    """The hand as the editor sees it: grab sphere = injected pose + a fixed unknown offset."""

    def __init__(self, target, offset=(8.0, 0.0, 7.0)):
        self.target, self.offset, self.pose = list(target), offset, [0.0, 0.0, 0.0]

    def probe(self, *_a, **_k):
        grab = [p + o for p, o in zip(self.pose, self.offset)]
        return {"ok": True, "actor": "SORocketItem_6", "target_how": "actor bounds centre",
                "target": self.target, "grab": grab, "grab_component": "GrabSphereRight",
                "grab_radius": 4.0,
                "delta_tracking": [t - g for t, g in zip(self.target, grab)],
                "ctrl_rel_loc": list(self.pose), "ctrl_rel_rot": [0.0, 0.0, 0.0]}


def _reach_wire(monkeypatch, hand):
    poses = []

    def fake(func, params, project=None):
        assert func == "InjectXRControllerPose"
        p = params["Position"]
        hand.pose = [p["X"], p["Y"], p["Z"]]
        poses.append((params["Hand"], hand.pose))
        return True

    monkeypatch.setattr(cli, "_rc_call", fake)
    monkeypatch.setattr(cli, "_exec_probe", lambda code, args: hand.probe())
    return poses


@pytest.mark.parametrize("side,name", [("left", "Left"), ("right", "Right")])
def test_reach_closes_the_loop_despite_an_unknown_hand_offset(monkeypatch, capsys, side, name):
    hand = _FakeHand([100.0, -20.0, 95.0])
    poses = _reach_wire(monkeypatch, hand)
    assert cli.main(["xr", "reach", side, "SORocketItem_6"]) == 0
    body = _out(capsys)
    assert body["reached"] is True and body["distance_cm"] <= 2.0
    assert poses and poses[0][0] == name
    assert body["grab_component"] == "GrabSphereRight"


def test_reach_that_cannot_close_is_a_failure_with_the_distance(monkeypatch, capsys):
    hand = _FakeHand([100.0, 0.0, 95.0])
    _reach_wire(monkeypatch, hand)
    monkeypatch.setattr(cli, "_exec_probe", lambda c, a: {**hand.probe(), "grab": [0.0, 0.0, 0.0],
                                                          "delta_tracking": [1.0, 0.0, 0.0]})
    assert cli.main(["xr", "reach", "right", "Cap", "--iterations", "2"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["distance_cm"] > 90 and "clamping" in body["error"]


def test_reach_probe_failure_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_exec_probe",
                        lambda c, a: {"ok": False, "error": "target actor 'X' matched 0 actors: []"})
    assert cli.main(["xr", "reach", "left", "X"]) == 1
    assert "matched 0" in _out(capsys)["error"]


def test_probe_scripts_are_safe_for_remote_exec():
    for code in (xr_reach.reach_code("left", 'Actor.Comp"x'), xr_reach.look_code("A", 170)):
        compile(code, "probe", "exec")
        assert ".py" not in code.replace("UAP_XR_PROBE", "")


def test_parse_probe_takes_the_marker_line():
    line = xr_reach.MARK + json.dumps({"ok": True, "n": 1})
    assert xr_reach.parse_probe(["noise", line]) == {"ok": True, "n": 1}
    assert xr_reach.parse_probe(["nothing"])["ok"] is False


# --- screenshot head -------------------------------------------------------------------

def test_look_at_sets_the_head_before_the_capture(monkeypatch, capsys, tmp_path):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(cli, "_exec_probe", lambda c, a: {"ok": True, "pitch": -12.5, "yaw": 30.0,
                                                          "actor": "SORocketItem_6"})
    order = []
    monkeypatch.setattr(cli, "_wants_level_viewport", lambda a: order.append("shot") or True)
    monkeypatch.setattr(cli, "_screenshot_level_viewport",
                        lambda a, p: {"ok": True, "exists": True, "file": p})
    assert cli.main(["screenshot", str(tmp_path / "a.png"), "--head-at", "SORocketItem_6"]) == 0
    pose = [c for c in calls if c[0] == "InjectXRControllerPose"][-1][1]
    assert pose["Hand"] == "Head" and pose["Position"]["Z"] == 170.0
    assert pose["Orientation"]["Pitch"] == -12.5 and pose["Orientation"]["Yaw"] == 30.0
    assert _out(capsys)["head_pose"]["looking_at"] == "SORocketItem_6"
