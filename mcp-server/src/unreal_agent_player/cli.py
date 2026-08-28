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
from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player import coordination as _coord


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


def _err(exc: Exception) -> dict:
    """Uniform failure body. Carries the machine-readable code (and whether a retry is
    worth it) so a caller can tell a transient transport blip from a dead editor instead
    of string-matching a message -- or, worse, getting a raw traceback."""
    body: dict = {"ok": False, "error": str(exc)}
    if isinstance(exc, AgentError):
        body["code"] = exc.code.value
        body["retryable"] = exc.recoverable
        if exc.retry_hint:
            body["retry_hint"] = exc.retry_hint
    return body


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
        body = _err(exc)
    _capture("report:diag", {"project": args.project}, body, 0)
    _emit(body)
    return 0 if body["ok"] else 1


def _report_finish(args) -> int:
    s = _require_active()
    if s is None:
        return 2

    # Clean up the editor: a finished test must not leave PIE running forever. Stop PIE if it is
    # still live (idempotent, best-effort -- a failure here must never block rendering the report).
    # Targets the report's own project so we stop the right editor. Opt out with --keep-pie for the
    # rare case you want to keep inspecting the running game after finish.
    #
    # Goes through the CONFIRMED stop: this used to be `IsInPIE -> StopPIE -> pie_stopped = True`,
    # which reported a stop it never verified. `pie_stopped` now means the teardown was observed,
    # and a stop that did not take says so loudly instead of leaving the next agent a live session.
    pie_stopped = False
    pie_stop_error = None
    if not getattr(args, "keep_pie", False):
        proj = getattr(s, "project", None) or None
        try:
            res = _pie_stop(proj, _pie_stop_timeout())
            pie_stopped = bool(res.get("stopped"))
            pie_stop_error = None if pie_stopped else res.get("error")
        except Exception:
            pass  # editor gone / RC unreachable -- nothing to stop, still render the report
        try:
            if pie_stopped:
                s.add_note("PIE auto-stopped on report finish (teardown confirmed).")
            elif pie_stop_error:
                s.add_note(f"PIE stop NOT confirmed on report finish: {pie_stop_error}")
        except Exception:
            pass

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
           "downgraded": s.status != args.verdict, "pie_stopped": pie_stopped}
    if pie_stop_error:
        out["pie_stop_error"] = pie_stop_error
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


# --- CLI / plugin version skew ----------------------------------------------------------
# Every project vendors its OWN copy of the uap plugin, but they all share THIS CLI (each
# project's uap.ps1 resolves this one repo). The CLI therefore updates the moment it is
# pulled, while a project's plugin copy only catches up when that project syncs and
# REBUILDS -- so CLI/plugin skew is permanent and expected, not a transient state.
#
# A verb the plugin does not export is not a preset field, and RemoteControl answers
# 404 "Unable to resolve the preset field", which reads like a broken editor rather than a
# tooling version gap (that cost a teammate an afternoon on `pie start`). Rule for anyone
# adding a plugin verb: never call it bare from here. Either route it through _rc_require,
# saying what the verb is FOR, or -- when an OLDER verb does the same job honestly -- fall
# back to that verb. Never fall back to a verb that answers a DIFFERENT question.


def _is_missing_verb(exc: Exception) -> bool:
    """True when RemoteControl could not RESOLVE the function -- i.e. this editor's plugin
    copy predates the verb. That is the ONLY case in which a fallback may fire.

    A verb that exists and fails answers HTTP 200 carrying its own result (a JSON envelope,
    or false), so a real failure never reaches here and can never be masked by a fallback.
    We key on the 404 STATUS rather than on RemoteControl's wording: the preset-call endpoint
    404s only when the preset or the field is unresolvable, never because the UFUNCTION
    itself refused.
    """
    return isinstance(exc, AgentError) and "returned 404" in str(exc)


def _skew_error(func: str, project: "str | None", needs: str) -> AgentError:
    """The refusal for "this editor's plugin is too old to serve that request". Same shape as
    the ListTestHelpersJson message below: name the missing verb, say what is lost, and give
    the one action that fixes it."""
    return AgentError(
        ErrorCode.UE_OBJECT_NOT_FOUND,
        f"this editor's plugin has no {func} ({needs}). The CLI is shared by every project "
        f"while each project vendors its own plugin copy, so this one is behind the CLI: "
        f"sync and rebuild {project or 'that project'} (Restart-Editor.ps1) to get the verb.",
        recoverable=False,
    )


def _rc_require(func: str, params: dict, project: "str | None", needs: str):
    """_rc_call for a verb an older plugin copy may not have: turns RemoteControl's raw 404
    into the version-skew refusal above. Every other failure passes through untouched."""
    try:
        return _rc_call(func, params, project)
    except AgentError as exc:
        if _is_missing_verb(exc):
            raise _skew_error(func, project, needs) from None
        raise


def _rc_json(func: str, params: dict, project: "str | None" = None,
             *, needs: "str | None" = None) -> dict:
    """Call a UFUNCTION that returns a JSON string and decode it to a dict.

    Plugin verbs that can fail for more than one reason return a JSON envelope so the refusal
    carries its own explanation; the CLI relays that verbatim rather than guessing. Pass
    `needs` for a verb older plugin copies lack, so a 404 is named as skew instead of leaking.
    """
    raw = _rc_require(func, params, project, needs) if needs else _rc_call(func, params, project)
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {"ok": False, "error": f"{func}: unexpected result {raw!r}"}


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


# UFUNCTIONs whose declared return type cannot survive RemoteControl's preset-call route:
# it serializes the return through a filter that only admits the function's own out/return
# params, which empties any nested struct. The plugin exposes a JSON-string twin; `uap rc`
# transparently uses it so the documented incantation keeps working and keeps its data.
_RC_JSON_TWINS = {"ListTestHelpers": "ListTestHelpersJson"}


def _rc(args) -> int:
    try:
        params = _parse_rc_params(args.params)
    except (ValueError, json.JSONDecodeError) as exc:
        _emit({"ok": False, "error": f"bad rc params: {exc}"})
        return 2
    t0 = time.monotonic()
    body: dict = {"ok": True}
    func = args.rc_func
    twin = _RC_JSON_TWINS.get(func) if not params else None
    try:
        if twin:
            body["result"] = {"helpers": _helpers_payload(args.project)}
            body["via"] = twin
        else:
            body["result"] = _rc_call(func, params, args.project)
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
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
        body = _err(exc)
    _capture("exec", {"code": code[:200]}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _exec_file(args) -> int:
    with open(args.path, encoding="utf-8") as f:
        code = f.read()
    return _exec(argparse.Namespace(code=code, project=args.project))


def _pie_start(mode: str, project: "str | None") -> dict:
    """Start PIE in `mode`, tolerating a plugin copy that predates StartPIEMode -- for FLAT
    only.

    --mode vr uses the editor's VR Preview, i.e. the HMD code path: OpenXR input and every
    IsHeadMountedDisplayEnabled() branch. Flat PIE takes neither, so an HMD-only bug is
    invisible there. StartPIEMode refuses "vr" with a concrete reason when no headset is
    connected rather than silently starting flat PIE.

    StartPIEMode is newer than StartPIE, so a project whose vendored plugin has not been
    rebuilt 404s on it. For flat that is pure skew -- the old StartPIE verb starts exactly the
    same session -- so fall back and say so. For vr there is nothing honest to fall back to:
    StartPIE can only start FLAT PIE, and quietly giving flat when vr was asked for is the
    precise failure StartPIEMode exists to prevent, so refuse with the rebuild instruction.
    """
    try:
        raw = _rc_call("StartPIEMode", {"Mode": mode}, project)
    except AgentError as exc:
        if not _is_missing_verb(exc):
            raise                      # the verb exists and genuinely failed -- never mask it
        if mode == "vr":
            raise _skew_error(
                "StartPIEMode", project,
                "VR Preview needs it; the legacy StartPIE verb can only start FLAT PIE, and "
                "starting flat when you asked for vr would hide every HMD-only bug",
            ) from None
        started = bool(_rc_call("StartPIE", {}, project))
        out = {"ok": started, "mode": "flat", "via": "StartPIE",
               "note": ("this editor's plugin predates StartPIEMode; started flat PIE with the "
                        "legacy StartPIE verb. Sync and rebuild "
                        f"{project or 'that project'} for `--mode vr`.")}
        if not started:
            out["error"] = "StartPIE returned false (no editor world?)"
        return out
    res = json.loads(raw) if isinstance(raw, str) else (raw or {})
    out = {"ok": bool(res.get("ok", False)), "mode": res.get("mode", mode), "result": res}
    if not out["ok"]:
        out["error"] = res.get("error", "StartPIEMode failed")
    return out


# --- PIE stop: CONFIRMED, not merely acked ------------------------------------------------
# `uap pie stop` used to answer {"ok":true,"result":true} the instant RemoteControl returned --
# and PIE went on running. A PIE start is QUEUED work: GEditor->RequestPlaySession only sets
# PlaySessionRequest, and the editor tick creates the play world one or more frames later. The
# engine's end-play request is a NO-OP unless that play world already exists
# (`if (PlayWorld) { bRequestEndPlayMapQueued = true; }`), so a stop landing in the gap did
# nothing at all -- observed live: the stop "succeeded", then `Creating play world package`
# appeared ~4s LATER and the session ran on. A second stop genuinely tore it down.
#
# That ok:true is a silent false signal, and the lease system is built on top of it: an agent
# that believes the stop releases its lease and hands the next agent an editor still in PIE.
#
# Two halves, neither optional:
#   1. SERIALISE (plugin, StopPIEEx): cancel a queued play-session request before asking for
#      end-play, so the stop consumes the pending start instead of racing it.
#   2. CONFIRM (here): do not return until IsPIEInProgress() -- live OR queued -- reads false,
#      within a bounded timeout; on timeout say ok:false and that the editor is NOT free.
# (1) without (2) still returns before teardown finishes; (2) without (1) can only observe the
# race, not prevent it.
_PIE_STOP_POLL_SECONDS = 0.5


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _pie_stop_timeout() -> float:
    """Seconds to wait for teardown to complete. $UAP_PIE_STOP_TIMEOUT overrides. Bounded, so a
    wedged teardown becomes a clear ok:false rather than a hang."""
    return _float_env("UAP_PIE_STOP_TIMEOUT", 30.0)


def _pie_stop_settle() -> float:
    """DEGRADED path only (see _pie_stop): how long IsInPIE must read false CONTINUOUSLY before
    a stop is believed. IsInPIE cannot see a queued start, so a single false reading is exactly
    the false signal we are fixing; the window is a heuristic, not a proof, and is reported as
    such. $UAP_PIE_STOP_SETTLE overrides."""
    return _float_env("UAP_PIE_STOP_SETTLE", 5.0)


def _pie_in_progress(project: "str | None", *, degraded: bool) -> bool:
    """True while a play session is live OR queued.

    `degraded` selects the older, WEAKER verb. IsInPIE only sees a live play world, so it reads
    false while a start is queued -- the exact window this bug lives in. It is used only when the
    plugin copy predates IsPIEInProgress, and every result built on it is labelled degraded.
    """
    return bool(_rc_call("IsInPIE" if degraded else "IsPIEInProgress", {}, project))


_DEGRADED_NOTE = (
    "this editor's plugin predates StopPIEEx/IsPIEInProgress: the stop could not cancel a QUEUED "
    "start, and the confirmation polled IsInPIE, which cannot see one. Confirmed by a settle "
    "window instead of a proof -- sync and rebuild this project for the exact check."
)


def _pie_stop(project: "str | None", timeout: float) -> dict:
    """Stop PIE and do not return ok:true until the world is actually gone."""
    t0 = time.monotonic()
    degraded = False
    try:
        raw = _rc_call("StopPIEEx", {}, project)
        res = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except AgentError as exc:
        if not _is_missing_verb(exc):
            raise                      # the verb exists and genuinely failed -- never mask it
        # Skew: an older plugin copy has only the bool StopPIE. Same question ("stop PIE"), weaker
        # guarantee -- it cannot cancel a queued start -- so fall back and SAY the result is weaker
        # rather than passing a heuristic off as the exact check.
        degraded = True
        res = {"ok": bool(_rc_call("StopPIE", {}, project)), "via": "StopPIE"}

    out: dict = {"ok": True, "result": bool(res.get("ok", False)), "stopped": False,
                 "was_playing": res.get("was_playing"),
                 "cancelled_queued_start": res.get("cancelled_queued_start"),
                 "confirmed_with": "IsInPIE" if degraded else "IsPIEInProgress"}
    if res.get("via"):
        out["via"] = res["via"]
    if degraded:
        out["degraded"] = True
        out["note"] = _DEGRADED_NOTE
    if not res.get("ok", False):
        out["ok"] = False
        out["error"] = res.get("error", "the stop request was refused (no editor world?)")
        return out

    # Clamped to the timeout so a short --timeout cannot make the settle window itself the reason
    # the stop "fails" while PIE is in fact gone.
    settle = min(_pie_stop_settle(), max(0.0, timeout)) if degraded else 0.0
    deadline = t0 + max(0.0, timeout)
    clear_since: "float | None" = None
    restops = 0
    while True:
        live = _pie_in_progress(project, degraded=degraded)
        now = time.monotonic()
        if live:
            if clear_since is not None:
                # It read clear and then came back: a queued start we could not cancel has just
                # created the play world. Stop THAT session too. Degraded path only -- against a
                # current plugin the queued request was cancelled, so this cannot happen.
                _rc_call("StopPIE", {}, project)
                restops += 1
            clear_since = None
        else:
            if clear_since is None:
                clear_since = now
            if now - clear_since >= settle:
                out["stopped"] = True
                break
        if now >= deadline:
            out["ok"] = False
            out["error"] = (
                f"PIE still in progress {round(now - t0, 1)}s after the stop request -- the editor "
                "is NOT free. Do NOT release the editor lease or hand it to another agent. Retry "
                "`uap pie stop`, or stop it in the editor by hand."
            )
            break
        time.sleep(_PIE_STOP_POLL_SECONDS)
    out["waited_seconds"] = round(time.monotonic() - t0, 2)
    if restops:
        out["restops"] = restops
    return out


def _pie(args) -> int:
    """Start/stop PIE via the plugin's version-correct RC verbs, so agents never touch the raw,
    version-fragile engine subsystem.

    `start` and `stop` are deliberately ASYMMETRIC, and that is not an oversight:
      * `stop` blocks until teardown is confirmed. It is the handover point -- the lease, the next
        agent and `report finish` all act on "the editor is free", so it must not ack a queue.
      * `start` returns as soon as the session is QUEUED, because a caller may legitimately want to
        do other work while PIE comes up (and because agents already rely on it returning at once).
        It labels itself `queued: true, confirmed: false` and points at `uap pie wait <seconds>`,
        which is the confirmation half. Nothing may treat a start ack as "the world exists".
    """
    sub = args.pie_cmd
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        if sub == "start":
            body.update(_pie_start(getattr(args, "mode", "flat") or "flat", args.project))
            if body.get("ok"):
                # An ack of QUEUED work, said out loud. See the docstring above.
                body["queued"] = True
                body["confirmed"] = False
                body["next"] = "uap pie wait <seconds>   # blocks until the game world is live"
        elif sub == "stop":
            timeout = getattr(args, "timeout", None)
            body.update(_pie_stop(args.project,
                                  _pie_stop_timeout() if timeout is None else timeout))
        elif sub == "wait":
            # IsInPIE (live play world), NOT IsPIEInProgress: `wait` asks whether the world is
            # LIVE, and a queued-but-not-yet-created session is precisely what it must keep
            # waiting through. This is the one place the narrower verb is the right question.
            deadline = time.monotonic() + args.seconds
            playing = bool(_rc_call("IsInPIE", {}, args.project))
            while not playing and time.monotonic() < deadline:
                time.sleep(0.5)
                playing = bool(_rc_call("IsInPIE", {}, args.project))
            body["playing"] = playing
            body["ok"] = playing
            if not playing:
                body["error"] = f"PIE not running after {args.seconds}s"
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture(f"pie:{sub}", {}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


# What each newer verb is FOR, quoted back to the caller when the plugin lacks it. No older
# verb does any of these jobs, so the answer is always "rebuild", never a fallback: a single
# InjectKey is not a hold, and an exec round-trip cannot sample per frame.
_NEEDS_HOLD = "sustained in-engine input; a single injected event is not a hold"
_NEEDS_SAMPLE = "per-frame property sampling; an exec round-trip cannot see a sub-second window"
_NEEDS_LOG = "reading the editor log ring buffer through the plugin"


def _input(args) -> int:
    """Sustained input. A single injected event cannot drive locomotion: the CLI round-trip is
    ~1s and a latched key is silently dropped by any FlushPressedKeys, so the plugin re-asserts
    the input every frame in-engine for the requested duration. `axis` is the VR locomotion
    verb -- thumbsticks are analog axis FKeys, not buttons."""
    sub = args.input_cmd
    t0 = time.monotonic()
    body: dict = {"ok": True, "action": sub}
    try:
        # The plugin returns a JSON envelope carrying the REAL reason for a refusal. The CLI
        # must not invent one: a guessed "unknown key name" (for a key that was in fact valid,
        # and had already been pressed) sent a live investigation after a validation table
        # that does not exist. Pass the plugin's own message through.
        if sub == "hold":
            body.update(_rc_json("HoldKey", {"KeyName": args.key, "Seconds": args.seconds},
                                 args.project, needs=_NEEDS_HOLD))
        elif sub == "axis":
            # SlateUser goes on the wire ONLY when --user was given. Two reasons: an older
            # plugin copy has no such parameter, and "" is what the plugin reads as "resolve
            # it yourself / keep the game-viewport route", which is the historical behaviour.
            params = {"AxisKeyName": args.key, "Value": args.value, "Seconds": args.seconds}
            if args.user is not None:
                params["SlateUser"] = str(args.user)
            body.update(_rc_json("HoldAxis", params, args.project, needs=_NEEDS_HOLD))
        elif sub == "release":
            body.update(_rc_json("ReleaseHeldInput", {"KeyName": args.key or ""}, args.project,
                                 needs=_NEEDS_HOLD))
        else:  # status
            body.update(_rc_json("GetHeldInput", {}, args.project, needs=_NEEDS_HOLD))

        # Non-blocking by default: the hold runs IN-ENGINE, so the point is to sample game
        # state while it is still held. --wait blocks until it expires instead.
        if body.get("ok") and sub in ("hold", "axis") and getattr(args, "wait", False):
            time.sleep(args.seconds)
            body["waited"] = True
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture(f"input:{sub}", {k: v for k, v in vars(args).items()
                              if k in ("key", "value", "seconds", "user")},
             body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    # .get: the body is merged from the plugin's envelope, so never assume the key is there.
    return 0 if body.get("ok") else 1


def _sample_stats(samples: list) -> dict:
    """Frame-to-frame movement of the sampled value: what a judder test actually asserts on.
    Works for numbers and for the {x,y,z} / {pitch,yaw,roll} objects the sampler emits."""
    def flat(v):
        if isinstance(v, (int, float)):
            return [float(v)]
        if isinstance(v, dict):
            out = []
            for key in sorted(v):
                out.extend(flat(v[key]))
            return out
        return []

    vecs = [flat(s.get("v")) for s in samples]
    vecs = [v for v in vecs if v]
    if len(vecs) < 2 or len({len(v) for v in vecs}) != 1:
        return {}
    deltas = []
    for a, b in zip(vecs, vecs[1:]):
        deltas.append(sum((y - x) ** 2 for x, y in zip(a, b)) ** 0.5)
    deltas.sort()
    n = len(deltas)
    times = [s.get("t", 0.0) for s in samples]
    span = (times[-1] - times[0]) if len(times) > 1 else 0.0
    return {
        "delta_mean": round(sum(deltas) / n, 6),
        "delta_max": round(deltas[-1], 6),
        "delta_p95": round(deltas[min(n - 1, int(n * 0.95))], 6),
        "hz": round((len(samples) - 1) / span, 1) if span > 0 else None,
    }


def _sample(args) -> int:
    """Record a property once per frame IN-ENGINE for a bounded window, then return the series.
    The finest granularity an exec round-trip can reach is ~1s, which cannot see judder, a 0.6s
    wind-up, or a one-frame pop."""
    t0 = time.monotonic()
    body: dict = {"ok": True, "object": args.object, "property": args.property}
    try:
        raw = _rc_require("StartPropertySample",
                          {"ObjectPath": args.object, "PropertyPath": args.property,
                           "Seconds": args.seconds, "MaxSamples": args.max_samples},
                          args.project, _NEEDS_SAMPLE)
        start = json.loads(raw) if isinstance(raw, str) else (raw or {})
        if not start.get("ok"):
            body = {"ok": False, "error": start.get("error", "StartPropertySample failed"),
                    "object": args.object, "property": args.property}
        elif args.no_wait:
            body["started"] = True
            body["hint"] = "sampling in-engine; read it with `uap sample read`"
        else:
            time.sleep(args.seconds + 0.25)
            raw = _rc_require("ReadPropertySample", {}, args.project, _NEEDS_SAMPLE)
            body.update(json.loads(raw) if isinstance(raw, str) else (raw or {}))
            body["stats"] = _sample_stats(body.get("samples") or [])
            if args.summary:
                body.pop("samples", None)
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture("sample", {"object": args.object, "property": args.property,
                        "seconds": args.seconds}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _sample_read(args) -> int:
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        raw = _rc_require("ReadPropertySample", {}, args.project, _NEEDS_SAMPLE)
        body.update(json.loads(raw) if isinstance(raw, str) else (raw or {}))
        body["stats"] = _sample_stats(body.get("samples") or [])
        if args.summary:
            body.pop("samples", None)
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture("sample:read", {}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _log(args) -> int:
    """Read the editor's log through the plugin's in-process capture, so log evidence lands in
    the report instead of being tailed out-of-band with shell tools -- and so it targets the
    SAME editor as every other verb (this machine runs two).

    Note: this is the plugin's 4096-line ring buffer, populated from subsystem init onward. It
    is not Saved/Logs/<Project>.log; for a whole-session history read that file directly."""
    sub = args.log_cmd
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        if sub == "cursor":
            body["cursor"] = int(_rc_require("GetLogCursor", {}, args.project, _NEEDS_LOG) or 0)
        else:
            # `log since <cursor>` and `log since --since <cursor>` both work: the docs and
            # every natural reading use the positional form, so rejecting it was a trap.
            positional = getattr(args, "cursor", None)
            after = positional if positional is not None else args.since
            if sub == "tail":
                current = int(_rc_require("GetLogCursor", {}, args.project, _NEEDS_LOG) or 0)
                after = max(0, current - args.lines)
            raw = _rc_require("GetLogsSince",
                              {"AfterCursor": after, "MaxLines": args.lines,
                               "CategoryFilter": args.category,
                               "MinVerbosity": args.verbosity},
                              args.project, _NEEDS_LOG)
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            lines = parsed.get("lines") or []
            if args.grep:
                try:
                    rx = re.compile(args.grep, re.IGNORECASE)
                except re.error as exc:
                    _emit({"ok": False, "error": f"bad --grep regex: {exc}"})
                    return 2
                lines = [ln for ln in lines if rx.search(str(ln.get("message", "")))]
            body["cursor"] = parsed.get("cursor", after)
            body["count"] = len(lines)
            body["lines"] = lines
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture(f"log:{sub}", {"grep": getattr(args, "grep", None)}, body,
             int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _helpers_payload(project: "str | None") -> list:
    """Helper descriptors with their fields intact.

    ListTestHelpers returns TArray<FAgentHelperDescriptor>, and RemoteControl's preset-call
    route serializes a returned struct through a property filter that only admits the
    function's own out/return params -- so every nested field is dropped and the call comes
    back as [{},{},...]. The plugin exposes a JSON-string twin for exactly this reason.
    """
    try:
        raw = _rc_call("ListTestHelpersJson", {}, project)
    except AgentError as exc:
        # Plugin predates the JSON twin (CLI pulled, editor not rebuilt yet). Fall back so the
        # verb still runs, and say plainly why the fields are missing. Only on an unresolvable
        # verb: an unreachable editor has to stay an unreachable editor, not "old plugin".
        if not _is_missing_verb(exc):
            raise
        legacy = _rc_call("ListTestHelpers", {}, project)
        raise AgentError(
            ErrorCode.UE_OBJECT_NOT_FOUND,
            f"this editor's plugin has no ListTestHelpersJson, and the legacy "
            f"ListTestHelpers returns {len(legacy) if isinstance(legacy, list) else '?'} "
            "entries with every field stripped by RemoteControl's preset-call serializer. "
            "Rebuild the plugin (Restart-Editor.ps1) to get helper names back.",
            recoverable=False,
        ) from None
    parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return parsed.get("helpers") or []


def _helpers(args) -> int:
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        helpers = _helpers_payload(args.project)
        if args.grep:
            try:
                rx = re.compile(args.grep, re.IGNORECASE)
            except re.error as exc:
                _emit({"ok": False, "error": f"bad --grep regex: {exc}"})
                return 2
            helpers = [h for h in helpers
                       if rx.search(str(h.get("name", ""))) or rx.search(str(h.get("category", "")))]
        if args.names:
            body["helpers"] = [h.get("name") for h in helpers]
        else:
            body["helpers"] = helpers
        body["count"] = len(helpers)
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture("helpers", {"grep": args.grep}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


_NEEDS_UI = "reading/driving on-screen UI (read-ui, click, tab, nav)"


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
        ui_raw = _rc_require("DumpViewportUI", {}, args.project, _NEEDS_UI)
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
        body = _err(exc)
    _capture("click", {"label": args.label}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _tab(args) -> int:
    """Select a CommonUI tab by its TabNameID -- menus are tab-driven, this is the #1
    navigation primitive."""
    t0 = time.monotonic()
    body: dict = {"ok": True, "tab": args.tab_id}
    try:
        ok = bool(_rc_require("SelectTab", {"TabId": args.tab_id}, args.project, _NEEDS_UI))
        body["ok"] = ok
        if not ok:
            body["error"] = (f"no tab '{args.tab_id}' on a live CommonUI tab list "
                             "(is PIE running and the menu open?)")
    except AgentError as exc:
        body = _err(exc)
    _capture("tab", {"tab": args.tab_id}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _nav(args) -> int:
    """Move UI focus / activate through Slate (up|down|left|right|accept|back) -- the path
    menus actually use, distinct from game input."""
    t0 = time.monotonic()
    body: dict = {"ok": True, "direction": args.direction}
    try:
        body["handled"] = bool(_rc_require("NavigateUI", {"Direction": args.direction},
                                           args.project, _NEEDS_UI))
    except AgentError as exc:
        body = _err(exc)
    _capture("nav", {"direction": args.direction}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _read_ui(args) -> int:
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        body["ui"] = _rc_require("DumpViewportUI", {}, args.project, _NEEDS_UI)
    except AgentError as exc:
        body = _err(exc)
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
        _rc_require("CaptureViewportWithUI", {"Filename": args.file}, args.project,
                    "capturing the viewport WITH its UMG/Slate UI")
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
        body = _err(exc)
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
  * NEVER tight-loop `uap exec` / RC while PIE is still initializing or transitioning -- Python
    remote-exec runs on the GAME THREAD and re-enters the engine task graph, HARD-CRASHING the editor
    (`RecursionGuard`, TaskGraph.cpp). Wait for PIE via the OS window (title has the project + "Preview")
    + a short settle delay, THEN exec/inject. Do NOT poll get_game_world() in a loop during startup.
  * Multiple agents share ONE editor. Rebuild ONLY via Restart-Editor.ps1 (it takes the exclusive
    lease); never kill/msbuild the editor by hand. `uap` editor ops auto-WAIT through another agent's
    rebuild (they block then resume -- a slow call is not an error). Hold PIE/level across calls with
    `uap lease acquire exclusive --reason pie --agent <tok>` ... `uap lease release --agent <tok>`.
  * The CLI is SHARED by every project; each project vendors its OWN plugin copy, so a project
    that has not synced+rebuilt is behind this CLI. A verb its plugin lacks now answers "this
    editor's plugin has no <Verb> ... sync and rebuild <project>" -- that is a TOOLING version
    gap, not a broken editor and not a product bug. Flat `pie start` degrades to the legacy
    verb automatically; `--mode vr`, `input hold/axis`, `sample` and `helpers` cannot and say so.
  * `uap exec` runs IN-PROCESS: a bad call can HARD-CRASH the editor (taking RC + your run down).
    Known landmine: the engine's `DataTableFunctionLibrary.ExportDataTableToJSONString` check()-
    crashes on some row-struct shapes (JsonWriter assert "Stack.Top() == EJson::Object"). To read a
    DataTable, iterate rows -- `get_editor_property('row_names')` / row handles + per-row reads --
    NEVER ExportDataTableToJSONString. Treat any whole-asset "...ToJSONString" exporter as unsafe.

PREFLIGHT
  uap status                      liveness + plugin version + resolved RC port
  uap report diag                 capture editor/level/PIE + frame perf into the report

REPORT (a verification is NOT done until `report finish` emits the HTML report; cite its path)
  uap report start "<question>" [--no-require-screenshot]   # screenshot REQUIRED for pass by default
  uap report assert "<label>" pass|fail "<evidence>"
  uap report note "<text>"
  uap report finish pass|fail "<summary>"   # pass w/o screenshot -> FAIL; also auto-stops PIE (--keep-pie to skip)
  -> attach proof: `uap screenshot <abs.png>` (auto-attaches), or `uap report screenshot <file>`

PLAY-IN-EDITOR
  uap pie start                   start PIE (version-correct; do NOT use PlayWorldEditorSubsystem)
  uap pie start --mode vr         start VR PREVIEW instead -- the HMD code path (OpenXR input,
                                  IsHeadMountedDisplayEnabled branches). Flat PIE takes neither,
                                  so an HMD-only bug looks absent there. Needs a connected headset,
                                  and a plugin copy new enough to have StartPIEMode -- it REFUSES
                                  on an older one rather than quietly giving you flat PIE.
  uap pie wait <seconds>          block until the game world is live. REQUIRED after `pie start`:
                                  start only QUEUES the session (it answers queued:true,
                                  confirmed:false) -- the world does not exist yet when it returns.
  uap pie stop [--timeout 30]     stop PIE and WAIT until the teardown is confirmed. ok:true means
                                  the world is gone; on timeout it FAILS and says the editor is
                                  not free. Never treat a stop as done without ok:true+stopped:true.

MULTI-AGENT COORDINATION (several agents sharing one editor; see docs/agent-coordination.md)
  Rebuild ONLY via Restart-Editor.ps1 -- it self-locks; never bounce the editor by hand.
  Editor-touching verbs auto-wait through another agent's rebuild (block then resume, not fail).
  uap lease status                          who holds the editor + why
  uap lease acquire exclusive --reason pie --agent <tok> [--wait 900]   # hold PIE/level across calls
  uap lease release --agent <tok>           # ...then release (pass the SAME token every call).
                                            REFUSES while PIE is still in progress -- stop PIE
                                            first (`uap pie stop`), or --force to hand over a
                                            live session on purpose.

DRIVE + OBSERVE
  uap rc <Func> [key=value ...]   call a plugin UFUNCTION (one-shot input injection lives here)
  uap exec "<python>"             run `import unreal; ...` in the editor (escape hatch)
  uap read-ui                     dump on-screen UMG: [{text, x, y}, ...] + focused
  uap click "<label>"             click an on-screen UMG element by its visible text
  uap tab "<TabId>"               select a CommonUI tab by id (menus are tab-driven)
  uap nav up|down|left|right|accept|back   move UI focus / activate (Slate nav path)
  uap screenshot <file>           capture composited game+UMG frame (needs live PIE)
  uap helpers [--grep RE] [--names]   list the project's test helpers with their arg schemas

SUSTAINED INPUT (a single injected event CANNOT drive locomotion -- see below)
  uap input hold <Key> --seconds N     hold a digital key; returns at once, held in-engine
  uap input axis <AxisKey> <v> --seconds N   drive an analog axis -- THE VR LOCOMOTION VERB
  uap input axis <AxisKey> <v> --user N      ...on the SLATE route as Slate user N, which is
                                       the only route an analog/virtual cursor or any
                                       RegisterInputPreProcessor handler can see. Without it
                                       the sample goes to the game viewport, BELOW Slate
  uap input release                    RECOVERY: release every hold AND flush any key the
                                       engine still has down (clears a stuck key without a
                                       PIE restart). Run it if input starts behaving oddly.
  uap input release <Key>              force-release one key, held or not
  uap input status                     what is held, for how long, and whether it is really
                                       down in the engine (`down`)
  Why: `rc InjectKey bPressed=true` is ONE event. The CLI round-trip is ~1s, so re-injecting
  per poll cannot cover a sub-second window, and any FlushPressedKeys (input-mode change,
  focus loss, PC recreation) silently drops a latched key. `input hold/axis` re-asserts the
  input every frame INSIDE the engine, then releases. VR sticks are AXES, not buttons.
  Key names are exact FKeys from the engine's own registry (W, C, LeftControl, SpaceBar,
  Gamepad_LeftY, OculusTouch_Left_Thumbstick_Y). A refused hold presses NOTHING.

SAMPLING + LOGS (sub-second truth; a ~1s exec round-trip cannot see judder or a 0.6s wind-up)
  uap sample start <object> <property> --seconds N   per-frame series + delta stats
      object: /Game/... path | actor name in the live world | PlayerPawn | PlayerController
              | PlayerCameraManager
      property: dot path (CharacterMovement.Velocity) or a computed leaf
              (WorldLocation|WorldRotation|WorldScale|WorldTransform|ForwardVector|Velocity)
  uap sample read [--summary]          read the series (use with `sample start --no-wait`)
  uap log cursor                       grab a cursor BEFORE driving the condition
  uap log since <cursor> --grep RE     what the editor logged since then
  uap log tail --lines 200 --grep RE   the last N captured lines

RECIPES
  Click an on-screen button by label (one call):
    uap click "VR TRAINING"
  ...or the underlying chain (what `uap click` does), e.g. to click a precise spot:
    uap read-ui                                   # find the element's x,y
    uap rc InjectMouseMove X=<x> Y=<y> bAbsolute=true
    uap rc InjectMouseButton Button=Left bPressed=true
    uap rc InjectMouseButton Button=Left bPressed=false
  Press a key once (routes through the real input path):
    uap rc InjectKey KeyName=E bPressed=true ; uap rc InjectKey KeyName=E bPressed=false
  WALK for 3 seconds and read state WHILE moving (this is what one-shot injection cannot do):
    uap input hold W --seconds 3
    uap rc CallTestHelper Name=... JsonArgs={}      # runs while the key is still held
  VR locomotion -- push the left stick forward for 3s (thumbstick is an AXIS):
    uap input axis OculusTouch_Left_Thumbstick_Y 1.0 --seconds 3

  Drive a SLATE analog/virtual cursor (a pre-processor, not gameplay input). Without --user
  the sample takes the viewport route, below Slate, and the cursor never moves -- with no
  error, which reads as a broken cursor. The result says which route it took:

    uap input axis Gamepad_LeftX 1.0 --seconds 2 --user 0
    uap input status               # route: slate / user_index: 0 while it is held

  VR controller button:
    uap rc InjectXRButton Hand=Right ButtonKeyName=OculusTouch_Right_Trigger_Click bPressed=true
  Prove something is smooth (or juddering) at frame rate:
    uap sample start PlayerCameraManager WorldLocation --seconds 2
    # -> stats.delta_max / delta_p95 are the per-frame movement; a spiky p95 IS the judder
  Tie a log line to an action:
    C=$(uap log cursor | ...)      # grab the cursor first
    uap input hold W --seconds 2
    uap log since $C --grep "Janitor|Catch"
  Input acting up (pawn stuck crouched, movement that won't stop)? Clear it without a restart:
    uap input status               # what the registry thinks is held + engine `down` truth
    uap input release              # release all holds AND flush any key still down
  Select a CommonUI tab / move focus:
    uap tab "VRTraining"            # select tab by id
    uap nav down ; uap nav accept   # focus nav + activate
  Read game-truth (preferred over screenshots): uap rc CallTestHelper Name=... JsonArgs={}
    list helpers: uap helpers --names

FLAGS: --project <name> and --agent <token> are accepted by EVERY verb (ignored by the ones
that don't touch the editor), so you can pass the same pair on every call in a run.

MORE: docs/agent-testing.md (usage), docs/capabilities.md (every tool), docs/known-issues.md.
Per-verb flags: uap <verb> --help
"""


def _help(args) -> int:
    print(_HELP_CATALOG)
    return 0


# Editor-touching verbs auto-wait while another agent is rebuilding this editor (see main()).
_REBUILD_GUARDED = {"status", "rc", "exec", "exec-file", "pie",
                    "read-ui", "click", "tab", "nav", "screenshot",
                    "input", "sample", "log", "helpers"}

# ...and additionally wait out ANY other agent's exclusive lease (pie / level / rebuild).
# `status` is deliberately exempt: it is the health probe you reach for WHILE diagnosing a
# stuck editor, so it must always answer instead of blocking behind the thing you are probing.
_LEASE_GUARDED = _REBUILD_GUARDED - {"status"}


def _pie_state_for_lease(project: "str | None") -> "tuple[bool | None, str]":
    """Best-effort "is this editor still in PIE", for the release guard.

    Returns (in_progress, how). `None` means the question could not be asked at all (editor down /
    RC unreachable) -- a dead editor has no PIE session, so the caller proceeds. Prefers the exact
    verb and degrades to IsInPIE on an older plugin copy; either answer is enough to refuse.
    """
    for func in ("IsPIEInProgress", "IsInPIE"):
        try:
            return bool(_rc_call(func, {}, project)), func
        except AgentError as exc:
            if _is_missing_verb(exc):
                continue        # older plugin copy: try the narrower verb
            return None, "unreachable"
        except Exception:
            return None, "unreachable"
    return None, "unreachable"


def _lease(args) -> int:
    """Multi-agent coordination lease for a shared editor. See docs/agent-coordination.md."""
    proj = getattr(args, "project", "") or _env_project()
    cmd = args.lease_cmd
    if cmd == "acquire":
        res = _coord.acquire(proj, args.mode, reason=args.reason, agent=args.agent,
                             pid=args.pid, wait=args.wait, ttl=args.ttl)
    elif cmd == "release":
        # Releasing says "the editor is free"; the next agent takes the lease on that word alone.
        # A release granted while PIE is still live is therefore worse than a stop that lies: by
        # the time anyone notices, a DIFFERENT agent is already driving an editor mid-session, and
        # nothing in the system will ever tell either of them. So check, and refuse.
        # Fail-open on an unreachable editor (nothing to protect) and --force for a deliberate
        # handover of a live session.
        live, how = (None, "skipped (--force)") if args.force else _pie_state_for_lease(proj)
        if live:
            res = {"ok": False, "released": False, "pie_live": True, "checked_with": how,
                   "agent": args.agent or _coord.default_agent_id(),
                   "error": ("refusing to release the editor lease: PIE is still in progress, so "
                             "the editor is NOT free. Run `uap pie stop` (it now waits for the "
                             "teardown and fails if it does not happen), then release. Pass "
                             "--force only if you deliberately mean to hand over a live session.")}
        else:
            res = _coord.release(proj, agent=args.agent)
            res["pie_live"] = live
            res["checked_with"] = how
    elif cmd == "heartbeat":
        res = _coord.heartbeat(proj, agent=args.agent)
    else:  # status
        res = _coord.status(proj)
    _emit(res)
    return 0 if res.get("ok", True) else 1


def _env_project() -> str:
    """Default editor target. Comes from $UAP_PROJECT -- which the per-project uap.ps1 launcher
    pins -- so a command run from a project's launcher targets THAT editor. Empty (no launcher,
    no env) means 'first editor that answers', NOT a hardcoded project: a hardcoded default made
    commands cross-target the wrong editor (e.g. starting PIE in the wrong project)."""
    return os.environ.get("UAP_PROJECT", "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uap")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ONE flag set, accepted by EVERY verb -- including the ones that never touch the editor.
    # The documented workflow tells agents to pass the SAME --agent token on every related call,
    # so appending it everywhere is the natural behaviour; a verb that hard-errored on it
    # (`report assert ... --agent <tok>`: "unrecognized arguments") broke whole runs. --project
    # is the same class of trap, so it lives here too. Verbs that do not touch the editor accept
    # and ignore both rather than failing.
    #
    # --project also carries the per-editor targeting: the RC HTTP port is resolved per editor
    # (by project), so two open editors are each addressed on their own port instead of both
    # hitting 30010. Empty means "first editor that answers", never a hardcoded project.
    proj = argparse.ArgumentParser(add_help=False)
    proj.add_argument("--agent", default=None,
                      help="your lease token (default $UAP_AGENT_ID). If ANOTHER agent holds "
                           "the exclusive lease an editor op waits for it; passing your own "
                           "token is what stops your own lease from blocking you. Accepted "
                           "(and ignored) on verbs that do not touch the editor.")
    proj.add_argument("--project", default=_env_project(),
                      help="editor to target (default $UAP_PROJECT); RC port resolved per "
                           "project. Ignored by verbs that do not touch the editor.")
    common = proj

    sub.add_parser("help", parents=[common],
                   help="catalog of verbs + copy-paste recipes").set_defaults(func=_help)
    sub.add_parser("tools", parents=[common], help="alias of help").set_defaults(func=_help)

    rep = sub.add_parser("report").add_subparsers(dest="rcmd", required=True)
    rs = rep.add_parser("start", parents=[common])
    rs.add_argument("task")
    # (--project comes from the shared parent; report start records it on the session.)
    # Screenshot proof is REQUIRED by default: `finish pass` auto-downgrades to fail unless a
    # screenshot is attached. --require-screenshot is the (now redundant) explicit-on;
    # --no-require-screenshot opts out for a genuinely headless/no-visual check.
    rs.add_argument("--require-screenshot", dest="require_screenshot", action="store_true",
                    default=True, help="(default) require a screenshot for a passing report")
    rs.add_argument("--no-require-screenshot", dest="require_screenshot", action="store_false",
                    help="rare: allow a pass with no screenshot (justify in the summary)")
    rs.set_defaults(func=_report_start)
    ra = rep.add_parser("assert", parents=[common])
    ra.add_argument("label")
    ra.add_argument("verdict", choices=["pass", "fail"])
    ra.add_argument("evidence", nargs="?", default="")
    ra.set_defaults(func=_report_assert)
    rn = rep.add_parser("note", parents=[common])
    rn.add_argument("text")
    rn.set_defaults(func=_report_note)
    rd = rep.add_parser("diag", parents=[common])
    # --project (shared parent) selects the editor these diagnostics are read from, via exec.
    rd.set_defaults(func=_report_diag)
    rsh = rep.add_parser("screenshot", parents=[common])
    rsh.add_argument("file")
    rsh.add_argument("--caption", default="")
    rsh.set_defaults(func=_report_screenshot)
    rf = rep.add_parser("finish", parents=[common])
    rf.add_argument("verdict", choices=["pass", "fail"])
    rf.add_argument("summary")
    rf.add_argument("--keep-pie", action="store_true",
                    help="do not auto-stop PIE on finish (default: stop it so a finished test "
                         "never leaves the editor stuck in Play-In-Editor)")
    rf.set_defaults(func=_report_finish)

    st = sub.add_parser("status", parents=[proj])
    st.set_defaults(func=_status)
    rcp = sub.add_parser("rc", parents=[proj])
    rcp.add_argument("rc_func")
    rcp.add_argument("params", nargs="*",
                     help="key=value pairs (e.g. Command=stat fps KeyName=E bPressed=true) "
                          "or a single JSON object")
    rcp.set_defaults(func=_rc)
    ex = sub.add_parser("exec", parents=[proj])
    ex.add_argument("code")
    ex.set_defaults(func=_exec)
    exf = sub.add_parser("exec-file", parents=[proj])
    exf.add_argument("path")
    exf.set_defaults(func=_exec_file)
    pie = sub.add_parser("pie").add_subparsers(dest="pie_cmd", required=True)
    ps = pie.add_parser("start", parents=[proj])
    ps.add_argument("--mode", choices=["flat", "vr"], default="flat",
                    help="'vr' uses the editor's VR Preview -- the HMD code path (OpenXR input, "
                         "IsHeadMountedDisplayEnabled branches) that flat PIE never takes. "
                         "Needs a connected headset; fails with a reason if there is none.")
    ps.set_defaults(func=_pie)
    pst = pie.add_parser("stop", parents=[proj])
    pst.add_argument("--timeout", type=float, default=None,
                     help="max seconds to wait for teardown to be CONFIRMED (default 30, "
                          "$UAP_PIE_STOP_TIMEOUT). On timeout the verb fails rather than "
                          "acking a stop that did not happen.")
    pst.set_defaults(func=_pie)
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

    # Sustained input. The plugin re-asserts the input every frame in-engine for the duration,
    # which is the only way to hold anything across a ~1s CLI round-trip -- and the only way to
    # drive an analog stick at all, since a real stick re-sends its value every frame.
    inp = sub.add_parser("input", help="hold a key / drive an analog axis for N seconds")
    inps = inp.add_subparsers(dest="input_cmd", required=True)
    ih = inps.add_parser("hold", parents=[proj], help="hold a digital key for N seconds")
    ih.add_argument("key", help="FKey name, e.g. W / SpaceBar / OculusTouch_Right_Trigger_Click")
    ih.add_argument("--seconds", type=float, default=1.0)
    ih.add_argument("--wait", action="store_true",
                    help="block until the hold expires (default: return immediately so you can "
                         "read game state WHILE it is held)")
    ih.set_defaults(func=_input)
    ia = inps.add_parser("axis", parents=[proj],
                         help="drive an analog axis FKey for N seconds (VR/gamepad sticks)")
    ia.add_argument("key", help="axis FKey, e.g. OculusTouch_Left_Thumbstick_Y or Gamepad_LeftY")
    ia.add_argument("value", type=float, help="-1.0 .. 1.0")
    ia.add_argument("--seconds", type=float, default=1.0)
    ia.add_argument("--wait", action="store_true", help="block until the hold expires")
    # Slate DISCARDS an input event whose user index does not match the handler's owning user
    # (FAnalogCursor::IsRelevantInput -- engine AnalogCursor.cpp:192). Without --user the sample
    # takes the game-viewport route, which never enters the Slate pre-processor chain at all, so
    # an analog/virtual cursor sees nothing either way. --user picks the Slate route AND the user.
    ia.add_argument("--user", type=int, default=None, metavar="N",
                    help="drive the SLATE route as Slate user N (what an analog/virtual cursor "
                         "or any input pre-processor sees). Omit for the game-viewport route "
                         "(gameplay/Enhanced Input). Refuses loudly if Slate has no user N")
    ia.set_defaults(func=_input)
    ir = inps.add_parser("release", parents=[proj], help="end a hold early (default: all holds)")
    ir.add_argument("key", nargs="?", default="", help="FKey name; omit to release everything")
    ir.set_defaults(func=_input)
    inps.add_parser("status", parents=[proj],
                    help="what is currently held and for how much longer").set_defaults(func=_input)

    # Frame-rate property sampling: sub-second behaviour a ~1s exec round-trip cannot see.
    smp = sub.add_parser("sample", help="record a property per-frame in-engine, return the series")
    smps = smp.add_subparsers(dest="sample_cmd", required=True)
    sst = smps.add_parser("start", parents=[proj], help="sample a property for N seconds")
    sst.add_argument("object", help="object path (/Game/...), an actor name in the live world, "
                                    "or PlayerPawn / PlayerController / PlayerCameraManager")
    sst.add_argument("property", help="dot path, e.g. CharacterMovement.Velocity, or a computed "
                                      "leaf: WorldLocation|WorldRotation|WorldScale|"
                                      "WorldTransform|ForwardVector|Velocity")
    sst.add_argument("--seconds", type=float, default=2.0)
    sst.add_argument("--max-samples", dest="max_samples", type=int, default=5000)
    sst.add_argument("--no-wait", dest="no_wait", action="store_true",
                     help="return as soon as sampling starts (read it later with `sample read`)")
    sst.add_argument("--summary", action="store_true",
                     help="omit the raw series; keep only the stats")
    sst.set_defaults(func=_sample)
    sr = smps.add_parser("read", parents=[proj], help="read the series collected so far")
    sr.add_argument("--summary", action="store_true")
    sr.set_defaults(func=_sample_read)

    # Editor log, through the plugin's in-process capture -- same project targeting as every
    # other verb, and the lines land in the report instead of a side-channel shell tail.
    lg = sub.add_parser("log", help="read the editor log (cursor-based, grep-able)")
    lgs = lg.add_subparsers(dest="log_cmd", required=True)
    for name, helptext in (("tail", "the last N captured lines"),
                           ("since", "lines after a cursor from an earlier call")):
        lp = lgs.add_parser(name, parents=[proj], help=helptext)
        lp.add_argument("--lines", type=int, default=200)
        lp.add_argument("--grep", default="", help="case-insensitive regex over the message")
        lp.add_argument("--category", default="", help="exact log category, e.g. LogUAP")
        lp.add_argument("--verbosity", default="Log",
                        choices=["Fatal", "Error", "Warning", "Display", "Log",
                                 "Verbose", "VeryVerbose"],
                        help="minimum verbosity to include (default Log)")
        lp.add_argument("--since", type=int, default=0,
                        help="cursor from a previous call (required for `log since`)")
        if name == "since":
            # Positional form too -- `uap log since 42` is what the docs show and what reads
            # naturally; only accepting --since made the documented incantation an error.
            lp.add_argument("cursor", type=int, nargs="?", default=None,
                            help="cursor from `uap log cursor` (same as --since)")
        lp.set_defaults(func=_log)
    lgs.add_parser("cursor", parents=[proj],
                   help="current log cursor -- grab one BEFORE driving the condition"
                   ).set_defaults(func=_log)

    hp = sub.add_parser("helpers", parents=[proj],
                        help="list the project's test helpers (names + arg schemas)")
    hp.add_argument("--grep", default="", help="case-insensitive regex over name/category")
    hp.add_argument("--names", action="store_true", help="just the names")
    hp.set_defaults(func=_helpers)

    # Multi-agent coordination: a per-editor lease so agents take turns instead of stepping on
    # each other. See docs/agent-coordination.md. Editor-touching verbs auto-wait through a
    # rebuild (main()); these verbs are for explicit exclusive holds (PIE/level) + inspection.
    lz = sub.add_parser("lease", help="editor coordination lease (multi-agent turn-taking)")
    lzs = lz.add_subparsers(dest="lease_cmd", required=True)
    la = lzs.add_parser("acquire", parents=[proj],
                        help="block until an exclusive|shared lease is free, then take it")
    la.add_argument("mode", choices=["exclusive", "shared"])
    la.add_argument("--reason", default="",
                    help="rebuild|pie|level|... (rebuild* makes other agents' ops auto-wait)")
    # --agent comes from the shared `proj` parent (also honours $UAP_AGENT_ID). It is REQUIRED for
    # a hold spanning multiple calls -- this harness has no reliable auto-id -- and it is the same
    # token your later editor ops must carry, or your own lease will block you.
    # Default 0 (TTL-only): a standalone `lease acquire` process exits immediately, so anchoring
    # liveness to it would evict the lease the instant the command returns. Pass --pid <PID> of a
    # long-lived process (e.g. a rebuild script's $PID) to have PID-death reclaim it sooner.
    la.add_argument("--pid", type=int, default=0,
                    help="liveness-anchor PID (default 0 = TTL-only, correct for a cross-call hold; "
                         "pass a long-lived process's PID to also reclaim on its death)")
    la.add_argument("--wait", type=float, default=_coord.DEFAULT_WAIT_CAP,
                    help="max seconds to block before returning busy (default 900)")
    la.add_argument("--ttl", type=int, default=None)
    la.set_defaults(func=_lease)
    lzr = lzs.add_parser("release", parents=[proj])
    lzr.add_argument("--force", action="store_true",
                     help="release even though PIE is still in progress. Without it, release "
                          "REFUSES while the editor is mid-session -- handing a live PIE to the "
                          "next agent is the failure the lease exists to prevent.")
    lzr.set_defaults(func=_lease)
    lzs.add_parser("heartbeat", parents=[proj]).set_defaults(func=_lease)
    lzs.add_parser("status", parents=[proj]).set_defaults(func=_lease)
    return p


def _lease_wait_cap() -> float:
    """Seconds an editor op will wait out another agent's exclusive lease.

    $UAP_LEASE_WAIT overrides (0 = do not wait, fail fast with `busy`). Bounded, because a
    forgotten lease must degrade into a clear error rather than a hang.
    """
    raw = os.environ.get("UAP_LEASE_WAIT")
    if raw is None or raw.strip() == "":
        return _coord.DEFAULT_WAIT_CAP
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _coord.DEFAULT_WAIT_CAP


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = getattr(args, "cmd", None)
    project = getattr(args, "project", "") or _env_project()
    # Coordination: if another agent is rebuilding this editor (it's down), wait it out instead of
    # hard-failing, then proceed against the relaunched editor. Fail-open; only editor-touching
    # verbs are guarded (lease/report verbs manage or don't need the editor).
    if cmd in _REBUILD_GUARDED:
        try:
            _coord.wait_while_rebuild(project)
        except Exception:
            pass
    # ...and wait out any OTHER agent's exclusive lease. Without this the lease was advisory
    # only: `lease acquire exclusive --reason pie` recorded a holder that nothing consulted, so
    # other agents drove the editor straight through it (swapping levels and starting PIE under
    # the holder's feet). `wait_if_blocked` existed for exactly this and had no callers.
    if cmd in _LEASE_GUARDED:
        agent = getattr(args, "agent", None) or _coord.default_agent_id()
        try:
            res = _coord.wait_if_blocked(project, agent=agent, wait=_lease_wait_cap())
        except Exception:
            res = {"ok": True, "blocked": False}
        if not res.get("ok", True) and res.get("blocked"):
            holder = res.get("holder") or {}
            _emit({"ok": False, "busy": True, "blocked_by": holder.get("agent"),
                    "reason": holder.get("reason"), "cmd": cmd, "agent": agent,
                    "hint": "another agent holds the exclusive editor lease; wait, or pass "
                            "--agent/$UAP_AGENT_ID if that lease is yours "
                            "(`uap lease status` to inspect, `uap lease release --agent <token>` "
                            "if it is abandoned)"})
            return 1
        # Using the editor IS liveness: refresh our own lease so an actively-working holder never
        # has it reclaimed mid-hold, and an abandoned one still ages out on TTL.
        try:
            _coord.heartbeat(project, agent=agent)
        except Exception:
            pass
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
