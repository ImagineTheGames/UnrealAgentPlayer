import json
from pathlib import Path

from unreal_agent_player.reporting.session import ReportSession


def _new(tmp_path: Path) -> ReportSession:
    return ReportSession(task="Tier-1 proof", project="SchoolsOut",
                         run_dir=tmp_path / "run", quote="q")


def test_session_persists_data_json_on_creation(tmp_path):
    s = _new(tmp_path)
    data_file = tmp_path / "run" / "data.json"
    assert data_file.exists()
    data = json.loads(data_file.read_text(encoding="utf-8"))
    assert data["task"] == "Tier-1 proof"
    assert data["project"] == "SchoolsOut"
    assert data["status"] == "running"
    assert data["quote"] == "q"
    assert data["assertions"] == [] and data["timeline"] == []


def test_session_appends_persist_immediately(tmp_path):
    s = _new(tmp_path)
    s.add_assertion("pawn moved", True, "9400->9874")
    s.add_note("loaded L_DevTest")
    s.add_tool_call("pie_start", {}, ok=True, ms=1200)
    s.set_perf({"frame_ms": 11.2, "gpu_ms": 4.1})
    s.set_env({"plugin_version": "0.0.1"})
    s.add_logs([{"verbosity": "Warning", "category": "X", "line": "y"}])

    data = json.loads((tmp_path / "run" / "data.json").read_text(encoding="utf-8"))
    assert data["assertions"][0] == {"label": "pawn moved", "passed": True, "evidence": "9400->9874"}
    assert data["notes"][0]["text"] == "loaded L_DevTest"
    assert data["timeline"][0]["tool"] == "pie_start"
    assert data["perf"]["gpu_ms"] == 4.1
    assert data["env"]["plugin_version"] == "0.0.1"
    assert data["logs"][0]["verbosity"] == "Warning"


def test_finish_sets_status_summary_duration(tmp_path):
    s = _new(tmp_path)
    s.finish("pass", "it worked")
    data = json.loads((tmp_path / "run" / "data.json").read_text(encoding="utf-8"))
    assert data["status"] == "pass"
    assert data["summary"] == "it worked"
    assert data["finished"] is not None
    assert isinstance(data["duration_s"], (int, float))


def test_add_screenshot_copies_and_indexes(tmp_path):
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG fake")
    s = _new(tmp_path)
    fname = s.add_screenshot(str(src), caption="hub")
    assert fname == "screenshots/000.png"
    assert (tmp_path / "run" / "screenshots" / "000.png").read_bytes() == b"\x89PNG fake"
    assert s.screenshots[0]["file"] == "screenshots/000.png"
    assert s.screenshots[0]["caption"] == "hub"


def test_set_caption_by_index_and_default_latest(tmp_path):
    src = tmp_path / "shot.png"; src.write_bytes(b"x")
    s = _new(tmp_path)
    s.add_screenshot(str(src))
    s.add_screenshot(str(src))
    s.set_caption(0, "first")
    s.set_caption(None, "latest")  # None -> most recent
    assert s.screenshots[0]["caption"] == "first"
    assert s.screenshots[1]["caption"] == "latest"


def test_add_missing_screenshot_records_missing_flag(tmp_path):
    s = _new(tmp_path)
    fname = s.add_screenshot(str(tmp_path / "nope.png"))
    assert s.screenshots[0]["missing"] is True
    assert fname is None


from unreal_agent_player.reporting import session as sess_mod


def test_start_session_sets_active_and_creates_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    sess_mod.clear_active()
    s = sess_mod.start_session(task="My Task!", project="P")
    assert sess_mod.active() is s
    assert s.run_dir.parent == tmp_path
    assert "my-task" in s.run_dir.name
    assert (s.run_dir / "data.json").exists()


def test_double_start_finishes_previous(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    sess_mod.clear_active()
    first = sess_mod.start_session(task="first")
    second = sess_mod.start_session(task="second")
    assert first.status == "incomplete"
    assert sess_mod.active() is second


def test_clear_active(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    sess_mod.start_session(task="x")
    sess_mod.clear_active()
    assert sess_mod.active() is None
