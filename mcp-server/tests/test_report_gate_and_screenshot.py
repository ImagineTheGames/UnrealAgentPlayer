from unreal_agent_player.cli import _pie, _report_diag, _screenshot_body, build_parser
from unreal_agent_player.reporting.session import ReportSession


def test_report_diag_routes_with_project():
    args = build_parser().parse_args(["report", "diag", "--project", "Foo"])
    assert args.func is _report_diag
    assert args.project == "Foo"


def test_parse_perf_extracts_frame_timing_and_fps():
    from unreal_agent_player.cli import _parse_perf
    p = _parse_perf("Frame: 11.20 ms\nGame: 5.10 ms\nDraw: 3.40 ms\nGPU: 8.90 ms", "FPS: 60.0")
    assert p["frame_ms"] == 11.2
    assert p["game_ms"] == 5.1
    assert p["draw_ms"] == 3.4
    assert p["gpu_ms"] == 8.9
    assert p["fps"] == 60.0


def test_parse_perf_handles_empty():
    from unreal_agent_player.cli import _parse_perf
    assert _parse_perf("", "") == {}


# --- #2: `uap pie` verbs route correctly (no editor needed for arg wiring) ---

def test_pie_wait_parses_seconds_and_routes_to_pie():
    args = build_parser().parse_args(["pie", "wait", "5"])
    assert args.func is _pie
    assert args.pie_cmd == "wait"
    assert args.seconds == 5.0


def test_pie_start_and_stop_route_to_pie():
    for verb in ("start", "stop"):
        args = build_parser().parse_args(["pie", verb])
        assert args.func is _pie
        assert args.pie_cmd == verb


# --- #3: report finish must not pass without required visual proof ---

def test_gate_downgrades_pass_when_required_and_no_screenshot(tmp_path):
    s = ReportSession(task="t", run_dir=tmp_path, quote="q", requires_screenshot=True)
    s.finish("pass", "done")
    assert s.status == "fail"


def test_gate_keeps_pass_when_required_and_screenshot_present(tmp_path):
    s = ReportSession(task="t", run_dir=tmp_path, quote="q", requires_screenshot=True)
    (tmp_path / "x.png").write_bytes(b"\x89PNG\r\n")
    s.add_screenshot(str(tmp_path / "x.png"), "cap")
    s.finish("pass", "done")
    assert s.status == "pass"


def test_gate_no_downgrade_when_not_required(tmp_path):
    s = ReportSession(task="t", run_dir=tmp_path, quote="q", requires_screenshot=False)
    s.finish("pass", "done")
    assert s.status == "pass"


def test_gate_does_not_touch_fail_verdict(tmp_path):
    s = ReportSession(task="t", run_dir=tmp_path, quote="q", requires_screenshot=True)
    s.finish("fail", "broken")
    assert s.status == "fail"


def test_requires_screenshot_roundtrips_through_load(tmp_path):
    ReportSession(task="t", run_dir=tmp_path, quote="q", requires_screenshot=True)
    loaded = ReportSession.load(tmp_path)
    assert loaded.requires_screenshot is True


# --- #4: screenshot reports failure (not a silent ok) when no file lands ---

def test_screenshot_body_ok_when_file_exists():
    b = _screenshot_body("shot.png", True)
    assert b["ok"] is True
    assert b["exists"] is True
    assert b["path"] == "shot.png"


def test_screenshot_body_fails_with_reason_when_missing():
    b = _screenshot_body("shot.png", False)
    assert b["ok"] is False
    assert b["exists"] is False
    assert "PIE" in b["error"]
