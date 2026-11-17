"""None-world execute_console_command and travel commands without PIE crashed the shared editor
(UnrealEngine.cpp:16996 HandleInvalidWorldContext, 2026-11-15/16)."""

import json

import pytest

from unreal_agent_player import cli
from unreal_agent_player.tools.console import none_world_console_call, is_world_command
from test_cli_net_emulation import _fake_exec, _out, _pie


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
