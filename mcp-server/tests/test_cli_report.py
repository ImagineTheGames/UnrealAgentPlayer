from unreal_agent_player.cli import main


def test_report_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    assert main(["report", "start", "does the door open"]) == 0
    assert main(["report", "assert", "door opens", "pass", "bIsOpen=true"]) == 0
    assert main(["report", "finish", "pass", "verified"]) == 0
    runs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(runs) == 1
    html = (runs[0] / "index.html").read_text(encoding="utf-8")
    assert "door opens" in html
    assert not (tmp_path / ".active").exists()  # cleared on finish


def test_assert_without_start_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    assert main(["report", "assert", "x", "pass", "y"]) == 2
