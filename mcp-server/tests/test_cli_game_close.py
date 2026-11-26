"""`uap game close`: in-engine window close of a -game client (17tm46799m0)."""

import json

import pytest

from unreal_agent_player import cli


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_close_calls_plugin_verb(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_resolve_instance_target",
                        lambda i, p: {"port": 30121, "preset": "x", "pid": 1, "role": "game"})

    def fake(func, params, project=None):
        calls.append(func)
        return json.dumps({"ok": True, "scheduled": True, "window_title": "SchoolsOut"})

    monkeypatch.setattr(cli, "_rc_call", fake)
    rc = cli.main(["game", "close", "--instance", "pid:1"])
    out = _out(capsys)
    assert rc == 0 and out["ok"] and out["scheduled"]
    assert calls == ["CloseGameWindow"]


def test_close_refuses_without_instance(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_rc_call", lambda *a, **k: pytest.fail("must not call"))
    rc = cli.main(["game", "close"])
    out = _out(capsys)
    assert rc == 1 and not out["ok"] and "--instance" in out["error"]


def test_close_relays_plugin_refusal(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_instance_target",
                        lambda i, p: {"port": 1, "preset": "x", "pid": 1, "role": "game"})
    monkeypatch.setattr(cli, "_rc_call", lambda *a, **k: json.dumps({"ok": False, "error": "editor"}))
    rc = cli.main(["game", "close", "--instance", "pid:1"])
    assert rc == 1 and _out(capsys)["error"] == "editor"


def test_reach_on_game_client_uses_runtime_probe(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_instance_target",
                        lambda i, p: {"port": 1, "preset": "x", "pid": 1, "role": "game"})
    monkeypatch.setattr(cli, "_exec_probe", lambda *a, **k: pytest.fail("no editor python in -game"))
    monkeypatch.setattr(cli, "_REACH_SETTLE_S", 0)
    state = {"grab": [10.0, 0.0, 0.0]}
    calls = []

    def probe(func, params, project=None, *, needs=None):
        calls.append((func, params))
        return {"ok": True, "actor": "Ball", "target_how": "actor location", "target": [0.0, 0.0, 0.0],
                "grab": list(state["grab"]), "grab_component": "GrabSphere", "grab_radius": 5.0,
                "controller": "GripR", "delta_tracking": [-10.0, 0.0, 0.0],
                "ctrl_rel_loc": [1.0, 2.0, 3.0], "ctrl_rel_rot": [0.0, 0.0, 0.0]}

    def rc(func, params, project=None):
        assert func == "InjectXRControllerPose"
        state["grab"] = [0.0, 0.0, 0.0]
        return True

    monkeypatch.setattr(cli, "_rc_json", probe)
    monkeypatch.setattr(cli, "_rc_call", rc)
    rc_code = cli.main(["xr", "reach", "right", "Ball", "--instance", "pid:1"])
    out = _out(capsys)
    assert rc_code == 0 and out["reached"] and out["actor"] == "Ball"
    assert calls[0] == ("ReachProbe", {"Hand": "right", "Target": "Ball", "OffsetCsv": ""})


def test_reach_game_probe_passes_offset(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_instance_target",
                        lambda i, p: {"port": 1, "preset": "x", "pid": 1, "role": "game"})
    seen = {}

    def probe(func, params, project=None, *, needs=None):
        seen.update(params)
        return {"ok": False, "error": "stop"}

    monkeypatch.setattr(cli, "_rc_json", probe)
    cli.main(["xr", "reach", "left", "Ball.Mesh", "--offset", "1,2,3", "--instance", "pid:1"])
    assert seen["OffsetCsv"] == "1.0,2.0,3.0" and seen["Hand"] == "left"
    assert _out(capsys)["error"] == "stop"
