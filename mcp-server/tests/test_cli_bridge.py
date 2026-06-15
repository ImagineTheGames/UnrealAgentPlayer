from unreal_agent_player import cli
from unreal_agent_player.reporting import session as sess


def test_rc_verb_calls_preset_and_autocaptures(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_rc_call", lambda func, params: "0.0.1")
    assert cli.main(["report", "start", "q"]) == 0
    assert cli.main(["rc", "GetPluginVersion"]) == 0
    run = sess.get_active_run()
    loaded = sess.ReportSession.load(run)
    assert any(e["tool"] == "rc:GetPluginVersion" for e in loaded.timeline)


def test_status_maps_unreachable(monkeypatch, capsys):
    def boom(func, params):
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
    from unreal_agent_player.cli import _parse_rc_params
    assert _parse_rc_params([]) == {}
    # key=value with coercion
    assert _parse_rc_params(["Command=stat fps"]) == {"Command": "stat fps"}
    assert _parse_rc_params(["KeyName=E", "bPressed=true", "Count=3"]) == {
        "KeyName": "E", "bPressed": True, "Count": 3}
    # single JSON-object token escape hatch
    assert _parse_rc_params(['{"Command":"stat fps"}']) == {"Command": "stat fps"}
    # bad token (no '=' and not JSON) -> ValueError
    import pytest as _pt
    with _pt.raises(ValueError):
        _parse_rc_params(["notakeyvalue"])
