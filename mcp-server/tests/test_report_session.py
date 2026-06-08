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
