from unreal_agent_player.reporting.render import render


def _data(**over):
    d = {
        "task": "Tier-1 proof", "project": "SchoolsOut", "status": "pass",
        "started": "2026-06-08T18:42:01", "finished": "2026-06-08T18:43:14",
        "duration_s": 73.0, "quote": "You shipped proof, not promises.",
        "summary": "pawn moved 710u", "env": {"plugin_version": "0.0.1"},
        "perf": {"frame_ms": 11.2, "gpu_ms": 4.1}, "notes": [],
        "assertions": [{"label": "pawn moved", "passed": True, "evidence": "9400->9874"}],
        "timeline": [{"t": "18:42:05", "tool": "pie_start", "args": {}, "ok": True, "ms": 1200, "error": None}],
        "screenshots": [{"file": "screenshots/000.png", "caption": "hub", "t": "18:42:40", "missing": False}],
        "logs": [{"verbosity": "Warning", "category": "X", "line": "warn"}],
    }
    d.update(over)
    return d


def test_render_returns_full_html_document():
    html = render(_data())
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_render_shows_verdict_task_quote():
    html = render(_data())
    assert "PASS" in html
    assert "Tier-1 proof" in html
    assert "You shipped proof, not promises." in html


def test_render_has_four_tabs():
    html = render(_data())
    for tab in ("Overview", "Screenshots", "Timeline", "Diagnostics"):
        assert f'data-tab="{tab}"' in html


def test_render_includes_assertions_and_screenshot_img():
    html = render(_data())
    assert "pawn moved" in html and "9400->9874" in html
    assert '<img' in html and "screenshots/000.png" in html


def test_render_escapes_html_in_text():
    html = render(_data(summary="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_fail_status_class():
    html = render(_data(status="fail"))
    assert "FAIL" in html
    assert "status-fail" in html


def test_render_deterministic():
    d = _data()
    assert render(d) == render(d)
