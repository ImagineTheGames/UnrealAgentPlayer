from __future__ import annotations

import webbrowser
from typing import Any

from unreal_agent_player.errors import ErrorCode, error_response, ok_response
from unreal_agent_player.reporting import session as sess
from unreal_agent_player.reporting.render import render


def _no_session() -> dict[str, Any]:
    return error_response(ErrorCode.REPORT_NO_SESSION,
                          "No active report. Call report_start first.")


async def report_start(*, rc: Any = None, py_exec: Any = None,
                       task: str, project: str | None = None,
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
                      text: str, section: str | None = None) -> dict[str, Any]:
    s = sess.active()
    if s is None:
        return _no_session()
    s.add_note(text, section)
    return ok_response()


async def report_caption(*, rc: Any = None, py_exec: Any = None,
                         caption: str, screenshot: Any | None = None) -> dict[str, Any]:
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

    # Clean up the editor: a finished test must not leave PIE running forever. Stop PIE if it
    # is still live (best-effort; a failure here must never block rendering the report).
    # `pie_stopped` is a claim the next agent acts on, so it means the teardown was OBSERVED --
    # this used to set it from the stop request alone, which could report a stop that never
    # happened (known-issues #25).
    pie_stopped = False
    pie_stop_error = None
    if rc is not None and py_exec is not None:
        try:
            from unreal_agent_player.tools import pie as _pie
            st = await _pie.pie_status(rc=rc, py_exec=py_exec)
            if st.get("phase") not in (None, "NotPlaying"):
                res = await _pie.pie_stop(rc=rc, py_exec=py_exec)
                pie_stopped = bool(res.get("stopped"))
                pie_stop_error = None if pie_stopped else res.get("error")
                s.add_note("PIE auto-stopped on report finish (teardown confirmed)." if pie_stopped
                           else f"PIE stop NOT confirmed on report finish: {pie_stop_error}")
        except Exception:
            pass  # editor gone / RC unreachable -- still render the report

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
    out = {"run_dir": str(s.run_dir), "html": str(html_path), "opened": opened,
           "pie_stopped": pie_stopped}
    if pie_stop_error:
        out["pie_stop_error"] = pie_stop_error
    return ok_response(out)
