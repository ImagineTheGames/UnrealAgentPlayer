"""Net-emulation console commands without a live PIE world assert the editor (2026-11-15):
`uap exec` (Python execute_console_command) and `uap rc ExecuteConsoleCommand` refuse them."""

import json

import pytest

from unreal_agent_player import cli

CODE = "unreal.SystemLibrary.execute_console_command(w, 'NetEmulation.Off')"


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


@pytest.mark.parametrize("code", [CODE, "execute_console_command(w, 'net PktLag=100')",
                                  "execute_console_command(w, 'PktLoss 5')"])
def test_exec_refuses_net_emulation_without_pie(monkeypatch, capsys, code):
    _pie(monkeypatch, False)
    ran = _fake_exec(monkeypatch)
    assert cli.main(["exec", code]) == 1
    assert "needs a live PIE world" in _out(capsys)["error"]
    assert not ran


def test_exec_allows_net_emulation_with_pie(monkeypatch, capsys):
    _pie(monkeypatch, True)
    ran = _fake_exec(monkeypatch)
    assert cli.main(["exec", CODE]) == 0
    assert ran


def test_rc_refuses_net_emulation_without_pie(monkeypatch, capsys):
    seen = _pie(monkeypatch, False)
    assert cli.main(["rc", "ExecuteConsoleCommand", '{"Command": "NetEmulation.Off"}']) == 1
    assert "needs a live PIE world" in _out(capsys)["error"]
    assert all(f != "ExecuteConsoleCommand" for f, _ in seen)


def test_rc_allows_net_emulation_with_pie(monkeypatch, capsys):
    seen = _pie(monkeypatch, True)
    assert cli.main(["rc", "ExecuteConsoleCommand", '{"Command": "NetEmulation.Off"}']) == 0
    assert any(f == "ExecuteConsoleCommand" for f, _ in seen)


def test_rc_other_console_command_untouched(monkeypatch, capsys):
    seen = _pie(monkeypatch, False)
    assert cli.main(["rc", "ExecuteConsoleCommand", '{"Command": "stat unit"}']) == 0
    assert all(f != "IsInPIE" for f, _ in seen)
