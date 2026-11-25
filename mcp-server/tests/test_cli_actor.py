"""`uap actor teleport`: move any PIE actor in-engine (gap from verify 0efa9e9d: janitor floors)."""

import json
import warnings

import pytest

from unreal_agent_player import actor_move, cli


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.delenv("UAP_CLIENT", raising=False)


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _wire(monkeypatch, result):
    seen = []

    def fake(code, args):
        seen.append((code, cli._CLIENT[0]))
        return result

    monkeypatch.setattr(cli, "_exec_probe", fake)
    return seen


def test_probe_code_compiles_and_writes_no_property():
    for code in (actor_move.teleport_code("BP_Janitor_C", 1, 2, 3),
                 actor_move.teleport_code('Ca"p', None, None, 300, 90.0, True)):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(code, "probe", "exec")
        assert cli._EXEC_PIE_WRITE_RE.search(code) is None
        assert ".py" not in code
        assert "UEDPIE" in code and "stop_movement" in code and "_u.log(" in code


def test_probe_embeds_values_and_escapes_actor():
    code = actor_move.teleport_code('a"b', 1, None, 3.5)
    assert 'a\\"b' in code and "_dx, _dy, _dz = 1.0, None, 3.5" in code


def test_teleport_default_is_server_world(monkeypatch, capsys):
    seen = _wire(monkeypatch, {"ok": True, "to": [1, 2, 3], "moved": []})
    assert cli.main(["actor", "teleport", "BP_Janitor_C", "--x", "1", "--y", "2", "--z", "3"]) == 0
    out = _out(capsys)
    assert out["ok"] and out["action"] == "actor teleport" and out["world"] == "server"
    assert seen[0][1] == "0" and "BP_Janitor_C" in seen[0][0]


def test_world_client_defaults_to_client_1(monkeypatch, capsys):
    seen = _wire(monkeypatch, {"ok": True})
    assert cli.main(["actor", "teleport", "X", "--z", "5", "--world", "client"]) == 0
    assert seen[0][1] == "1"


def test_needs_a_coordinate(monkeypatch, capsys):
    seen = _wire(monkeypatch, {"ok": True})
    assert cli.main(["actor", "teleport", "X"]) == 1
    assert not seen and "at least one" in _out(capsys)["error"]


def test_probe_failure_is_nonzero(monkeypatch, capsys):
    _wire(monkeypatch, {"ok": False, "error": "refusing: world X is not a PIE world"})
    assert cli.main(["actor", "teleport", "X", "--z", "0"]) == 1
    assert "PIE" in _out(capsys)["error"]


def test_no_pie_message(monkeypatch, capsys):
    _wire(monkeypatch, {"ok": False, "error": "the editor-side probe printed nothing (x)"})
    assert cli.main(["actor", "teleport", "X", "--z", "0"]) == 1
    assert "start PIE" in _out(capsys)["error"]


def test_actor_is_a_guarded_instance_verb():
    assert "actor" in cli._MACHINE_GUARDED and "actor" in cli._LEASE_GUARDED
    assert "actor" in cli._INSTANCE_RC_CMDS
