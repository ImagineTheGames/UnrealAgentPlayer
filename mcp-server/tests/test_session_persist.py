from unreal_agent_player.reporting import session as sess
from unreal_agent_player.reporting.session import ReportSession


def test_load_roundtrips_datajson(tmp_path):
    s = ReportSession(task="q", run_dir=tmp_path / "run", quote="hi", project="P")
    s.add_assertion("a", True, "ev")
    s.add_note("n")
    loaded = ReportSession.load(tmp_path / "run")
    assert loaded.task == "q"
    assert loaded.project == "P"
    assert loaded.assertions == [{"label": "a", "passed": True, "evidence": "ev"}]
    assert loaded.notes == [{"text": "n", "section": None}]


def test_active_pointer_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    assert sess.get_active_run() is None
    s = sess.start_session(task="hello world")
    assert sess.get_active_run() == s.run_dir


def test_clear_active_pointer(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    s = sess.start_session(task="hello world")
    sess.clear_active_run()
    assert sess.get_active_run() is None
