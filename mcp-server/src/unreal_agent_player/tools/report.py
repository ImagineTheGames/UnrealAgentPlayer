from __future__ import annotations

import webbrowser
from typing import Any, Optional

from unreal_agent_player.errors import ErrorCode, error_response, ok_response
from unreal_agent_player.reporting import session as sess
from unreal_agent_player.reporting.render import render


def _no_session() -> dict[str, Any]:
    return error_response(ErrorCode.REPORT_NO_SESSION,
                          "No active report. Call report_start first.")


async def report_start(*, rc: Any = None, py_exec: Any = None,
                       task: str, project: Optional[str] = None,
                       requires_screenshot: bool = True) -> dict[str, Any]:
    # Screenshot proof is required for a passing report by default; pass
    # requires_screenshot=False only for a genuinely headless/no-visual check.
    s = sess.start_session(task=task, project=project, requires_screenshot=requires_screenshot)
    return ok_response({"run_dir": str(s.run_dir), "requires_screenshot": s.requires_screenshot})


async def report_assert(*, rc: Any = None, py_exec: Any = None,
                        label: str, passed: bool, evidence: str = "") -> dict[str, Any]:
    s = sess.active()
    if s is None:
        return _no_session()
    s.add_assertion(label, passed, evidence)
    return ok_response()


async def report_note(*, rc: Any = None, py_exec: Any = None,
                      text: str, section: Optional[str] = None) -> dict[str, Any]:
    s = sess.active()
    if s is None:
        return _no_session()
    s.add_note(text, section)
    return ok_response()


async def report_caption(*, rc: Any = None, py_exec: Any = None,
                         caption: str, screenshot: Optional[Any] = None) -> dict[str, Any]:
    s = sess.active()
    if s is None:
        return _no_session()
    found = s.set_caption(screenshot, caption)
    return ok_response({"applied": found})


async def report_finish(*, rc: Any = None, py_exec: Any = None,
                        verdict: str, summary: str) -> dict[str, Any]:
    s = sess.active()
    if s is None:
        return _no_session()
    if verdict not in ("pass", "fail"):
        return error_response(ErrorCode.SCHEMA_VALIDATION,
                              f"verdict must be 'pass' or 'fail', got {verdict!r}",
                              recoverable=False)
    s.finish(verdict, summary)
    html_path = s.run_dir / "index.html"
    try:
        html_path.write_text(render(s.to_dict()), encoding="utf-8")
        opened = bool(webbrowser.open(html_path.as_uri()))
    except Exception as exc:
        sess.clear_active()
        return ok_response({"run_dir": str(s.run_dir), "html": None,
                            "warning": f"render/open failed: {exc}"})
    sess.clear_active()
    return ok_response({"run_dir": str(s.run_dir), "html": str(html_path), "opened": opened})
