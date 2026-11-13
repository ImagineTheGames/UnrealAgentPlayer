# ruff: noqa: S102, RUF012
"""`uap exec --client N` must run against client N's PIE world, never the host's [17tm466zztw].

The editor-side preamble is real Python, so it is exercised here against a fake `unreal` module
holding one world per PIE client. That is the part that was broken: `exec` ignored `--client`.
"""

import contextlib
import io
import json
import re
import sys
import types

import pytest

from unreal_agent_player import cli
from unreal_agent_player import exec_client as xc
from unreal_agent_player.errors import AgentError


class _Pkg:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class _World:
    def __init__(self, name, pkg):
        self._name = name
        self._outer = _Pkg(pkg)

    def get_name(self):
        return self._name

    def get_outer(self):
        return self._outer


class _EditorLevelLibrary:
    HOST = None

    @staticmethod
    def get_game_world():
        return _EditorLevelLibrary.HOST


class _UnrealEditorSubsystem:
    def get_game_world(self):
        return _EditorLevelLibrary.HOST


def _fake_unreal(clients):
    """clients: {pie_index: world}. The host world is the lowest index, like the real editor."""
    mod = types.ModuleType("unreal")
    mod.World = _World
    mod.ObjectIterator = lambda cls: iter(
        [_World("Editor_Map", "/Game/Maps/Editor_Map")] + list(clients.values()))
    elib = type("EditorLevelLibrary", (_EditorLevelLibrary,), {})
    _EditorLevelLibrary.HOST = clients[min(clients)] if clients else None
    mod.EditorLevelLibrary = elib
    mod.UnrealEditorSubsystem = _UnrealEditorSubsystem
    mod.get_editor_subsystem = lambda cls: cls()
    return mod


def _run(code, selector, clients, monkeypatch):
    """Run the generated code with a fake `unreal`; return (stdout lines, globals)."""
    fake = _fake_unreal(clients)
    monkeypatch.setitem(sys.modules, "unreal", fake)
    text = xc.build_client_code(code, selector)
    g: dict = {}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(text, "<test>", "exec"), g)
    return buf.getvalue().splitlines(), g, fake


def _two_clients():
    return {0: _World("Map", "/Game/Maps/UEDPIE_0_Map"),
            1: _World("Map", "/Game/Maps/UEDPIE_1_Map")}


def test_generated_code_is_valid_python_and_has_no_py_trigger():
    text = xc.build_client_code("print('a.py')\nx = 1\n", "1")
    compile(text, "<gen>", "exec")
    assert not re.search(r"\.py", text, re.IGNORECASE), "UE would read this as a script path"


def test_client_one_gets_world_one_not_the_host(monkeypatch):
    clients = _two_clients()
    code = "print('W', UAP_CLIENT, id(UAP_WORLD))"
    lines0, _, _ = _run(code, "0", clients, monkeypatch)
    lines1, _, _ = _run(code, "1", clients, monkeypatch)
    w0 = next(ln for ln in lines0 if ln.startswith("W ")).split()
    w1 = next(ln for ln in lines1 if ln.startswith("W ")).split()
    assert w0[1] == "0" and w1[1] == "1"
    assert w0[2] == str(id(clients[0])) and w1[2] == str(id(clients[1]))
    assert w0[2] != w1[2], "two clients must be two different worlds"


def test_get_game_world_is_refused_when_it_would_read_the_host(monkeypatch):
    """unreal's classes are immutable: get_game_world() cannot be pointed at client 1, so a
    script that calls it under --client 1 must NOT run (it would read the host and say ok)."""
    code = "import unreal\nran = True\nunreal.EditorLevelLibrary.get_game_world()"
    lines, g, _ = _run(code, "1", _two_clients(), monkeypatch)
    assert "ran" not in g
    _, info, refusal = xc.parse_client_output(lines)
    assert info is None
    assert "get_game_world()" in refusal and "UAP_WORLD" in refusal


def test_get_game_world_is_fine_when_it_already_is_the_selected_client(monkeypatch):
    code = "import unreal\nran = True\nw = unreal.EditorLevelLibrary.get_game_world()"
    lines, g, _ = _run(code, "0", _two_clients(), monkeypatch)
    assert g.get("ran") is True
    assert xc.parse_client_output(lines)[1]["index"] == 0


def test_missing_client_is_refused_and_script_does_not_run(monkeypatch):
    lines, g, _ = _run("ran = True", "5", _two_clients(), monkeypatch)
    assert "ran" not in g, "the script must NOT run against some other world"
    _, info, refusal = xc.parse_client_output(lines)
    assert info is None
    assert "no client 5" in refusal
    assert "0 (/Game/Maps/UEDPIE_0_Map)" in refusal and "1 (/Game/Maps/UEDPIE_1_Map)" in refusal


def test_no_pie_world_is_refused(monkeypatch):
    lines, g, _ = _run("ran = True", "0", {}, monkeypatch)
    assert "ran" not in g
    _, _, refusal = xc.parse_client_output(lines)
    assert "no PIE world" in refusal


def test_late_joined_client_two_resolves_by_index_not_order(monkeypatch):
    clients = {0: _World("Map", "/Game/Maps/UEDPIE_0_Map"),
               2: _World("Map", "/Game/Maps/UEDPIE_2_Map"),
               1: _World("Map", "/Game/Maps/UEDPIE_1_Map")}
    _lines, g, _ = _run("got = UAP_WORLD", "2", clients, monkeypatch)
    assert g["got"] is clients[2]


def test_client_all_and_garbage_are_refused():
    with pytest.raises(ValueError, match="one script, one world"):
        xc.build_client_code("pass", "all")
    with pytest.raises(ValueError, match="PIE instance index"):
        xc.build_client_code("pass", "left")


def test_parse_strips_markers_and_keeps_user_output():
    out = [f"{xc.MARK_WORLD} 1|UEDPIE_1_Map\nhello\n", "second"]
    kept, info, refusal = xc.parse_client_output(out)
    assert info == {"index": 1, "world": "UEDPIE_1_Map"}
    assert refusal is None
    assert "".join(kept) == "hello\nsecond"


# --- the CLI verb ---------------------------------------------------------------------------

class _FakeExec:
    """Stands in for PythonRemoteExecClient: runs the code against a fake `unreal`."""
    last_target = {"pid": 1, "role": "editor", "project": "p"}
    instances: list = []

    def __init__(self, **_kw):
        _FakeExec.instances.append(self)
        self.ran = []

    def exec_python(self, code, **_kw):
        self.ran.append(code)
        buf = io.StringIO()
        ok = True
        with contextlib.redirect_stdout(buf):
            try:
                exec(compile(code, "<remote>", "exec"), {})
            except Exception as exc:
                ok = False
                print(f"Traceback: {exc!r}")
        return {"success": ok, "result": "None", "output": [{"output": buf.getvalue()}]}


@pytest.fixture
def _cli(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _FakeExec)
    monkeypatch.setattr(cli, "_capture", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_CLIENT", [None])
    _FakeExec.instances = []
    monkeypatch.setitem(sys.modules, "unreal", _fake_unreal(_two_clients()))


def _exec_json(capsys, code):
    import argparse
    rc = cli._exec(argparse.Namespace(code=code, project=None, instance=None))
    return rc, json.loads(capsys.readouterr().out)


def test_cli_exec_client_reports_the_world_it_ran_in(_cli, capsys):
    cli._CLIENT[0] = "1"
    rc, body = _exec_json(capsys, "print('hi', UAP_CLIENT)")
    assert rc == 0 and body["ok"] is True
    assert body["client"]["index"] == 1
    assert "hi 1" in "".join(body["output"])
    assert "@@UAP" not in json.dumps(body["output"])


def test_cli_exec_missing_client_is_ok_false_with_the_reason(_cli, capsys):
    cli._CLIENT[0] = "9"
    rc, body = _exec_json(capsys, "print('should not run')")
    assert rc == 1 and body["ok"] is False
    assert "no client 9" in body["error"] and "NOT run" in body["error"]
    assert "should not run" not in "".join(body["output"])


def test_cli_exec_client_all_is_refused_before_anything_is_sent(_cli, capsys):
    cli._CLIENT[0] = "all"
    rc, body = _exec_json(capsys, "print(1)")
    assert rc == 1 and body["ok"] is False
    assert "one script, one world" in json.dumps(body)
    assert all(not i.ran for i in _FakeExec.instances), "nothing may reach the editor"


def test_cli_exec_without_client_is_unchanged(_cli, capsys):
    rc, body = _exec_json(capsys, "print('plain')")
    assert rc == 0 and body["ok"] is True
    assert "client" not in body
    assert _FakeExec.instances[0].ran == ["print('plain')"], "no preamble without --client"


def test_cli_exec_client_whose_preamble_never_reports_is_not_trusted(_cli, capsys, monkeypatch):
    """If the markers never come back the script may have run against the host: say so."""
    cli._CLIENT[0] = "1"
    monkeypatch.setattr(_FakeExec, "exec_python",
                        lambda self, code, **k: {"success": True, "result": "None",
                                                 "output": [{"output": "host-read\n"}]})
    rc, body = _exec_json(capsys, "print('x')")
    assert rc == 1 and body["ok"] is False
    assert "could not be honoured" in body["error"]


def test_cli_exec_client_zero_on_standalone_is_passed_through(_cli, capsys, monkeypatch):
    cli._CLIENT[0] = "0"
    monkeypatch.setattr(cli, "_standalone_instance_selected", lambda a, t: True)
    rc, body = _exec_json(capsys, "print('plain')")
    assert rc == 0 and "client" not in body
    assert _FakeExec.instances[0].ran == ["print('plain')"]


def test_agent_error_type_is_importable():
    assert issubclass(AgentError, Exception)


def test_no_world_reference_is_left_in_the_exec_globals(monkeypatch):
    """Remote exec keeps globals between calls and a UWorld wrapper in them roots the world: a PIE
    world still referenced after PIE ends is a 'World Memory Leaks' editor crash."""
    _, g, _ = _run("keep = 1", "1", _two_clients(), monkeypatch)
    leftovers = [k for k in g if k.startswith("_uap_") or k in ("UAP_WORLD", "UAP_CLIENT")]
    assert leftovers == []
    assert g["keep"] == 1                       # the script's own globals are its own business
    _, g2, _ = _run("x = 1", "7", _two_clients(), monkeypatch)       # refused path
    assert [k for k in g2 if k.startswith(("_uap_", "UAP_"))] == []


def test_no_world_reference_is_left_when_the_script_raises(monkeypatch):
    fake = _fake_unreal(_two_clients())
    monkeypatch.setitem(sys.modules, "unreal", fake)
    g: dict = {}
    text = xc.build_client_code("raise RuntimeError('boom')", "0")
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(RuntimeError, match="boom"):
        exec(compile(text, "<test>", "exec"), g)
    assert [k for k in g if k.startswith(("_uap_", "UAP_"))] == []
