from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import webbrowser

from unreal_agent_player.reporting import session as sess
from unreal_agent_player.reporting.render import render
from unreal_agent_player.transport import RemoteControlClient, PythonRemoteExecClient
from unreal_agent_player.errors import AgentError


def _load_active() -> "sess.ReportSession | None":
    run = sess.get_active_run()
    if run is None or not (run / "data.json").exists():
        return None
    return sess.ReportSession.load(run)


def _require_active():
    """Return the active session, or None after emitting the no-session error."""
    s = _load_active()
    if s is None:
        _emit({"ok": False, "error": "no active report; run `uap report start` first"})
    return s


def _emit(obj: dict) -> None:
    print(json.dumps(obj))


# --- report verbs ---

def _report_start(args) -> int:
    s = sess.start_session(task=args.task, project=args.project)
    _emit({"ok": True, "run_dir": str(s.run_dir)})
    return 0


def _report_assert(args) -> int:
    s = _require_active()
    if s is None:
        return 2
    s.add_assertion(args.label, args.verdict == "pass", args.evidence)
    _emit({"ok": True})
    return 0


def _report_note(args) -> int:
    s = _require_active()
    if s is None:
        return 2
    s.add_note(args.text)
    _emit({"ok": True})
    return 0


def _report_finish(args) -> int:
    s = _require_active()
    if s is None:
        return 2
    s.finish(args.verdict, args.summary)
    html_path = s.run_dir / "index.html"
    try:
        html_path.write_text(render(s.to_dict()), encoding="utf-8")
    except Exception as exc:
        sess.clear_active_run()
        _emit({"ok": False, "error": f"render failed: {exc}"})
        return 1
    sess.clear_active_run()
    try:
        webbrowser.open(html_path.as_uri())
    except Exception:
        pass
    _emit({"ok": True, "html": str(html_path)})
    return 0


def _rc_call(func: str, params: dict):
    async def _go():
        rc = RemoteControlClient()
        try:
            return await rc.call_preset(func, params)
        finally:
            await rc.aclose()
    return asyncio.run(_go())


def _capture(tool: str, args: dict, body: dict, ms: int) -> None:
    s = _load_active()
    if s is None:
        return
    try:
        ok = bool(body.get("ok", True)) and "error" not in body
        s.add_tool_call(tool, args, ok=ok, ms=ms, error=body.get("error"))
        if tool == "screenshot" and body.get("path"):
            s.add_screenshot(body["path"], body.get("caption", ""))
    except Exception:
        pass


def _status(args) -> int:
    t0 = time.monotonic()
    out = {"ok": True, "rc_reachable": False, "plugin_version": None}
    try:
        ver = _rc_call("GetPluginVersion", {})
        out["rc_reachable"] = True
        out["plugin_version"] = ver
    except AgentError as exc:
        out["ok"] = False
        out["error"] = str(exc)
    _capture("status", {}, out, int((time.monotonic() - t0) * 1000))
    _emit(out)
    return 0 if out["rc_reachable"] else 1


def _rc(args) -> int:
    params = json.loads(args.params) if args.params else {}
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        body["result"] = _rc_call(args.rc_func, params)
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture(f"rc:{args.rc_func}", {"params": params}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _exec(args) -> int:
    code = args.code
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        client = PythonRemoteExecClient(node_project_substr=args.project)
        res = client.exec_python(code)
        body["result"] = res.get("result")
        body["output"] = [o.get("output", "") for o in (res.get("output") or [])]
        body["ok"] = bool(res.get("success", True))
        if not body["ok"]:
            body["error"] = "exec returned success=false; see output"
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("exec", {"code": code[:200]}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _exec_file(args) -> int:
    with open(args.path, encoding="utf-8") as f:
        code = f.read()
    return _exec(argparse.Namespace(code=code, project=args.project))


def _read_ui(args) -> int:
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        body["ui"] = _rc_call("DumpViewportUI", {})
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("read-ui", {}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _screenshot(args) -> int:
    t0 = time.monotonic()
    body: dict = {"ok": True, "caption": args.caption}
    try:
        _rc_call("CaptureViewportWithUI", {"Filename": args.file})
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not os.path.exists(args.file):
            time.sleep(0.25)
        body["path"] = args.file
        body["exists"] = os.path.exists(args.file)
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("screenshot", {"file": args.file}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if (body["ok"] and body.get("exists", False)) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uap")
    sub = p.add_subparsers(dest="cmd", required=True)

    rep = sub.add_parser("report").add_subparsers(dest="rcmd", required=True)
    rs = rep.add_parser("start")
    rs.add_argument("task")
    rs.add_argument("--project", default="SchoolsOutVR")
    rs.set_defaults(func=_report_start)
    ra = rep.add_parser("assert")
    ra.add_argument("label")
    ra.add_argument("verdict", choices=["pass", "fail"])
    ra.add_argument("evidence", nargs="?", default="")
    ra.set_defaults(func=_report_assert)
    rn = rep.add_parser("note")
    rn.add_argument("text")
    rn.set_defaults(func=_report_note)
    rf = rep.add_parser("finish")
    rf.add_argument("verdict", choices=["pass", "fail"])
    rf.add_argument("summary")
    rf.set_defaults(func=_report_finish)

    st = sub.add_parser("status")
    st.set_defaults(func=_status)
    rcp = sub.add_parser("rc")
    rcp.add_argument("rc_func")
    rcp.add_argument("params", nargs="?", default="")
    rcp.set_defaults(func=_rc)
    ex = sub.add_parser("exec")
    ex.add_argument("code")
    ex.add_argument("--project", default="SchoolsOut")
    ex.set_defaults(func=_exec)
    exf = sub.add_parser("exec-file")
    exf.add_argument("path")
    exf.add_argument("--project", default="SchoolsOut")
    exf.set_defaults(func=_exec_file)
    ru = sub.add_parser("read-ui")
    ru.set_defaults(func=_read_ui)
    sc = sub.add_parser("screenshot")
    sc.add_argument("file")
    sc.add_argument("--caption", default="")
    sc.set_defaults(func=_screenshot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
