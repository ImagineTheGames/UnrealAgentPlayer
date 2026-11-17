"""None-world execute_console_command and travel commands without PIE crashed the shared editor
(UnrealEngine.cpp:16996 HandleInvalidWorldContext, 2026-11-15/16)."""


import json

import pytest

from unreal_agent_player import cli
from unreal_agent_player.tools.console import is_world_command, none_world_console_call


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _pie(monkeypatch, live):
    seen = []

    def fake(func, params, project=None):
        seen.append((func, dict(params)))
        return live if func == "IsInPIE" else "ok"

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _fake_exec(monkeypatch):
    ran = []

    class Fake:
        last_target = None

        def __init__(self, *a, **k):
            pass

        def exec_python(self, code):
            ran.append(code)
            return {"success": True, "result": "None", "output": []}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", Fake)
    return ran



@pytest.mark.parametrize("code", [
    "unreal.SystemLibrary.execute_console_command(None, 'open /Game/Maps/X')",
    "unreal.SystemLibrary.execute_console_command(None, cmd)",
    "unreal.SystemLibrary.execute_console_command(world_context_object=None, command='servertravel X')",
    'SystemLibrary.execute_console_command(None, "travel X")',
])
@pytest.mark.parametrize("live", [False, True])
def test_none_world_refused(monkeypatch, capsys, code, live):
    _pie(monkeypatch, live)
    ran = _fake_exec(monkeypatch)
    assert cli.main(["exec", code]) == 1
    assert "None world context" in _out(capsys)["error"]
    assert not ran


@pytest.mark.parametrize("code", [
    "unreal.SystemLibrary.execute_console_command(None, 'stat fps')",
    "unreal.SystemLibrary.execute_console_command(world, 'open X')",
    "print(1)",
])
def test_normal_exec_unaffected(monkeypatch, capsys, code):
    _pie(monkeypatch, False)
    ran = _fake_exec(monkeypatch)
    assert cli.main(["exec", code]) == 0
    assert ran


def test_world_command_detection():
    assert is_world_command("open /Game/X") and is_world_command("ServerTravel X")
    assert not is_world_command("openlevel") and not is_world_command("stat unit")
    assert not none_world_console_call("x = 1")


def test_rc_open_refused_without_pie(monkeypatch, capsys):
    seen = _pie(monkeypatch, False)
    assert cli.main(["rc", "ExecuteConsoleCommand", '{"Command": "open /Game/X"}']) == 1
    assert "live PIE world" in _out(capsys)["error"]
    assert all(f != "ExecuteConsoleCommand" for f, _ in seen)


def test_rc_open_allowed_with_pie(monkeypatch, capsys):
    seen = _pie(monkeypatch, True)
    assert cli.main(["rc", "ExecuteConsoleCommand", '{"Command": "open /Game/X"}']) == 0
    assert any(f == "ExecuteConsoleCommand" for f, _ in seen)
