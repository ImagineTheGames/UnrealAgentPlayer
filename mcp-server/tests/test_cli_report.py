from unreal_agent_player.cli import main


def test_report_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    # `report finish` now best-effort stops PIE (an RC call). Pin an unreachable RC port so that
    # call fails fast and deterministically instead of touching a live editor on this machine.
    monkeypatch.setenv("UAP_RC_PORT", "1")
    assert main(["report", "start", "does the door open"]) == 0
    assert main(["report", "assert", "door opens", "pass", "bIsOpen=true"]) == 0
    assert main(["report", "finish", "pass", "verified"]) == 0
    # Only count run dirs -- infra like the .rcports port cache also lives under UAP_REPORTS_DIR.
    runs = [p for p in tmp_path.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert len(runs) == 1
    html = (runs[0] / "index.html").read_text(encoding="utf-8")
    assert "door opens" in html
    assert not (tmp_path / ".active").exists()  # cleared on finish


def test_report_finish_autostops_pie(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    import unreal_agent_player.cli as cli
    calls = []

    def fake_rc(func, params, project=None):
        calls.append(func)
        return True if func == "IsInPIE" else None

    monkeypatch.setattr(cli, "_rc_call", fake_rc)
    assert main(["report", "start", "t"]) == 0
    assert main(["report", "finish", "pass", "done"]) == 0
    # PIE was live -> finish must have queried and stopped it.
    assert "IsInPIE" in calls and "StopPIE" in calls


def test_report_finish_keep_pie_skips_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    import unreal_agent_player.cli as cli
    calls = []
    monkeypatch.setattr(cli, "_rc_call", lambda f, p, project=None: calls.append(f))
    assert main(["report", "start", "t"]) == 0
    assert main(["report", "finish", "pass", "done", "--keep-pie"]) == 0
    assert calls == []  # --keep-pie makes finish touch the editor not at all


def test_assert_without_start_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    assert main(["report", "assert", "x", "pass", "y"]) == 2
