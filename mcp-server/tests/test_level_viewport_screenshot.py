"""`uap screenshot` with no PIE captures the LEVEL EDITOR viewport, and the pose is applied on the
same pane in the same call (ClickUp 17tm466fv84 / 17tm466ft2k).

Two defects: with no PIE the old path queued a frame an idle or unfocused editor never renders,
and a pose set through Python moved a different pane than the capture took. These tests pin the
CLI half: routing, the single-call pose, argument validation, the skew refusal and the absolute
path. The in-engine half (FViewport::Draw + ReadPixels, the pane resolver) is verified live.
"""
import json
import os
import pathlib

import pytest

from unreal_agent_player import cli
from unreal_agent_player.errors import AgentError


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setattr(cli, "_exec_project_name", lambda project: "TestProj")
    monkeypatch.setattr(cli, "_capture", lambda *a, **k: None)


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _plugin(monkeypatch, tmp_path, *, in_pie, level_reply=None, missing_level_verb=False):
    """Stub the RC layer; CaptureLevelViewport writes the file like the plugin does."""
    seen = []

    def fake_call(func, params, project=None):
        seen.append((func, dict(params)))
        if func == "IsInPIE":
            return in_pie
        if func == "CaptureViewportWithUI":
            return True
        if func == "CaptureLevelViewport":
            if missing_level_verb:
                raise AgentError(cli.ErrorCode.UE_OBJECT_NOT_FOUND, "returned 404")
            reply = level_reply if level_reply is not None else {
                "ok": True, "path": params["Filename"], "pane": "FourPanes2x2.Viewport 1.Viewport1",
                "pane_is_active": True, "pane_is_unreal_editor_subsystem_pane": True}
            if reply.get("ok"):
                with open(params["Filename"], "wb") as f:
                    f.write(b"png")
            return json.dumps(reply)
        raise AssertionError(f"unexpected RC call {func}")

    monkeypatch.setattr(cli, "_rc_call", fake_call)
    return seen


def test_no_pie_takes_the_level_viewport_path(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=False)
    shot = str(tmp_path / "a.png")
    assert cli.main(["screenshot", shot]) == 0
    body = _out(capsys)
    assert [f for f, _ in seen] == ["IsInPIE", "CaptureLevelViewport"]
    assert body["ok"] and body["exists"] and body["pane"].endswith("Viewport1")
    assert body["provenance"] == "TestProj"


def test_pie_running_keeps_the_game_viewport_path(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=True)
    shot = str(tmp_path / "a.png")
    pathlib.Path(shot).write_bytes(b"png")   # the PIE verb is async; the file is what the CLI polls for
    assert cli.main(["screenshot", shot]) == 0
    assert [f for f, _ in seen] == ["IsInPIE", "CaptureViewportWithUI"]
    assert _out(capsys)["exists"]


def test_pose_flags_go_to_the_plugin_in_the_same_call(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=False)
    shot = str(tmp_path / "pose.png")
    rc = cli.main(["screenshot", shot, "--location", "100,200,300", "--look-at", "0,0,50",
                   "--fov", "70", "--pane", "Viewport1", "--draw-frames", "5"])
    assert rc == 0
    funcs = [f for f, _ in seen]
    assert funcs == ["CaptureLevelViewport"], "pose flags must not consult IsInPIE or split into two calls"
    pose = json.loads(seen[0][1]["PoseJson"])
    assert pose == {"location": [100, 200, 300], "look_at": [0, 0, 50], "fov": 70.0,
                    "pane": "Viewport1", "frames": 5}
    assert _out(capsys)["ok"]


def test_editor_flag_captures_the_level_viewport_even_in_pie(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=True)
    assert cli.main(["screenshot", str(tmp_path / "e.png"), "--editor"]) == 0
    assert [f for f, _ in seen] == ["CaptureLevelViewport"]
    assert seen[0][1]["PoseJson"] == ""
    _out(capsys)


def test_rotation_is_pitch_yaw_roll(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=False)
    assert cli.main(["screenshot", str(tmp_path / "r.png"), "--location", "1,2,3",
                     "--rotation", "-12,90,0"]) == 0
    assert json.loads(seen[0][1]["PoseJson"])["rotation"] == [-12, 90, 0]
    _out(capsys)


def test_rotation_and_look_at_together_are_refused(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=False)
    rc = cli.main(["screenshot", str(tmp_path / "x.png"), "--rotation", "0,0,0", "--look-at", "1,1,1"])
    body = _out(capsys)
    assert rc == 1 and not body["ok"] and "pass one" in body["error"]
    assert seen == []


def test_bad_vector_is_refused_before_any_call(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=False)
    rc = cli.main(["screenshot", str(tmp_path / "x.png"), "--location", "1,2"])
    body = _out(capsys)
    assert rc == 1 and "3 comma-separated numbers" in body["error"]
    assert seen == []


def test_plugin_refusal_is_relayed_not_swallowed(monkeypatch, tmp_path, capsys):
    _plugin(monkeypatch, tmp_path, in_pie=False,
            level_reply={"ok": False, "error": "pane 'Viewport9' matches nothing; panes: a, b"})
    rc = cli.main(["screenshot", str(tmp_path / "x.png"), "--pane", "Viewport9"])
    body = _out(capsys)
    assert rc == 1 and not body["ok"] and "matches nothing" in body["error"]
    assert not body["exists"]


def test_older_plugin_is_named_as_skew_not_silently_replaced(monkeypatch, tmp_path, capsys):
    """An older plugin lacks CaptureLevelViewport. Falling back to CaptureViewportWithUI would
    answer a different question (it queues a frame an idle editor never draws)."""
    seen = _plugin(monkeypatch, tmp_path, in_pie=False, missing_level_verb=True)
    rc = cli.main(["screenshot", str(tmp_path / "x.png")])
    body = _out(capsys)
    assert rc == 1 and not body["ok"]
    assert "CaptureLevelViewport" in body["error"]
    assert "CaptureViewportWithUI" not in [f for f, _ in seen]


def test_relative_path_is_made_absolute_for_the_engine(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=False)
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "Saved", exist_ok=True)
    assert cli.main(["screenshot", os.path.join("Saved", "rel.png")]) == 0
    sent = seen[-1][1]["Filename"]
    assert os.path.isabs(sent) and sent == str(tmp_path / "Saved" / "rel.png")
    _out(capsys)


def test_viewport_pose_verb_sets_and_reads_back(monkeypatch, capsys):
    seen = []

    def fake_call(func, params, project=None):
        seen.append((func, dict(params)))
        return json.dumps({"ok": True, "pane": "P.Viewport1", "pose_applied": True,
                           "location": [1, 2, 3], "rotation_pitch_yaw_roll": [-12, 0, 0]})

    monkeypatch.setattr(cli, "_rc_call", fake_call)
    assert cli.main(["viewport", "pose", "--location", "1,2,3", "--rotation", "-12,0,0"]) == 0
    assert seen[0][0] == "LevelViewportPose"
    assert json.loads(seen[0][1]["PoseJson"]) == {"location": [1, 2, 3], "rotation": [-12, 0, 0]}
    assert _out(capsys)["pose_applied"] is True


def test_viewport_pose_with_no_flags_is_a_pure_read(monkeypatch, capsys):
    seen = []

    def fake_call(func, params, project=None):
        seen.append((func, dict(params)))
        return json.dumps({"ok": True, "pane": "P.Viewport1", "pose_applied": False})

    monkeypatch.setattr(cli, "_rc_call", fake_call)
    assert cli.main(["viewport", "pose"]) == 0
    assert seen[0][1]["PoseJson"] == ""


def test_new_plugin_verbs_are_in_the_contract():
    from unreal_agent_player import contract
    assert "CaptureLevelViewport" in contract.PURPOSE
    assert "LevelViewportPose" in contract.PURPOSE


@pytest.mark.parametrize("argv, expected", [
    (["--rotation", "-12,90,0"], ["--rotation=-12,90,0"]),
    # PowerShell turns an unquoted `--location=-900,-1300,900` into separate arguments.
    (["--location=-900", "-1300", "900"], ["--location=-900,-1300,900"]),
    (["--location", "-900", "-1300", "900"], ["--location=-900,-1300,900"]),
    (["--location=1,2,3", "9"], ["--location=1,2,3", "9"]),
    (["--look-at", "0,0,50", "--fov", "70"], ["--look-at=0,0,50", "--fov", "70"]),
    (["--location", "abc"], ["--location", "abc"]),
    (["shot.png", "--rotation=-5,0,0"], ["shot.png", "--rotation=-5,0,0"]),
])
def test_vector_flags_survive_powershell_and_negative_values(argv, expected):
    assert cli._glue_negative_vectors(argv) == expected


def test_powershell_split_vector_reaches_the_plugin_intact(monkeypatch, tmp_path, capsys):
    seen = _plugin(monkeypatch, tmp_path, in_pie=False)
    assert cli.main(["screenshot", str(tmp_path / "ps.png"), "--location=-900", "-1300", "900",
                     "--look-at", "0", "0", "0"]) == 0
    pose = json.loads(seen[0][1]["PoseJson"])
    assert pose["location"] == [-900, -1300, 900] and pose["look_at"] == [0, 0, 0]
    _out(capsys)


# --- --client N: the shot is THAT client's own viewport (17tm466zx5y) -------------------------

def _client_plugin(monkeypatch, tmp_path, *, capture_result=True):
    """RC stub that applies the real _with_client, as _rc_call does, so the wire params show."""
    seen = []

    def fake_call(func, params, project=None):
        params = cli._with_client(func, params, project)
        seen.append((func, dict(params)))
        if func == "IsInPIE":
            return True
        if func == "CaptureViewportWithUI":
            return capture_result
        raise AssertionError(func)

    monkeypatch.setattr(cli, "_rc_call", fake_call)
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    monkeypatch.setattr(cli, "_CLIENT", [None])
    return seen


def test_client_flag_reaches_the_plugin_capture(monkeypatch, tmp_path, capsys):
    seen = _client_plugin(monkeypatch, tmp_path)
    shot = str(tmp_path / "c1.png")
    pathlib.Path(shot).write_bytes(b"png")
    assert cli.main(["screenshot", shot, "--client", "1"]) == 0
    assert dict(seen)["CaptureViewportWithUI"]["Client"] == "1"


def test_no_client_flag_sends_no_client_to_the_capture(monkeypatch, tmp_path, capsys):
    seen = _client_plugin(monkeypatch, tmp_path)
    shot = str(tmp_path / "c.png")
    pathlib.Path(shot).write_bytes(b"png")
    assert cli.main(["screenshot", shot]) == 0
    assert "Client" not in dict(seen)["CaptureViewportWithUI"]


def test_plugin_refusal_of_a_client_is_reported_not_a_stale_file(monkeypatch, tmp_path, capsys):
    _client_plugin(monkeypatch, tmp_path, capture_result=False)
    shot = str(tmp_path / "gone.png")
    assert cli.main(["screenshot", shot, "--client", "7"]) == 1
    body = _out(capsys)
    assert not body["ok"] and "client '7'" in body["error"]


def test_client_all_is_refused_for_a_screenshot(monkeypatch, tmp_path, capsys):
    seen = _client_plugin(monkeypatch, tmp_path)
    assert cli.main(["screenshot", str(tmp_path / "a.png"), "--client", "all"]) == 1
    assert "CaptureViewportWithUI" not in [f for f, _ in seen]
    assert "one image" in _out(capsys)["error"]


def test_older_plugin_without_client_param_is_refused_not_answered_with_client_0(monkeypatch, tmp_path, capsys):
    seen = _client_plugin(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_live_contract",
                        lambda project=None: {"CaptureViewportWithUI": ["Filename"]})
    assert cli.main(["screenshot", str(tmp_path / "o.png"), "--client", "1"]) == 1
    assert "CaptureViewportWithUI" not in [f for f, _ in seen]
