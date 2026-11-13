from unreal_agent_player import cli
from unreal_agent_player.reporting import session as sess


def test_rc_verb_calls_preset_and_autocaptures(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_RC_PORT", "30010")  # pin the port (no exec resolution in tests)
    monkeypatch.setattr(cli, "_rc_call", lambda func, params, project=None: "0.0.1")
    assert cli.main(["report", "start", "q"]) == 0
    assert cli.main(["rc", "GetPluginVersion"]) == 0
    run = sess.get_active_run()
    loaded = sess.ReportSession.load(run)
    assert any(e["tool"] == "rc:GetPluginVersion" for e in loaded.timeline)


def test_status_maps_unreachable(monkeypatch, capsys):
    monkeypatch.setenv("UAP_RC_PORT", "30010")  # pin the port (no exec resolution in tests)

    def boom(func, params, project=None):
        from unreal_agent_player.errors import AgentError, ErrorCode
        raise AgentError(ErrorCode.UE_UNREACHABLE, "down")
    monkeypatch.setattr(cli, "_rc_call", boom)
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert '"rc_reachable": false' in out
    assert rc == 1


def test_exec_file_reads_and_runs(tmp_path, monkeypatch):
    captured = {}

    class _FakeExec:
        def __init__(self, **kw):
            pass
        def exec_python(self, code):
            captured["code"] = code
            return {"success": True, "result": "ok", "output": []}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", _FakeExec)
    f = tmp_path / "snippet.py"
    f.write_text("print('hi from file')", encoding="utf-8")
    assert cli.main(["exec-file", str(f)]) == 0
    assert captured["code"] == "print('hi from file')"


def test_parse_rc_params_keyvalue_and_json():
    """No declared types available -> the historical heuristic, and every key whose text it
    RETYPED is reported as a guess. That is what lets `uap rc` say it inferred a JSON type
    rather than knowing it -- a value that went out as the caller's own string cannot have been
    silently downgraded, so it is not flagged.
    """
    from unreal_agent_player.cli import _parse_rc_params
    assert _parse_rc_params([]) == ({}, [])
    # key=value with coercion
    assert _parse_rc_params(["Command=stat fps"]) == ({"Command": "stat fps"}, [])
    assert _parse_rc_params(["KeyName=E", "bPressed=true", "Count=3"]) == (
        {"KeyName": "E", "bPressed": True, "Count": 3}, ["bPressed", "Count"])
    # single JSON-object token escape hatch: real JSON types already, nothing guessed
    assert _parse_rc_params(['{"Command":"stat fps"}']) == ({"Command": "stat fps"}, [])
    # bad token (no '=' and not JSON) -> ValueError
    import pytest as _pt
    with _pt.raises(ValueError):
        _parse_rc_params(["notakeyvalue"])


# --- exec-file: a ".py" in the text must not make UE read the code as a script path ---------
# UE's ExecuteFile mode takes everything before the FIRST ".py" as the file path, so a script
# opening with `# builder.py - ...` failed with "file not found" [17tm466zx66].

import re

from unreal_agent_player.transport import guard_py_path_trigger

_UE_TRIGGER = re.compile(r"\.py", re.IGNORECASE)


def test_guard_leaves_code_without_py_untouched():
    code = "import unreal\nprint('hi')\n"
    assert guard_py_path_trigger(code) == code


def test_guard_hides_every_py_from_the_engine_and_keeps_the_code_meaning():
    code = ("# builder.py - build the track\n"
            "x = 'a.PY b.py'\n"
            "y = r'C:\\scripts\\t.py'\n"
            "z = x + y + 'tail'\n")
    wrapped = guard_py_path_trigger(code)
    assert not _UE_TRIGGER.search(wrapped)
    g: dict = {}
    exec(wrapped, g)  # noqa: S102
    ref: dict = {}
    exec(code, ref)  # noqa: S102
    assert g["x"] == ref["x"] == "a.PY b.py"
    assert g["y"] == ref["y"]
    assert g["z"] == ref["z"]


def test_exec_file_with_py_comment_first_line_sends_no_path_trigger(tmp_path, monkeypatch):
    captured = {}

    class _FakeExec:
        def __init__(self, **kw):
            pass
        def exec_python(self, code):
            captured["code"] = code
            return {"success": True, "result": "ok", "output": []}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", _FakeExec)
    f = tmp_path / "t9.py"
    f.write_text("# t9_check.py - drive the marble\nanswer = 6 * 7\n", encoding="utf-8")
    assert cli.main(["exec-file", str(f)]) == 0
    assert not _UE_TRIGGER.search(captured["code"])
    g: dict = {}
    exec(captured["code"], g)  # noqa: S102
    assert g["answer"] == 42
