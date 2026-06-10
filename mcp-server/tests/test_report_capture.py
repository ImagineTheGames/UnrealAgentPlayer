
from unreal_agent_player.reporting.session import ReportSession, record_call


def _s(tmp_path) -> ReportSession:
    return ReportSession(task="t", run_dir=tmp_path / "r", quote="q")


def test_record_call_appends_timeline(tmp_path):
    s = _s(tmp_path)
    record_call(s, "pie_start", {"a": 1}, {"ok": True}, 12)
    assert s.timeline[0]["tool"] == "pie_start"
    assert s.timeline[0]["ok"] is True and s.timeline[0]["ms"] == 12


def test_record_call_ok_false_from_error_body(tmp_path):
    s = _s(tmp_path)
    record_call(s, "input_key", {}, {"ok": False, "error": {"message": "boom"}}, 3)
    assert s.timeline[0]["ok"] is False
    assert s.timeline[0]["error"] == "boom"


def test_record_call_files_screenshot(tmp_path):
    src = tmp_path / "x.png"; src.write_bytes(b"PNG")
    s = _s(tmp_path)
    record_call(s, "screenshot_viewport", {}, {"ok": True, "path": str(src)}, 50)
    assert s.screenshots[0]["file"] == "screenshots/000.png"


def test_record_call_captures_perf(tmp_path):
    s = _s(tmp_path)
    record_call(s, "perf_stat", {}, {"ok": True, "parsed": {"frame_ms": 11.2, "gpu_ms": 4.1}}, 8)
    assert s.perf == {"frame_ms": 11.2, "gpu_ms": 4.1}


def test_record_call_captures_env_from_bridge_status(tmp_path):
    s = _s(tmp_path)
    record_call(s, "bridge_status", {},
                {"ok": True, "plugin_version": "0.0.1", "rc_reachable": True,
                 "remote_exec_reachable": True, "ue_running": True}, 5)
    assert s.env["plugin_version"] == "0.0.1"
    assert s.env["bridge"]["rc_reachable"] is True


def test_record_call_captures_warn_error_logs(tmp_path):
    s = _s(tmp_path)
    body = {"ok": True, "lines": [
        {"verbosity": "Log", "category": "A", "line": "noise"},
        {"verbosity": "Warning", "category": "B", "line": "warn!"},
        {"verbosity": "Error", "category": "C", "line": "err!"},
    ]}
    record_call(s, "log_tail", {}, body, 4)
    kept = [l["verbosity"] for l in s.logs]
    assert kept == ["Warning", "Error"]
