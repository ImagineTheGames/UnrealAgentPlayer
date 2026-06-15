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
