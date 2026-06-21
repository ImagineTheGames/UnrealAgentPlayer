from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
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
    s = sess.start_session(task=args.task, project=args.project,
                           requires_screenshot=args.require_screenshot)
    _emit({"ok": True, "run_dir": str(s.run_dir),
           "requires_screenshot": s.requires_screenshot})
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


def _report_screenshot(args) -> int:
    """Attach an EXISTING image file to the active report (vs the top-level `screenshot`
    verb, which captures from the editor via RC and attaches). Useful when the image was
    produced another way (e.g. `uap exec` HighResShot from an editor RC can't reach)."""
    s = _require_active()
    if s is None:
        return 2
    rel = s.add_screenshot(args.file, args.caption)
    _emit({"ok": rel is not None, "attached": rel, "file": args.file})
    return 0 if rel is not None else 1


def _parse_perf(unit_text: str, fps_text: str) -> dict:
    """Parse the plugin's GetStatGroupText output into a perf dict for the report.
    unit_text is like 'Frame: 11.20 ms\\nGame: 5.10 ms\\nDraw: 3.40 ms\\nGPU: 8.90 ms';
    fps_text is like 'FPS: 60.0'."""
    perf: dict = {}
    for line in (unit_text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            try:
                perf[k.strip().lower() + "_ms"] = round(float(v.replace("ms", "").strip()), 2)
            except ValueError:
                pass
    if fps_text and ":" in fps_text:
        try:
            perf["fps"] = round(float(fps_text.split(":", 1)[1].strip()), 1)
        except ValueError:
            pass
    return perf


def _report_diag(args) -> int:
    """Capture editor diagnostics (env + perf/frame timing) into the active report. Sourced
    via `exec` (targets the editor by project name) so it is accurate even when another editor
    squats the RC port -- unlike `status`, which only ever reaches whatever holds :30010. Call
    it while PIE is live to record the game's frame rate, not the idle editor's."""
    s = _require_active()
    if s is None:
        return 2
    code = (
        "import unreal, json\n"
        "ss = unreal.get_editor_subsystem(unreal.UAPAgentSubsystem)\n"
        "ws = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
        "w = ws.get_game_world() or ws.get_editor_world()\n"
        "print('UAPDIAG:' + json.dumps({"
        "'plugin_version': ss.get_plugin_version(),"
        "'world': (w.get_name() if w else None),"
        "'is_in_pie': ss.is_in_pie(),"
        "'unit': ss.get_stat_group_text('unit'),"
        "'fps': ss.get_stat_group_text('fps')}))\n"
    )
    body: dict = {"ok": True}
    try:
        client = PythonRemoteExecClient(node_project_substr=args.project)
        res = client.exec_python(code)
        diag = None
        for o in (res.get("output") or []):
            line = o.get("output", "")
            if "UAPDIAG:" in line:
                diag = json.loads(line.split("UAPDIAG:", 1)[1].strip())
        if diag is None:
            body = {"ok": False, "error": "no diagnostics returned from editor"}
        else:
            env = {
                "plugin_version": diag.get("plugin_version"),
                "world": diag.get("world"),
                "is_in_pie": diag.get("is_in_pie"),
                "project": args.project,
                "bridge": {"remote_exec_reachable": True},
            }
            perf = _parse_perf(diag.get("unit", ""), diag.get("fps", ""))
            s.set_env(env)
            if perf:
                s.set_perf(perf)
            body["env"] = env
            body["perf"] = perf
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("report:diag", {"project": args.project}, body, 0)
    _emit(body)
    return 0 if body["ok"] else 1


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
    if not os.environ.get("UAP_NO_BROWSER"):
        try:
            webbrowser.open(html_path.as_uri())
        except Exception:
            pass
    out = {"ok": True, "html": str(html_path), "verdict": s.status,
           "downgraded": s.status != args.verdict}
    if not s.env:
        out["warning"] = ("no diagnostics in report (env empty) -- run `uap report diag` "
                          "after `report start` to capture editor version/level/PIE state")
    _emit(out)
    return 0


def _rcport_cache_dir() -> pathlib.Path:
    root = os.environ.get("UAP_REPORTS_DIR")
    base = pathlib.Path(root) if root else (pathlib.Path.home() / ".uap-reports")
    return base / ".rcports"


def _port_cache_file(project: str) -> pathlib.Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", project).strip("-").lower() or "default"
    return _rcport_cache_dir() / f"{slug}.txt"


def _read_port_cache(project: str) -> int:
    try:
        return int(_port_cache_file(project).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_port_cache(project: str, port: int) -> None:
    try:
        f = _port_cache_file(project)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def _exec_rc_port(project: str) -> int:
    """Ask the editor matching `project` (over Python remote-exec, which is addressed
    per-editor) for the RC HTTP port it actually bound. 0 if unreachable / no match."""
    code = ("import unreal\n"
            "print('UAPRCPORT:' + str("
            "unreal.get_editor_subsystem(unreal.UAPAgentSubsystem).get_remote_control_port()))\n")
    try:
        res = PythonRemoteExecClient(node_project_substr=project).exec_python(code)
    except AgentError:
        return 0
    for o in (res.get("output") or []):
        line = o.get("output", "")
        if "UAPRCPORT:" in line:
            try:
                return int(line.split("UAPRCPORT:", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def _exec_project_name(project: "str | None") -> "str | None":
    """The project name of the editor matching `project` (via exec). Used to stamp a
    screenshot's provenance so a pass can't be proven with a shot of another editor."""
    code = ("import unreal\n"
            "print('UAPPROJ:' + unreal.Paths.get_project_file_path()"
            ".rsplit('/',1)[-1].rsplit('.',1)[0])\n")
    try:
        res = PythonRemoteExecClient(node_project_substr=(project or "")).exec_python(code)
    except AgentError:
        return None
    for o in (res.get("output") or []):
        line = o.get("output", "")
        if "UAPPROJ:" in line:
            return line.split("UAPPROJ:", 1)[1].strip()
    return None


def _rc_port_for(project: "str | None") -> int:
    """Resolve the RC HTTP port. UAP_RC_PORT env overrides everything; else resolve the
    editor's advertised port by project (cached), so two editors are each addressed on their
    own port. Falls back to 30010."""
    env = os.environ.get("UAP_RC_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    # Resolve the editor's advertised port (by project, or first responder if project is empty).
    # Editors no longer use the default 30010 -- each binds a per-project port -- so we must ask.
    key = project or "_default"
    cached = _read_port_cache(key)
    if cached:
        return cached
    resolved = _exec_rc_port(project or "")
    if resolved:
        _write_port_cache(key, resolved)
        return resolved
    return 30010


def _rc_call(func: str, params: dict, project: "str | None" = None):
    port = _rc_port_for(project)

    def _call(p: int):
        async def _go():
            rc = RemoteControlClient(port=p)
            try:
                return await rc.call_preset(func, params)
            finally:
                await rc.aclose()
        return asyncio.run(_go())

    try:
        return _call(port)
    except AgentError:
        # The cached/default port may be stale (editor restarted on a different port).
        # Re-resolve once via exec and retry -- unless an explicit env override pins the port.
        if project and not os.environ.get("UAP_RC_PORT"):
            fresh = _exec_rc_port(project)
            if fresh and fresh != port:
                _write_port_cache(project, fresh)
                return _call(fresh)
        raise


def _capture(tool: str, args: dict, body: dict, ms: int) -> None:
    s = _load_active()
    if s is None:
        return
    try:
        ok = bool(body.get("ok", True)) and "error" not in body
        s.add_tool_call(tool, args, ok=ok, ms=ms, error=body.get("error"))
        if tool == "screenshot" and body.get("path") and body.get("exists"):
            s.add_screenshot(body["path"], body.get("caption", ""),
                             provenance=body.get("provenance"))
    except Exception:
        pass


def _status(args) -> int:
    t0 = time.monotonic()
    out = {"ok": True, "rc_reachable": False, "plugin_version": None, "rc_port": _rc_port_for(args.project)}
    try:
        ver = _rc_call("GetPluginVersion", {}, args.project)
        out["rc_reachable"] = True
        out["plugin_version"] = ver
    except AgentError as exc:
        out["ok"] = False
        out["error"] = str(exc)
    _capture("status", {}, out, int((time.monotonic() - t0) * 1000))
    _emit(out)
    return 0 if out["rc_reachable"] else 1


def _coerce(v: str):
    """Coerce a key=value string value to bool/int/float, else leave as string."""
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _parse_rc_params(tokens: list[str]) -> dict:
    """Parse rc params from CLI tokens.

    Two forms (the first dodges Windows shell quoting, which mangles embedded
    double-quotes in a JSON string arg):
      - key=value pairs:  Command=stat\\ fps   KeyName=E   bPressed=true
      - a single JSON object token:  '{"Command":"stat fps"}'  (when the caller
        can pass quotes through intact).
    """
    if not tokens:
        return {}
    if len(tokens) == 1 and tokens[0].lstrip().startswith("{"):
        return json.loads(tokens[0])
    out: dict = {}
    for tok in tokens:
        if "=" not in tok:
            raise ValueError(f"param must be key=value or a single JSON object, got: {tok!r}")
        k, v = tok.split("=", 1)
        out[k] = _coerce(v)
    return out


def _rc(args) -> int:
    try:
        params = _parse_rc_params(args.params)
    except (ValueError, json.JSONDecodeError) as exc:
        _emit({"ok": False, "error": f"bad rc params: {exc}"})
        return 2
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        body["result"] = _rc_call(args.rc_func, params, args.project)
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


def _pie(args) -> int:
    """Start/stop PIE via the plugin's version-correct RC verbs (StartPIE/StopPIE/IsInPIE),
    so agents never touch the raw, version-fragile engine subsystem."""
    sub = args.pie_cmd
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        if sub == "start":
            body["result"] = _rc_call("StartPIE", {}, args.project)
        elif sub == "stop":
            body["result"] = _rc_call("StopPIE", {}, args.project)
        elif sub == "wait":
            deadline = time.monotonic() + args.seconds
            playing = bool(_rc_call("IsInPIE", {}, args.project))
            while not playing and time.monotonic() < deadline:
                time.sleep(0.5)
                playing = bool(_rc_call("IsInPIE", {}, args.project))
            body["playing"] = playing
            body["ok"] = playing
            if not playing:
                body["error"] = f"PIE not running after {args.seconds}s"
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture(f"pie:{sub}", {}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _find_clickable(elements: list, label: str) -> "dict | None":
    """Choose the on-screen element to click for a label: exact (case-insensitive) match
    first, then a substring match. Returns the element dict (with x/y) or None."""
    low = label.strip().lower()
    exact = [e for e in elements if str(e.get("text", "")).strip().lower() == low]
    if exact:
        return exact[0]
    subs = [e for e in elements if low in str(e.get("text", "")).lower()]
    return subs[0] if subs else None


def _click(args) -> int:
    """Click an on-screen UMG element by its visible text -- read-ui to find it, then inject a
    real mouse move+down+up at its position. The easy path that used to require composing
    read-ui + InjectMouse* by hand. (Clicks the element's reported position; a future read-ui
    center coord will improve precision for large widgets -- see docs/agent-discoverability.md.)"""
    t0 = time.monotonic()
    body: dict = {"ok": True, "label": args.label}
    try:
        ui_raw = _rc_call("DumpViewportUI", {}, args.project)
        ui = json.loads(ui_raw) if isinstance(ui_raw, str) else (ui_raw or {})
        elements = ui.get("texts") or []
        match = _find_clickable(elements, args.label)
        if match is None:
            body = {"ok": False, "error": f"no on-screen element matching {args.label!r}",
                    "seen": [e.get("text") for e in elements]}
        else:
            x, y = float(match["x"]), float(match["y"])
            body.update({"matched": match.get("text"), "x": x, "y": y})
            _rc_call("InjectMouseMove", {"X": x, "Y": y, "bAbsolute": True}, args.project)
            _rc_call("InjectMouseButton", {"Button": "Left", "bPressed": True}, args.project)
            _rc_call("InjectMouseButton", {"Button": "Left", "bPressed": False}, args.project)
    except (AgentError, ValueError, KeyError, json.JSONDecodeError) as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("click", {"label": args.label}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _tab(args) -> int:
    """Select a CommonUI tab by its TabNameID -- menus are tab-driven, this is the #1
    navigation primitive."""
    t0 = time.monotonic()
    body: dict = {"ok": True, "tab": args.tab_id}
    try:
        ok = bool(_rc_call("SelectTab", {"TabId": args.tab_id}, args.project))
        body["ok"] = ok
        if not ok:
            body["error"] = (f"no tab '{args.tab_id}' on a live CommonUI tab list "
                             "(is PIE running and the menu open?)")
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("tab", {"tab": args.tab_id}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _nav(args) -> int:
    """Move UI focus / activate through Slate (up|down|left|right|accept|back) -- the path
    menus actually use, distinct from game input."""
    t0 = time.monotonic()
    body: dict = {"ok": True, "direction": args.direction}
    try:
        body["handled"] = bool(_rc_call("NavigateUI", {"Direction": args.direction}, args.project))
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("nav", {"direction": args.direction}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _read_ui(args) -> int:
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        body["ui"] = _rc_call("DumpViewportUI", {}, args.project)
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("read-ui", {}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _screenshot_body(file: str, exists: bool) -> dict:
    """Build the screenshot result. A missing file after the poll window is a hard FAIL
    with a concrete reason -- not a silent ok:true/exists:false (which reads like a
    transient and let false positives through)."""
    if exists:
        return {"ok": True, "exists": True, "path": file}
    return {
        "ok": False, "exists": False, "path": file,
        "error": ("screenshot not written: CaptureViewportWithUI renders on the next game "
                  "frame, but an idle editor viewport never renders one. Requires active PIE "
                  "(uap pie start) / a renderable frame."),
    }


def _screenshot(args) -> int:
    t0 = time.monotonic()
    try:
        _rc_call("CaptureViewportWithUI", {"Filename": args.file}, args.project)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not os.path.exists(args.file):
            time.sleep(0.25)
        body = _screenshot_body(args.file, os.path.exists(args.file))
        body["caption"] = args.caption
        if body.get("exists"):
            # Stamp which editor this shot came from, so report finish can reject a pass whose
            # proof is a screenshot of a DIFFERENT editor.
            body["provenance"] = _exec_project_name(args.project)
    except AgentError as exc:
        body = {"ok": False, "error": str(exc)}
    _capture("screenshot", {"file": args.file}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


_HELP_CATALOG = r"""uap -- drive the Unreal editor for agent testing. Commands target the
editor named by --project (default $UAP_PROJECT, pinned by this project's uap.ps1 launcher).

COMMON MISTAKES (don't)
  * Screenshots: use `uap screenshot <abs.png>` (composites 3D + UMG + CommonUI + Slate).
    HighResShot and most MCP screenshot tools capture the 3D SCENE ONLY (no UI). Missing UI
    in a shot = wrong tool, not "UI can't be captured."
  * Targeting: run via this project's uap.ps1 (it pins UAP_PROJECT). Calling the venv python
    directly with no --project cross-targets another open editor (e.g. PIE in the wrong project).
  * read-ui x/y are screen pixels for screen-space UMG/CommonUI; for a WORLD-SPACE VR menu
    (WidgetComponent) they're render-target coords -> a screen click misses (needs the laser).
  * Not done until `uap report finish` emits the HTML report. Read concrete state, not pixels.
  * A PASS requires a screenshot FROM THE EDITOR UNDER TEST -- `uap screenshot <abs.png>` via this
    project's uap.ps1 (it stamps the source editor). A shot of ANOTHER editor, or a manual attach
    of unknown origin, auto-FAILS the pass. And pixels aren't proof unless you read what they show
    (`uap read-ui`/state) and assert on it. Opt out (headless only): report start --no-require-screenshot.

PREFLIGHT
  uap status                      liveness + plugin version + resolved RC port
  uap report diag                 capture editor/level/PIE + frame perf into the report

REPORT (a verification is NOT done until `report finish` emits the HTML report; cite its path)
  uap report start "<question>" [--no-require-screenshot]   # screenshot REQUIRED for pass by default
  uap report assert "<label>" pass|fail "<evidence>"
  uap report note "<text>"
  uap report finish pass|fail "<summary>"   # pass with NO screenshot auto-downgrades to FAIL
  -> attach proof: `uap screenshot <abs.png>` (auto-attaches), or `uap report screenshot <file>`

PLAY-IN-EDITOR
  uap pie start                   start PIE (version-correct; do NOT use PlayWorldEditorSubsystem)
  uap pie wait <seconds>          block until the game world is live
  uap pie stop

DRIVE + OBSERVE
  uap rc <Func> [key=value ...]   call a plugin UFUNCTION (input injection lives here)
  uap exec "<python>"             run `import unreal; ...` in the editor (escape hatch)
  uap read-ui                     dump on-screen UMG: [{text, x, y}, ...] + focused
  uap click "<label>"             click an on-screen UMG element by its visible text
  uap tab "<TabId>"               select a CommonUI tab by id (menus are tab-driven)
  uap nav up|down|left|right|accept|back   move UI focus / activate (Slate nav path)
  uap screenshot <file>           capture composited game+UMG frame (needs live PIE)

RECIPES
  Click an on-screen button by label (one call):
    uap click "VR TRAINING"
  ...or the underlying chain (what `uap click` does), e.g. to click a precise spot:
    uap read-ui                                   # find the element's x,y
    uap rc InjectMouseMove X=<x> Y=<y> bAbsolute=true
    uap rc InjectMouseButton Button=Left bPressed=true
    uap rc InjectMouseButton Button=Left bPressed=false
  Press a key (routes through the real input path):
    uap rc InjectKey KeyName=E bPressed=true ; uap rc InjectKey KeyName=E bPressed=false
  VR controller button:
    uap rc InjectXRButton Hand=Right ButtonKeyName=OculusTouch_Right_Trigger_Click bPressed=true
  Select a CommonUI tab / move focus:
    uap tab "VRTraining"            # select tab by id
    uap nav down ; uap nav accept   # focus nav + activate
  Read game-truth (preferred over screenshots): uap rc CallTestHelper Name=... JsonArgs={}
    list helpers: uap rc ListTestHelpers

MORE: docs/agent-testing.md (usage), docs/capabilities.md (every tool), docs/known-issues.md.
Per-verb flags: uap <verb> --help
"""


def _help(args) -> int:
    print(_HELP_CATALOG)
    return 0


def _env_project() -> str:
    """Default editor target. Comes from $UAP_PROJECT -- which the per-project uap.ps1 launcher
    pins -- so a command run from a project's launcher targets THAT editor. Empty (no launcher,
    no env) means 'first editor that answers', NOT a hardcoded project: a hardcoded default made
    commands cross-target the wrong editor (e.g. starting PIE in the wrong project)."""
    return os.environ.get("UAP_PROJECT", "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uap")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Shared --project for RC verbs: the RC HTTP port is resolved per editor (by project), so
    # two editors are each addressed on their own port instead of both hitting 30010.
    proj = argparse.ArgumentParser(add_help=False)
    proj.add_argument("--project", default=_env_project(),
                      help="editor to target (default $UAP_PROJECT); RC port resolved per project")

    sub.add_parser("help", help="catalog of verbs + copy-paste recipes").set_defaults(func=_help)
    sub.add_parser("tools", help="alias of help").set_defaults(func=_help)

    rep = sub.add_parser("report").add_subparsers(dest="rcmd", required=True)
    rs = rep.add_parser("start")
    rs.add_argument("task")
    rs.add_argument("--project", default=_env_project())
    # Screenshot proof is REQUIRED by default: `finish pass` auto-downgrades to fail unless a
    # screenshot is attached. --require-screenshot is the (now redundant) explicit-on;
    # --no-require-screenshot opts out for a genuinely headless/no-visual check.
    rs.add_argument("--require-screenshot", dest="require_screenshot", action="store_true",
                    default=True, help="(default) require a screenshot for a passing report")
    rs.add_argument("--no-require-screenshot", dest="require_screenshot", action="store_false",
                    help="rare: allow a pass with no screenshot (justify in the summary)")
    rs.set_defaults(func=_report_start)
    ra = rep.add_parser("assert")
    ra.add_argument("label")
    ra.add_argument("verdict", choices=["pass", "fail"])
    ra.add_argument("evidence", nargs="?", default="")
    ra.set_defaults(func=_report_assert)
    rn = rep.add_parser("note")
    rn.add_argument("text")
    rn.set_defaults(func=_report_note)
    rd = rep.add_parser("diag")
    rd.add_argument("--project", default=_env_project(),
                    help="editor project to source diagnostics from (default $UAP_PROJECT, via exec)")
    rd.set_defaults(func=_report_diag)
    rsh = rep.add_parser("screenshot")
    rsh.add_argument("file")
    rsh.add_argument("--caption", default="")
    rsh.set_defaults(func=_report_screenshot)
    rf = rep.add_parser("finish")
    rf.add_argument("verdict", choices=["pass", "fail"])
    rf.add_argument("summary")
    rf.set_defaults(func=_report_finish)

    st = sub.add_parser("status", parents=[proj])
    st.set_defaults(func=_status)
    rcp = sub.add_parser("rc", parents=[proj])
    rcp.add_argument("rc_func")
    rcp.add_argument("params", nargs="*",
                     help="key=value pairs (e.g. Command=stat fps KeyName=E bPressed=true) "
                          "or a single JSON object")
    rcp.set_defaults(func=_rc)
    ex = sub.add_parser("exec")
    ex.add_argument("code")
    ex.add_argument("--project", default=_env_project())
    ex.set_defaults(func=_exec)
    exf = sub.add_parser("exec-file")
    exf.add_argument("path")
    exf.add_argument("--project", default=_env_project())
    exf.set_defaults(func=_exec_file)
    pie = sub.add_parser("pie").add_subparsers(dest="pie_cmd", required=True)
    pie.add_parser("start", parents=[proj]).set_defaults(func=_pie)
    pie.add_parser("stop", parents=[proj]).set_defaults(func=_pie)
    pw = pie.add_parser("wait", parents=[proj])
    pw.add_argument("seconds", type=float, help="max seconds to wait for PIE to be live")
    pw.set_defaults(func=_pie)

    ru = sub.add_parser("read-ui", parents=[proj])
    ru.set_defaults(func=_read_ui)
    cl = sub.add_parser("click", parents=[proj], help="click an on-screen UMG element by its text")
    cl.add_argument("label", help="visible text of the element to click")
    cl.set_defaults(func=_click)
    tb = sub.add_parser("tab", parents=[proj], help="select a CommonUI tab by its id")
    tb.add_argument("tab_id")
    tb.set_defaults(func=_tab)
    nv = sub.add_parser("nav", parents=[proj], help="UI focus nav (up|down|left|right|accept|back)")
    nv.add_argument("direction", choices=["up", "down", "left", "right", "accept", "back"])
    nv.set_defaults(func=_nav)
    sc = sub.add_parser("screenshot", parents=[proj])
    sc.add_argument("file")
    sc.add_argument("--caption", default="")
    sc.set_defaults(func=_screenshot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
