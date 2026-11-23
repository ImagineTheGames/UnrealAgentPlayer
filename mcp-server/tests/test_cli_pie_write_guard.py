"""`uap exec` refuses property/transform/attach writes on live PIE worlds (CRASH-0BD72FF1)."""

import json
from typing import ClassVar

import pytest

from unreal_agent_player import cli


class _Fake:
    last_target = None
    ran: ClassVar[list] = []

    def __init__(self, *a, **k):
        pass

    def exec_python(self, code):
        _Fake.ran.append(code)
        return {"success": True, "result": "None", "output": []}


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _Fake.ran = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Fake)
    monkeypatch.setattr(cli, "_exec_client_is_noop", lambda *a, **k: True)


WRITES = [
    "c.set_editor_property('fov', 90)",
    "c.set_editor_properties({'a': 1})",
    "c.set_relative_location(unreal.Vector(), False, False)",
    "c.set_relative_rotation(r)", "c.set_relative_scale3d(v)", "c.set_relative_transform(t)",
    "c.set_world_location(v)", "c.set_world_rotation(r)",
    "a.set_actor_location(v, False, False)", "a.set_actor_rotation(r, False)",
    "a.set_actor_transform(t)", "a.set_actor_scale3d(v)",
    "c.k2_set_world_location(v)", "c.k2_attach_to_component(p)",
    "c.attach_to_component(p)", "a.attach_to_actor(o)", "a.rerun_construction_scripts()",
]


@pytest.mark.parametrize("code", WRITES)
def test_writes_refused_with_client(code, capsys):
    assert cli.main(["exec", code, "--client", "1"]) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is False
    assert "uap input" in out["error"] and "uap xr pose" in out["error"]
    assert "--allow-pie-writes" in out["error"] and "NOT run" in out["error"]
    assert not _Fake.ran


@pytest.mark.parametrize("code", [
    "w = 'UEDPIE_1_Map'; c.set_editor_property('a', 1)",
    "unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world(); a.set_actor_location(v)",
])
def test_writes_refused_when_code_resolves_pie_world(code, capsys):
    assert cli.main(["exec", code]) == 1
    assert not _Fake.ran


@pytest.mark.parametrize("code", [
    "c.get_editor_property('fov')", "c.get_relative_location()", "a.get_actor_location()",
    "print(unreal.UEDPIE_1)",
])
def test_reads_allowed_with_client(code, capsys):
    assert cli.main(["exec", code, "--client", "1"]) == 0
    assert _Fake.ran


def test_editor_world_write_without_pie_is_allowed(capsys):
    assert cli.main(["exec", "a.set_actor_location(v, False, False)"]) == 0
    assert _Fake.ran


def test_escape_hatch_runs_and_warns(capsys):
    assert cli.main(["exec", "c.set_editor_property('fov', 90)", "--client", "1",
                     "--allow-pie-writes"]) == 0
    assert _Fake.ran
    assert "--allow-pie-writes" in capsys.readouterr().err


def test_exec_file_refused_and_hatch(tmp_path, capsys):
    f = tmp_path / "s.py"
    f.write_text("c.set_editor_property('fov', 90)\n", encoding="utf-8")
    assert cli.main(["exec-file", str(f), "--client", "1"]) == 1
    assert not _Fake.ran
    assert cli.main(["exec-file", str(f), "--client", "1", "--allow-pie-writes"]) == 0
    assert _Fake.ran


def test_help_documents_guard(capsys):
    assert cli.main(["help", "level"]) == 0
    out = capsys.readouterr().out
    assert "--allow-pie-writes" in out and "0BD72FF1" in out
