from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import itertools
import json
import os
import pathlib
import re
import shlex
import sys
import time
from datetime import datetime as datetime_cls
from datetime import timezone

from unreal_agent_player import contract as _contract
from unreal_agent_player import coordination as _coord
from unreal_agent_player import throttle as _throttle
from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.reporting import session as sess
from unreal_agent_player.reporting import viewer as _viewer
from unreal_agent_player.reporting.render import render
from unreal_agent_player.transport import PythonRemoteExecClient, RemoteControlClient


def _load_active() -> sess.ReportSession | None:
    run = sess.get_active_run()
    if run is None or not (run / "data.json").exists():
        return None
    return sess.ReportSession.load(run)


def _require_active(agent: str | None = None):
    """Return the active session, or None after emitting the no-session error.

    Two failures used to arrive as the same bare `no active report`, and neither said what
    happened. The slot is ONE per machine: a second `report start` took it, so a first agent's
    later note/assert either wrote into a STRANGER'S report or, once that one finished and
    cleared the pointer, got told there was no report at all -- as if it had forgotten to start
    one. Both are now named.
    """
    s = _load_active()
    if s is None:
        body = {"ok": False, "error": "no active report; run `uap report start` first"}
        lost = sess.find_superseded_run(agent)
        if lost:
            by = lost.get("superseded_by") or {}
            body["error"] = (
                f"no active report. NOTE: run {lost['run_dir']} (task {lost.get('task')!r}, "
                f"agent {lost.get('agent')!r}) LOST the machine-wide report slot at "
                f"{by.get('at')} to a `report start` from agent {by.get('agent')!r} -- there is "
                f"one slot per machine. If that was your report, its evidence up to that point "
                f"is in that directory; anything after it was never recorded. Start a fresh "
                f"report and pass --agent <your-token> so the slot cannot be taken silently.")
            body["superseded"] = lost
        _emit(body)
        return None
    holder = sess.get_active_owner() or {}
    held_agent = (holder.get("agent") or "").strip()
    mine = (agent or "").strip()
    if held_agent and mine and held_agent != mine:
        _emit({"ok": False, "error": (
            f"the active report belongs to agent {held_agent!r} (task {getattr(s, 'task', '')!r}, "
            f"run_dir {holder.get('run_dir')}), not to you ({mine!r}). There is ONE report slot "
            f"per machine, so writing here would put your evidence in their report. Wait for "
            f"them to finish, then `uap report start` your own."),
            "held_by": held_agent, "run_dir": holder.get("run_dir")})
        return None
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
    """Claim the machine-wide report slot.

    It REFUSES when a running report is held by a different --agent token. Before this it
    overwrote the pointer unconditionally, so two agents verifying at once collided quietly:
    the first agent's evidence stopped being recorded and it found out through a bare `no
    active report` some calls later. With no token on either side there is no identity to
    compare, so the take still proceeds -- but the displaced run is closed as `incomplete`,
    rendered, and named here rather than abandoned mid-write.
    """
    try:
        s = sess.start_session(task=args.task, project=args.project,
                               requires_screenshot=args.require_screenshot,
                               agent=args.agent, takeover=getattr(args, "takeover", False))
    except sess.ReportSlotConflict as exc:
        _emit({"ok": False, "error": str(exc), "busy": True,
               "held_by": exc.holder.get("agent"), "run_dir": exc.holder.get("run_dir")})
        return 1
    out = {"ok": True, "run_dir": str(s.run_dir),
           "requires_screenshot": s.requires_screenshot, "agent": s.agent}
    displaced = getattr(s, "displaced", None)
    if displaced:
        out["displaced"] = displaced
        out["warning"] = (
            f"this start TOOK the machine-wide report slot from a still-running report "
            f"({displaced['run_dir']}, task {displaced.get('task')!r}, agent "
            f"{displaced.get('agent')!r}). There is one slot per machine. That run has been "
            f"closed as `incomplete` and rendered, so its evidence so far is not lost -- but "
            f"if another agent is verifying right now, it just stopped being able to record. "
            f"Pass --agent <your-token> on every report call so this is refused instead.")
    if not s.agent:
        out["hint"] = ("no --agent token on this report, so nothing can stop another agent's "
                       "`report start` from taking the slot from you. Pass --agent <token> "
                       "(or set $UAP_AGENT_ID) on every report call.")
    _emit(out)
    return 0


def _report_assert(args) -> int:
    s = _require_active(getattr(args, "agent", None))
    if s is None:
        return 2
    s.add_assertion(args.label, args.verdict == "pass", args.evidence)
    _emit({"ok": True})
    return 0


def _report_note(args) -> int:
    s = _require_active(getattr(args, "agent", None))
    if s is None:
        return 2
    s.add_note(args.text)
    _emit({"ok": True})
    return 0


def _report_screenshot(args) -> int:
    """Attach an EXISTING image file to the active report (vs the top-level `screenshot`
    verb, which captures from the editor via RC and attaches). Useful when the image was
    produced another way (e.g. `uap exec` HighResShot from an editor RC can't reach)."""
    s = _require_active(getattr(args, "agent", None))
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
    s = _require_active(getattr(args, "agent", None))
    if s is None:
        return 2
    # Read back through the command RESULT in a private namespace (quiet_expr), not print():
    # a print is a LogPython line in the editor log, and the old top-level `ss`/`ws`/`w`
    # bindings left a UWorld rooted in the shared remote-exec globals -- exactly what kills the
    # editor on the next level load (ClickUp 17tm466jt8t, 17tm466g07m).
    code = (
        "import unreal, json\n"
        "ss = unreal.get_editor_subsystem(unreal.UAPAgentSubsystem)\n"
        "ws = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
        "w = ws.get_game_world() or ws.get_editor_world()\n"
        "_uap_out = json.dumps({"
        "'plugin_version': ss.get_plugin_version(),"
        "'world': (w.get_name() if w else None),"
        "'is_in_pie': ss.is_in_pie(),"
        "'unit': ss.get_stat_group_text('unit'),"
        "'fps': ss.get_stat_group_text('fps')})\n"
    )
    body: dict = {"ok": True}
    try:
        client = PythonRemoteExecClient(node_project_substr=args.project)
        raw = client.eval_quiet(code)
        diag = None
        if isinstance(raw, str):
            try:
                diag = json.loads(raw)
            except json.JSONDecodeError:
                diag = None
        if not isinstance(diag, dict):
            diag = None
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
            # A ~3 fps reading is the editor's not-foreground throttle, not the game. Say so
            # HERE, in the output the agent is reading. The HTML report says it too, but
            # render.py re-derives it from these same numbers rather than us writing a note --
            # so it also fires for reports captured before this existed, and the agent's own
            # notes stay its own evidence instead of carrying a tool warning twice.
            body.update(_throttle.throttle_annotation(perf))
    except AgentError as exc:
        body = _err(exc)
    _capture("report:diag", {"project": args.project}, body, 0)
    _emit(body)
    return 0 if body["ok"] else 1


def _report_finish(args) -> int:
    s = _require_active(getattr(args, "agent", None))
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
    # One window per testing session, not one tab per report: this REPLACES the window the
    # previous report opened instead of adding another (see reporting/viewer.py). `html` below
    # still names the per-run file -- agents quote that path, and nothing about it moved.
    shown = _viewer.open_report(html_path, no_open=getattr(args, "no_open", False) or None)
    out = {"ok": True, "html": str(html_path), "verdict": s.status,
           "downgraded": s.status != args.verdict, "pie_stopped": pie_stopped,
           "opened": shown.get("opened", False)}
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
    # Read back through the command RESULT, never print(): a print is a LogPython line in the
    # editor's log on every call (ClickUp 17tm466jt8t). See PythonRemoteExecClient.quiet_expr.
    code = ("import unreal\n"
            "_uap_out = int("
            "unreal.get_editor_subsystem(unreal.UAPAgentSubsystem).get_remote_control_port())\n")
    try:
        port = PythonRemoteExecClient(node_project_substr=project).eval_quiet(code)
    except AgentError:
        return 0
    return port if isinstance(port, int) and not isinstance(port, bool) else 0


def _exec_project_name(project: str | None) -> str | None:
    """The project name of the editor matching `project` (via exec). Used to stamp a
    screenshot's provenance so a pass can't be proven with a shot of another editor."""
    # Result, not print() -- see _exec_rc_port. This runs on every `uap screenshot`.
    code = ("import unreal\n"
            "_uap_out = unreal.Paths.get_project_file_path()"
            ".rsplit('/',1)[-1].rsplit('.',1)[0]\n")
    try:
        name = PythonRemoteExecClient(node_project_substr=(project or "")).eval_quiet(code)
    except AgentError:
        return None
    return name.strip() if isinstance(name, str) and name.strip() else None


def _rc_port_for(project: str | None) -> int:
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


def _rc_call(func: str, params: dict, project: str | None = None):
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


def _skew_error(func: str, project: str | None, needs: str) -> AgentError:
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


def _live_contract(project: str | None) -> dict | None:
    """What this project's editor ACTUALLY exports right now: {verb: {arg: declared_type}},
    read from its RemoteControl preset. None when it cannot be read.

    This is the proactive half of the skew story. `_rc_require` below still catches a missing
    verb REACTIVELY, after a call has already failed; this answers the same question up front,
    and answers the question a 404 cannot -- "the verb is there, but does it have the parameter
    I am about to send?". See contract.py for why neither side is a hand-bumped number.

    One localhost GET. Every failure is None, so a call that would have worked still does.
    """
    return _contract.fetch_live_contract(_rc_port_for(project))


def _contract_report(project: str | None) -> dict:
    """Compare what this checkout's plugin header declares against what the editor exports."""
    live = _live_contract(project)
    report = _contract.compare(_contract.expected_contract(), live)
    msg = _contract.skew_message(report, project)
    if msg:
        report["message"] = msg
    return report


def _missing_arg(func: str, arg: str, project: str | None) -> str | None:
    """The refusal text when this editor's plugin copy cannot receive `arg`, else None.

    None means "send it": either the parameter is there, or the contract could not be read at
    all and the CLI must not block a call that would otherwise have worked.
    """
    live = _live_contract(project)
    if live is None:
        return None
    args = live.get(func)
    if args is None or arg in args:
        return None     # verb missing entirely -> let the 404 path name it; it says more
    return _contract.arg_skew_message(func, arg, project)


def _rc_require(func: str, params: dict, project: str | None, needs: str):
    """_rc_call for a verb an older plugin copy may not have: turns RemoteControl's raw 404
    into the version-skew refusal above. Every other failure passes through untouched."""
    try:
        return _rc_call(func, params, project)
    except AgentError as exc:
        if _is_missing_verb(exc):
            raise _skew_error(func, project, needs) from None
        raise


def _rc_json(func: str, params: dict, project: str | None = None,
             *, needs: str | None = None) -> dict:
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
            prov = body.get("provenance")
            s.add_screenshot(body["path"], body.get("caption", ""), provenance=prov,
                             source={"kind": "editor", "project": prov} if prov else None)
    except Exception:
        pass


def _status(args) -> int:
    """Preflight. Also the ONE place the CLI/plugin skew check runs by default, because it is
    the call every documented workflow starts with -- so a project whose plugin copy is behind
    finds out here, in a verb that is meant to diagnose, rather than three steps later inside a
    verb that silently did the wrong thing.

    `plugin_version` is a BUILD STAMP, not a capability signal: it is a hardcoded literal in the
    plugin that has never been bumped. `contract` is the real answer -- see contract.py.
    """
    t0 = time.monotonic()
    out = {"ok": True, "rc_reachable": False, "plugin_version": None, "rc_port": _rc_port_for(args.project)}
    try:
        ver = _rc_call("GetPluginVersion", {}, args.project)
        out["rc_reachable"] = True
        out["plugin_version"] = ver
    except AgentError as exc:
        out["ok"] = False
        out["error"] = str(exc)
    if out["rc_reachable"]:
        out["contract"] = _contract_report(args.project)
    _capture("status", {}, out, int((time.monotonic() - t0) * 1000))
    _emit(out)
    return 0 if out["rc_reachable"] else 1


def _coerce(v: str):
    """Coerce a key=value string value to bool/int/float, else leave as string.

    A GUESS, and only correct by luck. `uap rc` sees shell text with no types, so this used to
    be the only rule -- and it silently broke a whole CLASS of call: RemoteControl binds the
    argument struct BY JSON TYPE, so a value guessed into a number cannot bind to an FString
    parameter. RemoteControl does not refuse; it leaves that field at its zero-initialised
    default and the plugin runs its "argument omitted" path, reporting success
    (`rc InjectGamepad ... SlateUser=9` -> plugin reads "" -> auto route; ClickUp 86ak7kcm7).
    Every FString parameter with a numeric-looking value has the same hole.

    So this is now the FALLBACK, used only where the declared type is unknown, and the caller
    is told which values were guessed. `_coerce_declared` below is the primary path.
    """
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


def _coerce_declared(v: str, type_name: str | None):
    """Encode a key=value string as the parameter's DECLARED type actually needs.

    Returns (value, guessed). `guessed` is True when the declared type gives no answer (enum,
    struct, or no contract at all) and the heuristic had to run -- reported, never hidden.
    """
    kind = _contract.kind_of(type_name)
    if kind == "string":
        return v, False                      # "9" stays "9"; this is the whole bug
    if kind == "bool":
        return v.strip().lower() not in ("false", "0", "no", ""), False
    if kind == "int":
        try:
            return int(v, 0), False
        except ValueError:
            return _coerce(v), True
    if kind == "float":
        try:
            return float(v), False
        except ValueError:
            return _coerce(v), True
    # Enum / struct / no contract. The risk in this whole class is text being silently
    # RETYPED, so flag it only when that actually happened: `Button=FaceBottom` goes out as
    # the caller's own string and carries no risk, while `Button=3` became a number on a guess
    # and the caller deserves to know.
    val = _coerce(v)
    return val, not isinstance(val, str)


def _parse_rc_params(tokens: list[str], types: dict | None = None) -> tuple[dict, list[str]]:
    """Parse rc params from CLI tokens. Returns (params, guessed_keys).

    Two forms (the first dodges Windows shell quoting, which mangles embedded
    double-quotes in a JSON string arg):
      - key=value pairs:  Command=stat\\ fps   KeyName=E   bPressed=true
      - a single JSON object token:  '{"Command":"stat fps"}'  (when the caller
        can pass quotes through intact). This form already carries real JSON types, so it is
        passed through untouched and is the escape hatch when no contract can be read.

    `types` is {arg_name: declared_type} for the target UFUNCTION, read live from the editor.
    None means no contract could be read, so every value falls back to the heuristic -- and a
    key lands in `guessed` when that heuristic actually RETYPED the caller's text, which is the
    only way this can go silently wrong.
    """
    if not tokens:
        return {}, []
    if len(tokens) == 1 and tokens[0].lstrip().startswith("{"):
        return json.loads(tokens[0]), []
    out: dict = {}
    guessed: list[str] = []
    for tok in tokens:
        if "=" not in tok:
            raise ValueError(f"param must be key=value or a single JSON object, got: {tok!r}")
        k, v = tok.split("=", 1)
        out[k], was_guess = _coerce_declared(v, types.get(k) if types else None)
        if was_guess:
            guessed.append(k)
    return out, guessed


# UFUNCTIONs whose declared return type cannot survive RemoteControl's preset-call route:
# it serializes the return through a filter that only admits the function's own out/return
# params, which empties any nested struct. The plugin exposes a JSON-string twin; `uap rc`
# transparently uses it so the documented incantation keeps working and keeps its data.
_RC_JSON_TWINS = {"ListTestHelpers": "ListTestHelpersJson"}


def _rc(args) -> int:
    """The raw UFUNCTION passthrough -- and the one verb whose parameters arrive as untyped
    shell text, so it is where a mistyped argument gets silently dropped by RemoteControl. It
    now asks the editor for the target function's DECLARED parameter types first and encodes
    against them, instead of guessing from the text (see `_coerce` / ClickUp 86ak7kcm7)."""
    func = args.rc_func
    types = None
    if any("=" in t for t in args.params):
        live = _live_contract(args.project)
        if live is not None:
            types = live.get(func)
    try:
        params, guessed = _parse_rc_params(args.params, types)
    except (ValueError, json.JSONDecodeError) as exc:
        _emit({"ok": False, "error": f"bad rc params: {exc}"})
        return 2

    # A parameter the function does not declare is dropped by RemoteControl exactly as silently
    # as a mistyped one -- so a typo'd key today "succeeds" having sent nothing. Refuse instead,
    # and only when the contract was actually read (never on a guess).
    if types is not None:
        unknown = [k for k in params if k not in types]
        if unknown:
            _emit({"ok": False, "error": (
                f"`{func}` has no parameter(s) {', '.join(sorted(unknown))}. RemoteControl "
                f"would accept the call and DROP them, so it would report success having sent "
                f"nothing. Declared parameters: "
                f"{', '.join(f'{k}: {v}' for k, v in types.items()) or '(none)'}.")})
            return 2

    t0 = time.monotonic()
    body: dict = {"ok": True}
    twin = _RC_JSON_TWINS.get(func) if not params else None
    try:
        if twin:
            body["result"] = {"helpers": _helpers_payload(args.project)}
            body["via"] = twin
        else:
            body["result"] = _rc_call(func, params, args.project)
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    if guessed and body.get("ok"):
        # Say so. A guessed encoding is the failure mode that produces an `ok` with the wrong
        # behaviour, and the reader has no other way to know it happened.
        body["coercion"] = {
            "guessed": sorted(guessed),
            "note": ("no declared type for these; their JSON type was inferred from the text. "
                     "If the plugin behaved as though the value were absent, pass the whole "
                     "parameter set as one JSON object instead: "
                     "uap rc " + func + " '{\"Name\": \"value\"}'"),
        }
    _capture(f"rc:{args.rc_func}", {"params": params}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _exec(args) -> int:
    code = args.code
    t0 = time.monotonic()
    body: dict = {"ok": True}
    client = PythonRemoteExecClient(node_project_substr=args.project,
                                    node_instance=getattr(args, "instance", None))
    try:
        res = client.exec_python(code)
        body["result"] = res.get("result")
        body["output"] = [o.get("output", "") for o in (res.get("output") or [])]
        body["ok"] = bool(res.get("success", True))
        if not body["ok"]:
            body["error"] = "exec returned success=false; see output"
    except AgentError as exc:
        body = _err(exc)
    # Always say WHICH process answered. The failure this exists for was an exec that landed on
    # a `-game` standalone client of the same project and answered `None` for every editor
    # subsystem; the reader had no way to tell that from a real editor answering `None`.
    if _target_of(client) is not None:
        body["target"] = _target_of(client)
    _capture("exec", {"code": code[:200]}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _target_of(client: PythonRemoteExecClient) -> dict | None:
    """The {pid, role, project} of the node an exec ran on, trimmed for the response."""
    info = getattr(client, "last_target", None)
    if not info:
        return None
    return {"pid": info.get("pid"), "role": info.get("role"), "project": info.get("project")}


def _instances(args) -> int:
    """Every Unreal process answering Python remote-exec discovery, and which one a bare
    command would select. Run this when an answer looks like it came from the wrong place."""
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        client = PythonRemoteExecClient(node_project_substr=args.project)
        nodes = client.list_nodes()
        rows = []
        for info in nodes:
            rows.append({
                "pid": info.get("pid"),
                "role": info.get("role"),
                "project": info.get("project"),
                "cmdline": info.get("cmdline"),
                "selected_by_default": (client._matches_project(info)
                                        and info.get("role") == "editor"),
                "describe": PythonRemoteExecClient.describe_node(info),
            })
        body["instances"] = rows
        body["project_filter"] = args.project or None
        if not any(r["selected_by_default"] for r in rows):
            body["note"] = ("no EDITOR matches the project filter, so editor verbs will refuse "
                            "rather than answer from one of these. Use --instance pid:<n> or "
                            "--instance <cmdline-substring> to target one deliberately.")
    except AgentError as exc:
        body = _err(exc)
    _capture("instances", {"project": args.project}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _exec_file(args) -> int:
    with open(args.path, encoding="utf-8") as f:
        code = f.read()
    return _exec(argparse.Namespace(code=code, project=args.project,
                                    instance=getattr(args, "instance", None)))


def _pie_start(mode: str, project: str | None) -> dict:
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
# --- how often to ask "has it happened yet?" ----------------------------------------------
# Both PIE waits polled on a flat 0.5s. A start or a stop that the engine finishes in 0.2s was
# still reported ~0.5s later, and an agent running start/stop around each check paid that twice
# per iteration for nothing (ClickUp 17tm466ft35). The wait itself is correct and stays -- `pie
# stop` confirming teardown rather than acking a queued request is NOT negotiable, and none of
# this shortens the engine's actual work. It only stops rounding it up.
#
# So: ask often while the answer is plausibly about to change, then back off. RC is a separate
# channel from the game-thread Python exec (which must NOT be tight-looped during a transition --
# it re-enters the task graph and hard-crashes the editor), and each poll is a synchronous
# request/response, so a faster interval cannot pile up requests.
_PIE_POLL_FAST_SECONDS = 0.1
_PIE_POLL_FAST_WINDOW = 3.0
_PIE_POLL_SLOW_SECONDS = 0.5


def _pie_poll_interval(elapsed: float) -> float:
    return _PIE_POLL_FAST_SECONDS if elapsed < _PIE_POLL_FAST_WINDOW else _PIE_POLL_SLOW_SECONDS


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


def _pie_start_timeout() -> float:
    """Seconds `uap pie start` waits for the play world to go LIVE before it fails.

    A start normally takes 1-5 seconds. The bound is generous because a cold map load is the
    slow case, not because the call is expected to be long: `waited_seconds` in the result is
    what tells the caller which of the two they got.
    $UAP_PIE_START_TIMEOUT overrides.
    """
    return _float_env("UAP_PIE_START_TIMEOUT", 60.0)


def _pie_stop_settle() -> float:
    """DEGRADED path only (see _pie_stop): how long IsInPIE must read false CONTINUOUSLY before
    a stop is believed. IsInPIE cannot see a queued start, so a single false reading is exactly
    the false signal we are fixing; the window is a heuristic, not a proof, and is reported as
    such. $UAP_PIE_STOP_SETTLE overrides."""
    return _float_env("UAP_PIE_STOP_SETTLE", 5.0)


def _pie_in_progress(project: str | None, *, degraded: bool) -> bool:
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


def _pie_stop(project: str | None, timeout: float) -> dict:
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
    clear_since: float | None = None
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
        time.sleep(_pie_poll_interval(now - t0))
    out["waited_seconds"] = round(time.monotonic() - t0, 2)
    if restops:
        out["restops"] = restops
    return out


# --- PIE start: LIVE, not merely queued ---------------------------------------------------
# One implementation of "is PIE live", shared by `pie wait` and by the blocking `pie start`.
# Two notions of it would drift, and the whole class of bug here is a caller acting on a
# weaker signal than the one it thinks it has.


def _pie_wait_for_live(project: str | None, seconds: float) -> dict:
    """Block until the play world is LIVE, bounded.

    `IsInPIE` (a live play world), NOT `IsPIEInProgress` (live OR queued): this asks whether the
    world EXISTS, and a queued-but-not-yet-created session is precisely what it must keep waiting
    through. This is the one place the narrower verb is the right question.

    It polls over RemoteControl and never through `uap exec`. Python remote-exec runs on the GAME
    THREAD, and calling it repeatedly during a PIE transition re-enters the engine task graph and
    HARD-CRASHES the editor (docs/known-issues.md #12). RC is a separate channel; polling it here
    is what makes "do not tight-loop exec during a transition" survivable advice rather than a
    reason to invent a waiting strategy of your own.
    """
    t0 = time.monotonic()
    deadline = t0 + max(0.0, seconds)
    playing = bool(_rc_call("IsInPIE", {}, project))
    while not playing and time.monotonic() < deadline:
        time.sleep(_pie_poll_interval(time.monotonic() - t0))
        playing = bool(_rc_call("IsInPIE", {}, project))
    return {"playing": playing, "waited_seconds": round(time.monotonic() - t0, 2)}


def _pie_start_timed_out(seconds: float) -> str:
    return (
        f"PIE was requested but the play world was not live {round(seconds, 1)}s later -- IsInPIE "
        "still reads false, so nothing is playing yet. A start is QUEUED work, so it may still "
        "come up AFTER this returns: the editor is NOT idle, NOT yours to hand over, and NOT safe "
        "to end a turn on. Run `uap pie stop` (it cancels a queued start and confirms the "
        "teardown), then retry -- or raise the bound with `--timeout <sec>` / "
        "$UAP_PIE_START_TIMEOUT if this map is simply slow to load. A normal start is 1-5s."
    )


def _pie(args) -> int:
    """Start/stop PIE via the plugin's version-correct RC verbs, so agents never touch the raw,
    version-fragile engine subsystem.

    `start` and `stop` are now the SAME shape: both block until the state they name is real, and
    both fail loudly if it never arrives. `start` used to return the instant the session was
    QUEUED, labelled `queued: true, confirmed: false, next: uap pie wait <seconds>`.

    That default cost three incidents in one day (2026-08-28), the worst of which left a Project
    Broken Wings aircraft flying unattended into a building. The mechanism was not that agents
    missed the hint -- the hint shipped that morning and the incidents happened after it. It is
    that the fields BESIDE the hint implied a SHAPE: `queued` implies a queue you come back to and
    `confirmed: false` implies confirmation arriving on its own schedule, so an agent did the
    correct thing for async work -- registered a watcher and ended its turn -- for an operation
    that takes 1-5 SECONDS. The wrong behaviour followed logically from an accurate response.

    So the async vocabulary is gone from the default path, not merely supplemented: a blocking
    start answers `playing: true` + `waited_seconds`, exactly as `stop` answers `stopped: true` +
    `waited_seconds`. `queued`/`confirmed` survive only where they are TRUE -- under `--no-wait`,
    and on the timeout path, where a ticket really is outstanding.

    `--no-wait` keeps fire-and-forget for a caller that genuinely wants to work while PIE comes
    up. It is opt-in because the safe path has to be the DEFAULT path; a hint is not a default.
    """
    sub = args.pie_cmd
    t0 = time.monotonic()
    body: dict = {"ok": True}
    try:
        if sub == "start":
            body.update(_pie_start(getattr(args, "mode", "flat") or "flat", args.project))
            if body.get("ok"):
                timeout = getattr(args, "timeout", None)
                timeout = _pie_start_timeout() if timeout is None else timeout
                if getattr(args, "no_wait", False):
                    # Opt-in fire-and-forget: here the async vocabulary is the truth. It is
                    # confined to this path on purpose -- see the docstring.
                    body["queued"] = True
                    body["confirmed"] = False
                    body["next"] = "uap pie wait <seconds>   # blocks until the game world is live"
                    body["warning"] = (
                        "--no-wait: this is an ack of a QUEUED start, not a live world. PIE may "
                        "come up AFTER this returns. Do not read game state, capture a frame, or "
                        "END YOUR TURN on it -- run `uap pie wait <seconds>` first, and stop PIE "
                        "(`uap pie stop`) before you finish."
                    )
                else:
                    waited = _pie_wait_for_live(args.project, timeout)
                    body["playing"] = waited["playing"]
                    body["waited_seconds"] = waited["waited_seconds"]
                    if not waited["playing"]:
                        # A start we could not confirm IS still queued -- say so here, where it is
                        # true, and never as a cheerful ok.
                        body["ok"] = False
                        body["queued"] = True
                        body["confirmed"] = False
                        body["error"] = _pie_start_timed_out(timeout)
        elif sub == "stop":
            timeout = getattr(args, "timeout", None)
            body.update(_pie_stop(args.project,
                                  _pie_stop_timeout() if timeout is None else timeout))
        elif sub == "wait":
            # The SAME implementation `pie start` blocks on -- see _pie_wait_for_live. `wait`
            # remains for the `--no-wait` path and for waiting on a session someone else started.
            waited = _pie_wait_for_live(args.project, args.seconds)
            body.update(waited)
            body["ok"] = waited["playing"]
            if not waited["playing"]:
                body["error"] = (
                    f"PIE was not live {round(args.seconds, 1)}s after the wait began -- IsInPIE "
                    "still reads false. The editor is NOT playing, and a queued start may still "
                    "come up later, so do not read game state, capture a frame, or end your turn "
                    "here. Run `uap pie stop` to clear any queued start, then `uap pie start`, "
                    "which now waits for you. A normal start is 1-5s."
                )
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


def _hold_ledger_path(project) -> pathlib.Path:
    """Where the CLI records its own outstanding holds, per project.

    A hold outlives the process that started it, and every CLI call is a fresh process, so the
    only way one call can know about another's hold is on disk. Lives beside the leases, which
    solve the same cross-process problem for the editor itself.
    """
    return _coord.hold_ledger_path(project)


def _hold_ledger_read(project) -> dict:
    try:
        return json.loads(_hold_ledger_path(project).read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _hold_ledger_note(project, key: str, seconds: float, agent: str | None) -> None:
    led = _hold_ledger_read(project)
    led[str(key).lower()] = {"key": key, "ends_at": time.time() + float(seconds),
                             "agent": agent or None}
    try:
        p = _hold_ledger_path(project)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(led), encoding="utf-8")
    except OSError:
        pass        # the ledger is an advisory optimisation; losing it must not fail a hold


def _hold_ledger_clear(project, key: str = "") -> None:
    led = _hold_ledger_read(project)
    if key:
        led.pop(str(key).lower(), None)
    else:
        led = {}
    try:
        _hold_ledger_path(project).write_text(json.dumps(led), encoding="utf-8")
    except OSError:
        pass


def _hold_conflict(key: str, project, *, allow: bool) -> dict | None:
    """Refuse a hold that would OVERLAP one already running, unless it was asked for.

    `hold` / `axis` return as soon as the plugin has latched the input -- deliberately, because
    the whole point is to read game state WHILE it is held, and the CLI round-trip is ~1s. What
    was NOT deliberate is that consecutive holds then overlap in silence: the plugin keeps a
    hold per FKey (`UAPFindOrAddHold`, AgentInput.cpp) and a second `hold` on a different key
    simply starts alongside the first. A caller reading the docs as "hold W for 3s, then hold A
    for 3s" gets 3 seconds of W+A, and the pawn keeps moving after the caller believes the hold
    ended. That contaminated a session -- a pawn drifted ~3500 cm off the plaza and the run was
    discarded -- and it looks like the pawn being odd, not like a tool fault.

    Re-issuing the SAME key is not a conflict: `UAPFindOrAddHold` finds the existing entry and
    pushes its EndRealTime out, which is a re-assert and is what `input hold` is for. Only a
    DIFFERENT key already held is the silent-overlap case.

    Two stages, so the common path costs NOTHING. The on-disk ledger says whether a hold could
    still be running; only if it might does this pay for one `GetHeldInput` to check engine
    ground truth, because the ledger can be stale in the safe-to-proceed direction (PIE stopped,
    a FlushPressedKeys, another agent's `input release`) and a refusal on a hold that is not
    really there would be its own false failure.

    Returns a refusal body, or None to proceed. A plugin too old to report held state (or an
    unreadable answer) is not a reason to refuse: proceed rather than invent a conflict.
    """
    if allow:
        return None
    now = time.time()
    suspects = [e for k, e in _hold_ledger_read(project).items()
                if k != str(key).lower() and float(e.get("ends_at") or 0) > now]
    if not suspects:
        return None
    try:
        state = _rc_json("GetHeldInput", {}, project, needs=_NEEDS_HOLD)
    except (AgentError, json.JSONDecodeError):
        return None
    others = [h for h in (state.get("held") or [])
              if str(h.get("key", "")).lower() != str(key).lower()
              and float(h.get("remaining_seconds") or 0) > 0]
    if not others:
        _hold_ledger_clear(project)     # the ledger was stale; do not keep refusing on it
        return None
    listed = ", ".join(f"{h.get('key')} ({float(h.get('remaining_seconds') or 0):.2f}s left, "
                       f"route {h.get('route')})" for h in others)
    return {
        "ok": False, "pressed": False, "held": others, "overlap_refused": True,
        "error": (f"another hold is still running, so this one would OVERLAP it: {listed}. "
                  f"`input hold`/`axis` return as soon as the input is latched -- the hold "
                  f"itself keeps running in-engine -- so a hold issued back-to-back with "
                  f"another one runs ON TOP of it and the pawn keeps moving after you think "
                  f"it stopped. Wait for it (`--wait` blocks for the duration, `uap input "
                  f"status` shows remaining_seconds), end it (`uap input release`), or say "
                  f"you meant them simultaneous with `--overlap`."),
    }


def _input(args) -> int:
    """Sustained input. A single injected event cannot drive locomotion: the CLI round-trip is
    ~1s and a latched key is silently dropped by any FlushPressedKeys, so the plugin re-asserts
    the input every frame in-engine for the requested duration. `axis` is the VR locomotion
    verb -- thumbsticks are analog axis FKeys, not buttons.

    Returning before the hold expires is deliberate (read state WHILE it is held), but it is no
    longer silent: the result carries `ends_in_seconds` / `ends_at_epoch` so the end is
    knowable, and a hold that would overlap one already running is REFUSED unless --overlap.
    See _hold_conflict."""
    sub = args.input_cmd
    t0 = time.monotonic()
    body: dict = {"ok": True, "action": sub}
    try:
        if sub in ("hold", "axis"):
            clash = _hold_conflict(args.key, args.project,
                                   allow=getattr(args, "overlap", False))
            if clash is not None:
                clash["action"] = sub
                _capture(f"input:{sub}", {"key": args.key}, clash,
                         int((time.monotonic() - t0) * 1000))
                _emit(clash)
                return 1
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
                # An older plugin copy HAS HoldAxis but without the SlateUser parameter, so
                # there is no 404 for `_rc_require` to catch: RemoteControl would take the call,
                # drop the argument, and the hold would go out the viewport route reporting ok
                # -- the silent discard of #26 all over again, one layer up. Check the live
                # contract first and refuse. Unreadable contract -> proceed as before.
                gap = _missing_arg("HoldAxis", "SlateUser", args.project)
                if gap:
                    _emit({"ok": False, "action": sub, "pressed": False, "error": gap})
                    return 1
                params["SlateUser"] = str(args.user)
            body.update(_rc_json("HoldAxis", params, args.project, needs=_NEEDS_HOLD))
        elif sub == "release":
            body.update(_rc_json("ReleaseHeldInput", {"KeyName": args.key or ""}, args.project,
                                 needs=_NEEDS_HOLD))
            _hold_ledger_clear(args.project, args.key or "")
        else:  # status
            body.update(_rc_json("GetHeldInput", {}, args.project, needs=_NEEDS_HOLD))

        # Non-blocking by default: the hold runs IN-ENGINE, so the point is to sample game
        # state while it is still held. --wait blocks until it expires instead.
        #
        # Either way the caller is told WHEN it ends. Before this the result said only
        # `seconds`, which reads as "it took that long" rather than "it is still going", and
        # nothing in the answer was awaitable -- so back-to-back holds overlapped and the
        # overlap was invisible until the pawn ended up somewhere it should not be.
        if body.get("ok") and sub in ("hold", "axis"):
            if getattr(args, "wait", False):
                time.sleep(args.seconds)
                body["waited"] = True
                body["ends_in_seconds"] = 0.0
                _hold_ledger_clear(args.project, args.key)
            else:
                _hold_ledger_note(args.project, args.key, args.seconds,
                                  getattr(args, "agent", None))
                body["ends_in_seconds"] = round(float(args.seconds), 3)
                body["ends_at_epoch"] = round(time.time() + float(args.seconds), 3)
                body["note"] = (f"STILL HELD for ~{float(args.seconds):.2f}s after this call "
                                f"returned -- that is the point (read state while it holds). "
                                f"Do not issue another hold until it ends: use --wait to block, "
                                f"`uap input status` to poll remaining_seconds, or `uap input "
                                f"release` to end it now.")
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture(f"input:{sub}", {k: v for k, v in vars(args).items()
                              if k in ("key", "value", "seconds", "user")},
             body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    # .get: the body is merged from the plugin's envelope, so never assume the key is there.
    return 0 if body.get("ok") else 1


_NEEDS_MOUSE = ("positioning the mouse INSIDE a captured PIE viewport; the OS cursor cannot be "
                "moved while PIE holds the mouse, so this has to happen engine-side")

_MOUSE_BUTTONS = {"left": "Left", "right": "Right", "middle": "Middle",
                  "xbutton1": "XButton1", "xbutton2": "XButton2"}


def _input_mouse(args) -> int:
    """Position and click the mouse inside a viewport that holds mouse capture.

    Why this is not just SetCursorPos: while the PIE viewport has the mouse, the pointer cannot
    be moved AT ALL. Win32 SetCursorPos is inert, and so is FSlateApplication::SetCursorPos --
    measured live, SetCursorPos(1754, 989) left GetCursorPos() reading 0,0 while the call
    reported success. The engine cannot cache a different position either: FSlateUser reads the
    cursor straight back out of the platform cursor.

    So the plugin does not move the cursor. Slate routes a pointer event by the position carried
    ON THE EVENT, and that is what it stamps -- an "agent cursor". The old chain
    (InjectMouseMove + InjectMouseButton) took its click position from GetCursorPos(), so every
    injected click landed at 0,0 on nothing while reporting ok:true. ClickUp 17tm466fbyj.

    x/y are ABSOLUTE SCREEN PIXELS -- the same space `read-ui` reports, so read-ui output feeds
    straight in. The result carries `hit`: the Slate widgets actually found under the point. An
    empty `hit` on a click is reported as a FAILURE, not a success, because clicking nothing is
    exactly the outcome this verb exists to stop reporting as ok."""
    sub = args.mouse_cmd
    t0 = time.monotonic()
    body: dict = {"ok": True, "action": f"mouse {sub}"}
    try:
        if sub == "move":
            body.update(_rc_json("SetMousePosition", {"X": args.x, "Y": args.y},
                                 args.project, needs=_NEEDS_MOUSE))
        else:  # click
            if (args.x is None) != (args.y is None):
                _emit({"ok": False, "action": "mouse click", "clicked": False,
                       "error": "give BOTH x and y or neither"})
                return 1
            # Strings on the wire, empty for "omitted". RemoteControl zero-initialises the
            # argument struct, so an omitted float would arrive as 0.0 -- a valid position, and
            # the top-left corner: the exact silent miss this verb replaces.
            params = {"Button": _MOUSE_BUTTONS[args.button],
                      "X": "" if args.x is None else str(args.x),
                      "Y": "" if args.y is None else str(args.y)}
            body.update(_rc_json("ClickMouse", params, args.project, needs=_NEEDS_MOUSE))
            if body.get("ok") and not str(body.get("hit") or ""):
                # The plugin delivered the events and says so; what it did NOT do is hit a
                # widget. Reporting that as ok:true is the bug, not the click.
                body["ok"] = False
                body.setdefault("error", body.get("warning")
                                or "the click landed on no Slate widget")
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture(f"input:mouse:{sub}", {k: v for k, v in vars(args).items()
                                    if k in ("x", "y", "button")},
             body, int((time.monotonic() - t0) * 1000))
    _emit(body)
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
    for a, b in itertools.pairwise(vecs):
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
            body.update(_throttle.sampler_annotation(body["stats"].get("hz")))
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
        body.update(_throttle.sampler_annotation(body["stats"].get("hz")))
        if args.summary:
            body.pop("samples", None)
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture("sample:read", {}, body, int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


#: A cursor that is AHEAD of the capture's head did not come from this capture. The ring is
#: rebuilt from scratch on every editor start, so a cursor taken before a restart (or from a
#: different instance) points past the end of the current one -- and `log since` then answers
#: `count: 0, ok: true`, which is the same false "clean log" shape by another route. The editor
#: is shared and any agent may rebuild it via Restart-Editor.ps1 mid-task, so this is not exotic.
_LOG_STALE_CURSOR_NOTE = (
    "STALE CURSOR: {after} is ahead of this capture's head ({head}), so it was not taken from "
    "the editor process now running -- the log ring is rebuilt on every editor start, and the "
    "editor is shared, so a restart by another agent resets it. This read covered NONE of the "
    "window you meant; count=0 says nothing about it. Take a fresh `uap log cursor`, or read "
    "Saved/Logs/<Project>.log (and its SchoolsOut-backup-*.log for the pre-restart process)."
)

#: What a caller must be told when the window it asked for no longer exists in full. The
#: point of the wording is that `count: 0` is NOT evidence of a clean log -- see _log_dropped.
_LOG_TRUNCATED_NOTE = (
    "log window TRUNCATED: the plugin's capture is a fixed-size ring buffer and the oldest "
    "{dropped} record(s) of the range you asked for had already been evicted when this read "
    "ran, so they were NOT searched. A low count -- and count=0 in particular -- is UNPROVEN "
    "here, not a clean log. Either re-read a narrower window ending sooner after the action, "
    "raise LogBufferCapacity under [/Script/UnrealAgentPlayer.UAPAgentSettings] in the "
    "project's Config/DefaultEditor.ini and restart the editor, or read "
    "Saved/Logs/<Project>.log for the evicted span."
)

#: `--lines` used to bound the PLUGIN read, and the plugin fills its quota from the OLDEST
#: surviving record forward -- so the cap threw away the NEWEST end of the window, which is
#: exactly the end holding the records the agent had just caused. It is now a display cap
#: applied AFTER the whole window has been scanned and filtered, so `count` is complete and
#: only the listing is shortened. Say which end was dropped, because that is the part that
#: silently produced wrong answers.
_LOG_OMITTED_NOTE = (
    "listing shortened by --lines: {total} record(s) matched and the {listed} {kept_end} "
    "are listed; {omitted} {dropped_end} one(s) are NOT shown. `count` above is the COMPLETE "
    "match count for the window -- the whole window was scanned and filtered before this cap "
    "was applied, so nothing was missed by the search, only by the listing. Raise --lines to "
    "see the rest."
)

#: A scan that hit its own record bound examined only part of the window, so `count` is a
#: floor rather than a total. Distinct from ring eviction: the records exist, we stopped.
_LOG_SCAN_BOUND_NOTE = (
    "scan BOUND at {scanned} record(s) before reaching the head of the log, so the window was "
    "only partly examined and `count` is a FLOOR, not a total. Re-read a narrower window, or "
    "raise --max-scan."
)

#: Records pulled per RC round-trip while paging a window. The plugin's ReadSince fills its
#: MaxLines quota from the oldest surviving record forward, so one call can never return the
#: newest end of a long window -- the window has to be paged. 4096 was the ring's own original
#: capacity and is a comfortable single payload.
_LOG_SCAN_CHUNK = 4096

#: Hard bound on a single `log since` scan. The ring cannot hold more than its capacity, so in
#: practice this is never reached; it exists so a runaway cannot page forever.
_LOG_SCAN_MAX = 200000


def _log_scan(after: int, category: str, verbosity: str, project,
              chunk: int, max_records: int) -> tuple[list, bool]:
    """Every retained record past `after` that passes the plugin-side filters, oldest first.

    One `GetLogsSince` cannot answer this. `FAgentLogCapture::ReadSince`
    (Plugins/UnrealAgentPlayer/Source/UnrealAgentPlayerRuntime/Private/AgentLogCapture.cpp)
    walks the ring from the OLDEST surviving record forward and stops the moment it has
    collected `MaxLines` matches -- so a cap smaller than the window silently returns the
    OLDEST slice of it and drops the newest. Measured over a 667-record window: `--lines 200`
    answered 1870..2131, `--lines 500` answered 1870..2527, and a `--grep` whose 10 matches
    all sat past 2131 answered `count: 0`. The window is therefore paged here, and the caller's
    `--lines` becomes a display cap applied afterwards.

    Termination is exact, not a heuristic: ReadSince scans the entire ring and only breaks
    early on a filled quota, so a page holding FEWER than `chunk` records proves the scan
    reached the newest record. Returns (records, scan_complete, resume_cursor) -- the resume
    cursor is the plugin's OWN reported cursor from the last page, which is what a follow-up
    `log since` should be given, not something re-derived from the records.
    """
    records: list = []
    cursor = after
    while True:
        raw = _rc_require("GetLogsSince",
                          {"AfterCursor": cursor, "MaxLines": chunk,
                           "CategoryFilter": category, "MinVerbosity": verbosity},
                          project, _NEEDS_LOG)
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
        page = parsed.get("lines") or []
        records.extend(page)
        reported = int(parsed.get("cursor") or cursor)
        if len(page) < chunk:
            return records, True, reported
        if reported <= cursor:
            # No forward progress -- a plugin that does not advance OutCursor would spin here.
            # Bail rather than loop, and report the scan as incomplete so the count is a floor.
            return records, False, reported
        cursor = reported
        if len(records) >= max_records:
            return records, False, reported


def _log_dropped(after: int, first_returned: int | None, project) -> tuple[int, int | None]:
    """How many records between `after` and the oldest SURVIVING record were evicted.

    Capture cursors are handed out by a single `NextCursor++` per log record, so they are
    contiguous: if the oldest retained record with cursor > `after` is K, then exactly
    K - after - 1 records were dropped. That is an exact count, not an estimate.

    `first_returned` is the lowest cursor the main read gave back. When it is already
    after + 1 the window is provably intact and this costs nothing; otherwise it takes ONE
    extra RC call, unfiltered and MaxLines=1, to find the true floor -- the main read cannot
    reveal it because its own verbosity/category filter also skips records.

    An empty probe means dropped=0 and is not a blind spot: eviction only ever removes the
    OLDEST records, so if anything at all had been logged past `after` the newest of it would
    still be there. No lines past the cursor therefore means nothing was logged past it.
    """
    if first_returned is not None and first_returned <= after + 1:
        return 0, first_returned
    probe = _rc_require("GetLogsSince",
                        {"AfterCursor": after, "MaxLines": 1,
                         "CategoryFilter": "", "MinVerbosity": "VeryVerbose"},
                        project, _NEEDS_LOG)
    parsed = json.loads(probe) if isinstance(probe, str) else (probe or {})
    probe_lines = parsed.get("lines") or []
    if not probe_lines:
        return 0, None
    oldest = int(probe_lines[0].get("cursor", after + 1))
    return max(0, oldest - after - 1), oldest


def _log(args) -> int:
    """Read the editor's log through the plugin's in-process capture, so log evidence lands in
    the report instead of being tailed out-of-band with shell tools -- and so it targets the
    SAME editor as every other verb (this machine runs two).

    Note: this is the plugin's ring buffer (LogBufferCapacity, 4096 records by default),
    populated from subsystem init onward. It is not Saved/Logs/<Project>.log; for a
    whole-session history read that file directly.

    The ring is why a sweep over a long window used to lie. A PIE session logs far more than
    4096 records, so `log since <old cursor> --verbosity Error` searched only the surviving
    tail and answered `count: 0, ok: true` -- indistinguishable from a clean log, while the
    startup errors the caller was told to sweep for had been overwritten hours earlier. Every
    read now reports `oldest_cursor` / `dropped`, and sets `truncated` + `warning` when the
    requested window is not retained in full, so silence is provable rather than assumed.

    `--lines` is a DISPLAY cap, not a read bound. It used to be passed straight through as
    MaxLines, and the plugin fills that quota from the oldest surviving record forward, so the
    cap silently discarded the newest end of the window -- the end holding whatever the agent
    had just caused. The window is now paged in full (see _log_scan), filtered, and only then
    shortened for display, so `count` is the complete match count and `--grep` can no longer
    miss a match that a small `--lines` had thrown away."""
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
            # `log tail 400` and `log tail --lines 400` are the same request.
            count = getattr(args, "count", None)
            limit = count if count is not None else args.lines
            if sub == "tail":
                current = int(_rc_require("GetLogCursor", {}, args.project, _NEEDS_LOG) or 0)
                after = max(0, current - limit)
            max_scan = max(1, getattr(args, "max_scan", None) or _LOG_SCAN_MAX)
            # `tail N` asks for a window exactly N records wide, so one page of N covers it and
            # the general chunk would only make the payload bigger than the request.
            # limit + 1 for tail so a window that is full to the brim still proves itself
            # exhausted in ONE call (a page of exactly `chunk` cannot).
            chunk = min(max(limit, 1) + 1, _LOG_SCAN_CHUNK) if sub == "tail" \
                else max(_LOG_SCAN_CHUNK, limit)
            scanned, scan_complete, resume = _log_scan(after, args.category, args.verbosity,
                                                       args.project, chunk, max_scan)
            cursors = [int(ln.get("cursor", 0)) for ln in scanned
                       if ln.get("cursor") is not None]
            lines = scanned
            if args.grep:
                try:
                    rx = re.compile(args.grep, re.IGNORECASE)
                except re.error as exc:
                    _emit({"ok": False, "error": f"bad --grep regex: {exc}"})
                    return 2
                # Match the record, not just its message text. `--grep "Error|Warning"` is the
                # documented broad sweep and it used to match only `message`, so a record whose
                # VERBOSITY is Error but whose text does not contain the word was missed: over
                # one real window, 15 of 19 Error records were invisible to it. Verbosity and
                # category are part of what an agent means when it greps for "Error" or for
                # "LogJanitorAI", so they are part of what is searched. Over-matching a prose
                # "error" is harmless in a sweep; under-matching a real one is the whole defect.
                lines = [ln for ln in lines
                         if rx.search(f"{ln.get('verbosity', '')} {ln.get('category', '')}: "
                                      f"{ln.get('message', '')}")]
            total = len(lines)
            # Keep the NEWEST by default. An agent asking "what happened since cursor X" means
            # the records its own action produced, which are the most recent ones; the old
            # behaviour handed back the oldest slice, which is the opposite.
            keep_end = getattr(args, "keep", "newest") or "newest"
            listed = lines if total <= limit else (
                lines[-limit:] if keep_end == "newest" else lines[:limit])
            warnings: list[str] = []
            body["cursor"] = resume
            body["count"] = total
            body["listed"] = len(listed)
            body["lines"] = listed
            body["scanned"] = len(scanned)
            if total > len(listed):
                body["omitted"] = total - len(listed)
                body["listed_end"] = keep_end
                warnings.append(_LOG_OMITTED_NOTE.format(
                    total=total, listed=len(listed), omitted=total - len(listed),
                    kept_end="newest" if keep_end == "newest" else "oldest",
                    dropped_end="oldest" if keep_end == "newest" else "newest"))
            if not scan_complete:
                body["truncated"] = True
                body["scan_incomplete"] = True
                warnings.append(_LOG_SCAN_BOUND_NOTE.format(scanned=len(scanned)))
            dropped, oldest = _log_dropped(after, min(cursors) if cursors else None,
                                           args.project)
            body["oldest_cursor"] = oldest
            body["dropped"] = dropped
            if dropped:
                body["truncated"] = True
                warnings.append(_LOG_TRUNCATED_NOTE.format(dropped=dropped))
            elif oldest is None and not scanned:
                # Nothing at all past the cursor. Honest when the cursor is at the head, a lie
                # when it is past it -- so only here, where it can matter, pay for the head.
                head = int(_rc_require("GetLogCursor", {}, args.project, _NEEDS_LOG) or 0)
                body["head_cursor"] = head
                if after > head:
                    body["stale_cursor"] = True
                    warnings.append(_LOG_STALE_CURSOR_NOTE.format(after=after, head=head))
            if warnings:
                body["warning"] = "\n\n".join(warnings)
    except (AgentError, json.JSONDecodeError) as exc:
        body = _err(exc)
    _capture(f"log:{sub}", {"grep": getattr(args, "grep", None)}, body,
             int((time.monotonic() - t0) * 1000))
    _emit(body)
    return 0 if body["ok"] else 1


def _helpers_payload(project: str | None) -> list:
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


def _find_clickable(elements: list, label: str) -> dict | None:
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
            # ClickMouse stamps the position onto the pointer events. The old chain below took
            # its click position from the OS cursor, which cannot be moved while the PIE
            # viewport holds the mouse -- so it clicked 0,0 and reported ok (ClickUp
            # 17tm466fbyj). Degrade to it only on a plugin too old to have the new verb, and
            # SAY SO, because on that path a success is not evidence of a click.
            try:
                res = _rc_json("ClickMouse", {"Button": "Left", "X": str(x), "Y": str(y)},
                               args.project, needs=_NEEDS_MOUSE)
                body.update({k: v for k, v in res.items() if k != "ok"})
                if not str(res.get("hit") or ""):
                    body["ok"] = False
                    body.setdefault("error", res.get("warning")
                                    or "the click landed on no Slate widget")
            except AgentError as exc:
                if not _is_missing_verb(exc) and "sync and rebuild" not in str(exc):
                    raise
                _rc_call("InjectMouseMove", {"X": x, "Y": y, "bAbsolute": True}, args.project)
                _rc_call("InjectMouseButton", {"Button": "Left", "bPressed": True}, args.project)
                _rc_call("InjectMouseButton", {"Button": "Left", "bPressed": False}, args.project)
                body["degraded"] = (
                    "this editor's plugin has no ClickMouse, so the click went out on the legacy "
                    "chain, which takes its position from the OS cursor. If that viewport holds "
                    "mouse capture the cursor cannot be moved and the click landed at 0,0 -- "
                    "ok here is NOT evidence of a click. Rebuild the plugin for this project.")
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


_MAX_FRAMES = 60


def _window_frame_paths(file: str, frames: int) -> list[str]:
    """`shot.png` for one frame; `shot_f01.png`..`shot_fNN.png` for a burst."""
    if frames <= 1:
        return [file]
    stem, ext = os.path.splitext(file)
    ext = ext or ".png"
    width = max(2, len(str(frames)))
    return [f"{stem}_f{i:0{width}d}{ext}" for i in range(1, frames + 1)]


def _report_for_attach(agent: str | None):
    """The active report, unless it belongs to a DIFFERENT agent (then attaching would put
    this agent's frames in a stranger's report). Returns (session|None, refusal|None)."""
    s = _load_active()
    if s is None:
        return None, None
    held = ((sess.get_active_owner() or {}).get("agent") or "").strip()
    mine = (agent or os.environ.get("UAP_AGENT_ID") or "").strip()
    # Stricter than the editor path on purpose: a tokenless capture does NOT drop frames into a
    # report someone else owns. Two-client tests are exactly when several agents are working.
    if held and held != mine:
        return None, (f"not attached: the active report belongs to agent {held!r}, not "
                      f"{mine or '(no --agent token)'!r}. Pass --agent <your-token> if it is "
                      f"yours; the image files are still written.")
    return s, None


def _window_screenshot(args) -> int:
    """`uap screenshot <file> --window <sel>`: capture a standalone game client's window from
    the OS side (PrintWindow), stamp its source, refuse a blank frame, attach to the report.
    Touches no editor -- see window_capture.py for why this exists."""
    from unreal_agent_player import window_capture as wc

    t0 = time.monotonic()
    frames = getattr(args, "frames", None)
    frames = 1 if frames is None else int(frames)
    interval_ms = getattr(args, "interval_ms", None)
    interval_ms = 500 if interval_ms is None else int(interval_ms)
    call_args = {"file": args.file, "window": args.window, "frames": frames,
                 "interval_ms": interval_ms}
    body: dict = {"ok": False, "window": args.window}

    def _done(b: dict) -> int:
        s, _ = _report_for_attach(getattr(args, "agent", None))
        if s is not None:
            try:
                s.add_tool_call("screenshot", call_args, ok=bool(b.get("ok")),
                                ms=int((time.monotonic() - t0) * 1000), error=b.get("error"))
            except Exception:
                pass
        _emit(b)
        return 0 if b.get("ok") else 1

    if not os.path.isabs(args.file):
        body["error"] = f"pass an ABSOLUTE path for the image, got {args.file!r}"
        return _done(body)
    if frames < 1 or frames > _MAX_FRAMES:
        body["error"] = f"--frames must be 1..{_MAX_FRAMES}, got {frames}"
        return _done(body)
    if interval_ms < 0:
        body["error"] = f"--interval-ms must be >= 0, got {interval_ms}"
        return _done(body)

    try:
        cands = wc.list_candidates()
        target, matched_by = wc.select(cands, args.window)
    except wc.SelectError as exc:
        body["error"] = str(exc)
        body["candidates"] = [c.summary() for c in exc.candidates]
        return _done(body)
    except wc.CaptureError as exc:
        body["error"] = str(exc)
        return _done(body)

    # Grab every frame FIRST and encode afterwards, so PNG encoding (~100 ms a frame) does not
    # stretch the interval -- a motion burst is only evidence if its spacing is what was asked.
    raw: list[tuple[int, int, bytes, datetime_cls, int]] = []
    start = time.monotonic()
    try:
        for i in range(frames):
            due = start + i * interval_ms / 1000.0
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            at = datetime_cls.now(timezone.utc).astimezone()
            off = int((time.monotonic() - start) * 1000)
            w, h, px = wc.capture_bgra(target.hwnd)
            raw.append((w, h, px, at, off))
    except wc.CaptureError as exc:
        body["error"] = f"{exc} (after {len(raw)} of {frames} frame(s))"
        body["target"] = target.summary()
        return _done(body)

    paths = _window_frame_paths(args.file, frames)
    out_frames = []
    blank_frames = []
    prev = None
    for i, ((w, h, px, at, off), path) in enumerate(zip(raw, paths), start=1):
        stats = wc.image_stats(w, h, px)
        stamp = wc.make_stamp(target, matched_by=matched_by, selector=args.window,
                              width=w, height=h, frame=i, frames=frames, offset_ms=off,
                              captured_at=at)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(wc.bgra_to_png(w, h, px))
        row = {"path": path, "frame": i, "offset_ms": off, "size": [w, h],
               "blank": stats["blank"], "stats": stats, "source": stamp}
        if stats["blank"]:
            row["reason"] = stats.get("reason")
            blank_frames.append(i)
        if prev is not None and prev[0] == w and prev[1] == h:
            row["changed_from_previous"] = wc.frame_difference(w, h, prev[2], px)
        prev = (w, h, px)
        out_frames.append(row)

    body.update({"target": target.summary(), "matched_by": matched_by,
                 "provenance": target.info.get("project"),
                 "source": wc.describe_source(out_frames[0]["source"]),
                 "frames": out_frames, "path": out_frames[0]["path"],
                 "exists": True})
    if frames > 1:
        diffs = [f["changed_from_previous"] for f in out_frames if "changed_from_previous" in f]
        body["motion"] = {"max_changed": max(diffs) if diffs else None,
                          "identical_pairs": sum(1 for d in diffs if d == 0.0)}
        if diffs and max(diffs) == 0.0:
            body["warning"] = ("every frame in the burst is IDENTICAL -- no motion was captured. "
                               "If you meant to show animation, the client may be paused, "
                               "frozen or not rendering.")
    if not target.info.get("project"):
        body["warning_provenance"] = (
            "could not tell which project this window belongs to (no .uproject on its command "
            "line), so these frames are attached but do NOT count as pass proof.")

    # Attach every non-blank frame; a blank frame is never proof of anything.
    s, refusal = _report_for_attach(getattr(args, "agent", None))
    attached = []
    if refusal:
        body["report"] = refusal
    elif s is not None:
        for row in out_frames:
            if row["blank"]:
                continue
            cap = args.caption or ""
            if frames > 1:
                cap = f"{cap} (frame {row['frame']}/{frames}, +{row['offset_ms']} ms)".strip()
            rel = s.add_screenshot(row["path"], cap, provenance=target.info.get("project"),
                                   source=row["source"])
            if rel:
                attached.append(rel)
        body["attached"] = attached

    if blank_frames:
        body["ok"] = False
        body["blank_frames"] = blank_frames
        body["error"] = (
            f"BLANK capture: frame(s) {blank_frames} of {frames} came back black or one flat "
            f"colour ({out_frames[blank_frames[0] - 1].get('reason')}), so they were NOT attached "
            f"as proof. The window may be minimised, still loading, or not presenting frames. "
            f"Look at the file, fix the cause, and capture again.")
    else:
        body["ok"] = True
    return _done(body)


def _screenshot(args) -> int:
    if getattr(args, "window", None):
        return _window_screenshot(args)
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
  * Standalone `-game` clients (launch_2p_standalone.ps1) are NOT in the editor viewport: capture
    them with `uap screenshot <abs.png> --window Context_2` (or pid:<n> / a title substring);
    `--frames 8 --interval-ms 250` saves a stamped burst as motion proof.
  * A PASS requires a screenshot FROM THE EDITOR UNDER TEST -- `uap screenshot <abs.png>` via this
    project's uap.ps1 (it stamps the source editor). A shot of ANOTHER editor, or a manual attach
    of unknown origin, auto-FAILS the pass. And pixels aren't proof unless you read what they show
    (`uap read-ui`/state) and assert on it. Opt out (headless only): report start --no-require-screenshot.
  * NEVER tight-loop `uap exec` / RC while PIE is still initializing or transitioning -- Python
    remote-exec runs on the GAME THREAD and re-enters the engine task graph, HARD-CRASHING the editor
    (`RecursionGuard`, TaskGraph.cpp). Wait for PIE via the OS window (title has the project + "Preview")
    + a short settle delay, THEN exec/inject. Do NOT poll get_game_world() in a loop during startup.
  * A frame rate at or below 5 fps is NOT a game measurement -- the editor throttles to exactly
    3.0 fps whenever it is not the foreground window, and every timing taken then is void. uap
    now says so on the number itself (`throttled`/`warning` on report diag, perf stats, baselines
    and sample). Focus the PIE window and re-measure. If focus cannot be taken (SetForegroundWindow
    returns False / GetForegroundWindow is 0; `Slate.bAllowThrottling 0` does NOT lift it), report
    the timing as unmeasurable instead of using it -- `uap sample` and `uap input hold`/`axis`
    still measure correctly, since they run in-engine with no CLI calls during the window.
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
  uap status                      liveness + resolved RC port + CLI/plugin CONTRACT
                                  READ `contract`. This project vendors its own plugin copy
                                  while the CLI is shared, so the CLI runs ahead of it until
                                  it syncs + rebuilds. state: current | behind (with a
                                  `message` saying what is unavailable and the remedy) |
                                  unknown (could not check -- never reported as current).
                                  A `behind` copy can 404 a verb, and can also ACCEPT a call
                                  and silently drop a parameter it lacks -- ok, wrong result.
                                  `plugin_version` is a stale build stamp; it decides nothing.
  uap report diag                 capture editor/level/PIE + frame perf into the report
                                  A rate <= 5 fps comes back `throttled:true` + a `warning`:
                                  that is the editor's NOT-FOREGROUND throttle (it pins ~3.0
                                  fps), not the game. Focus the PIE window and re-measure;
                                  timing already taken is void. Same flag on `sample` (its own
                                  `hz`) and in the HTML report. Never report a sub-5-fps number
                                  as a performance finding -- it is a tooling state.

REPORT (a verification is NOT done until `report finish` emits the HTML report; cite its path)
  uap report start "<question>" [--no-require-screenshot]   # screenshot REQUIRED for pass by default
  uap report assert "<label>" pass|fail "<evidence>"
  uap report note "<text>"
  uap report finish pass|fail "<summary>"   # pass w/o screenshot -> FAIL; also auto-stops PIE (--keep-pie to skip)
  -> attach proof: `uap screenshot <abs.png>` (auto-attaches), or `uap report screenshot <file>`
  -> finish shows the report in ONE reusable browser window: each run REPLACES the window the
     previous report opened instead of leaving a tab behind. `--no-open` (or
     UAP_REPORT_NO_OPEN=1) renders it without a browser at all; the path is printed either way.

PLAY-IN-EDITOR
  uap pie start                   start PIE and BLOCK until the play world is live (default; a
                                  normal start is 1-5s). Answers playing:true + waited_seconds,
                                  the same shape `pie stop` answers with. On timeout (60s,
                                  --timeout / $UAP_PIE_START_TIMEOUT) it FAILS and says the
                                  session may still be queued -- never a cheerful ok.
                                  (Version-correct; do NOT use PlayWorldEditorSubsystem.)
  uap pie start --no-wait         fire-and-forget: return as soon as the session is QUEUED
                                  (queued:true, confirmed:false). The world does NOT exist when it
                                  returns -- follow with `pie wait` and never end a turn on it.
  uap pie start --mode vr         start VR PREVIEW instead -- the HMD code path (OpenXR input,
                                  IsHeadMountedDisplayEnabled branches). Flat PIE takes neither,
                                  so an HMD-only bug looks absent there. Needs a connected headset,
                                  and a plugin copy new enough to have StartPIEMode -- it REFUSES
                                  on an older one rather than quietly giving you flat PIE. Waits
                                  for the live world exactly like flat does.
  uap pie wait <seconds>          block until the game world is live. Plain `pie start` now does
                                  this for you; reach for `wait` after `--no-wait`, or to wait on
                                  a session someone else started. Poll PIE through THIS, never a
                                  tight `uap exec` loop -- exec runs on the game thread and
                                  re-entering it during a PIE transition HARD-CRASHES the editor.
  uap pie stop [--timeout 30]     stop PIE and WAIT until the teardown is confirmed. ok:true means
                                  the world is gone; on timeout it FAILS and says the editor is
                                  not free. Never treat a stop as done without ok:true+stopped:true.
  NEVER END A TURN WITH PIE RUNNING. Stop PIE and release your lease first -- `uap report finish`
  does the stop for you unless you pass --keep-pie. A live PIE session nobody is driving is how a
  PBW aircraft flew unattended into a building on 2026-08-28.

MULTI-AGENT COORDINATION (several agents sharing one editor; see docs/agent-coordination.md)
  Rebuild ONLY via Restart-Editor.ps1 -- it self-locks; never bounce the editor by hand.
  Editor-touching verbs auto-wait through another agent's rebuild (block then resume, not fail).
  uap lease status                          who holds the editor + why
  uap lease acquire exclusive --reason pie --agent <tok> [--wait 900]   # hold PIE/level across calls
  uap lease release --agent <tok>           # ...then release (pass the SAME token every call).
                                            REFUSES while PIE is still in progress -- stop PIE
                                            first (`uap pie stop`), or --force to hand over a
                                            live session on purpose.

TWO PROJECTS ON ONE MACHINE (a second lock, above the per-project lease)
  A project lease covers that project's editor. It says nothing about the OTHER editor open on
  this workstation -- and there is only one keyboard, one foreground window and one GPU. So
  `pie` / `input` / `screenshot` / `click` / `tab` / `nav` / `read-ui` also take a MACHINE-wide
  turn: if the other project is playing, yours WAITS (same block-then-`busy` as the lease) and
  the busy answer names the project holding it. Automatic -- nothing to remember.
  Calls from your own project pass straight through, so working alone never waits.
  `pie start` holds the machine until `pie stop`; everything else holds it for one call.
  uap lease machine-status                  which project owns the foreground right now
  uap lease machine-release [--force]       hand it back / break a hold left by a dead session
  Read-only verbs (status, log, sample, helpers) and rc/exec never take it.
  $UAP_MACHINE_LOCK=0 disables it entirely (escape hatch, not a setting).

DRIVE + OBSERVE
  uap rc <Func> [key=value ...]   call a plugin UFUNCTION (one-shot input injection lives here)
                                  Values are encoded against the function's DECLARED parameter
                                  type, read from the editor -- so `SlateUser=9` goes out as the
                                  string "9" rather than the number 9, which would not bind and
                                  would be dropped in silence. A parameter the function does not
                                  declare is REFUSED, not dropped. If the result carries
                                  `coercion.guessed`, those types were inferred from your text:
                                  pass them as one JSON object instead --
                                  uap rc <Func> '{"Name": "value"}'
  uap exec "<python>"             run `import unreal; ...` in the editor (escape hatch)
  uap read-ui                     dump on-screen UMG: [{text, x, y}, ...] + focused
  uap click "<label>"             click an on-screen UMG element by its visible text
  uap tab "<TabId>"               select a CommonUI tab by id (menus are tab-driven)
  uap nav up|down|left|right|accept|back   move UI focus / activate (Slate nav path)
  uap screenshot <file>           capture composited game+UMG frame (needs live PIE)
  uap screenshot <abs.png> --window <Context_2|pid:N|title> [--frames N --interval-ms M]
                                  capture a STANDALONE -game client's window (PrintWindow; works
                                  unfocused/paused), stamped with its process/project/context;
                                  blank frames refused; counts as pass proof for its project
  uap helpers [--grep RE] [--names]   list the project's test helpers with their arg schemas

SUSTAINED INPUT (a single injected event CANNOT drive locomotion -- see below)
  uap input hold <Key> --seconds N     hold a digital key; returns at once, held in-engine
                                       The hold OUTLIVES the call by design (read state while
                                       it holds), so the result carries `ends_in_seconds` +
                                       `ends_at_epoch` and a `note` saying it is still down.
                                       `--wait` blocks for the duration instead. A hold on a
                                       DIFFERENT key while one is still running is REFUSED --
                                       back-to-back holds used to overlap in silence and the
                                       pawn kept moving after the caller thought it had
                                       stopped (one session was discarded over a ~3500 cm
                                       drift). `--overlap` says you meant them simultaneous.
  uap input axis <AxisKey> <v> --seconds N   drive an analog axis -- THE VR LOCOMOTION VERB
                                       Two axes at once (stick X + Y) needs `--overlap`.
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
  GAMEPAD BUTTONS (Gamepad_FaceButton_Bottom, Gamepad_LeftTrigger, DPad...): use
  `input hold <GamepadKey>` -- it takes the VIEWPORT route and reaches Enhanced Input.
  `rc InjectGamepad` routes BUTTONS through Slate BY DESIGN, because a face/DPad press is
  also how UMG focus navigation is driven -- so a button injected that way reaches gameplay
  input only while the PIE viewport holds Slate focus, and looks like a dead feature when it
  does not. Sticks and keyboard keys are unaffected: both already take the viewport route.
  Reaching for `rc InjectGamepad` to drive gameplay, failing, and concluding "uap cannot
  reach Enhanced Input" has already cost another project several sessions -- it was the
  wrong verb, not a missing capability.

MOUSE (a captured viewport pins the pointer -- the POSITION is the hard part, not the click)
  uap input mouse move <x> <y>         put the agent cursor at an ABSOLUTE screen point
  uap input mouse click [x y] [--button left]   press+release there (omit x y to use the
                                       position `mouse move` left)
  x/y are ABSOLUTE SCREEN PIXELS -- exactly what `read-ui` prints, so its output feeds in
  unchanged. Read the element's x,y, move, click, then `read-ui` AGAIN to prove the UI
  changed. A screenshot is not proof; a different read-ui is.
  Why a verb at all: while PIE holds mouse capture the pointer CANNOT BE MOVED. Win32
  SetCursorPos is inert and so is FSlateApplication::SetCursorPos (FSlateUser reads the
  position straight back out of the platform cursor), so `SetCursorPos(1754,989)` then
  `GetCursorPos()` still reads 0,0 -- while both calls report success. The plugin therefore
  does not move the cursor: Slate routes a pointer event by the position carried ON THE EVENT,
  so it stamps that instead. The legacy chain (`rc InjectMouseMove` + `rc InjectMouseButton`)
  read the pinned OS cursor for its click position, so every click landed at 0,0, hit nothing
  and reported ok:true. That is the whole of ClickUp 17tm466fbyj -- the LAYER was right all
  along, the POSITION was the corner of the screen.
  The result carries `hit`: the Slate widgets really found under the point. A click whose
  `hit` is empty FAILS instead of returning ok. It also reports `os_cursor_moved` (usually
  false, and that is fine) and `game_mouse_set` -- the separate PlayerController-side cache
  that GetMousePosition / GetHitResultUnderCursor read, which IS settable under capture.

SAMPLING + LOGS (sub-second truth; a ~1s exec round-trip cannot see judder or a 0.6s wind-up)
  uap sample start <object> <property> --seconds N   per-frame series + delta stats
      object: /Game/... path | actor name in the live world | PlayerPawn | PlayerController
              | PlayerCameraManager
      property: dot path (CharacterMovement.Velocity) or a computed leaf
              (WorldLocation|WorldRotation|WorldScale|WorldTransform|ForwardVector|Velocity)
  uap sample read [--summary]          read the series (use with `sample start --no-wait`)
  uap log cursor                       grab a cursor BEFORE driving the condition
  uap log since <cursor> --grep RE     what the editor logged since then
  uap log tail 200 [--grep RE]         the last N captured lines (or --lines 200)
    The capture is a fixed-size ring (LogBufferCapacity, 4096 records), so a long window does
    not fit and a low count can mean "not looked at" rather than "not there". Every read now
    carries `dropped` / `oldest_cursor`, and sets `truncated` + `warning` when part of the
    window you asked for had already been evicted, or `stale_cursor` when the cursor predates
    an editor restart. Treat either one as a sweep that did not happen.
    `--grep` matches verbosity + category + message, so `--grep "Error|Warning"` finds records
    CLASSIFIED that way, not just ones whose text happens to say the word.
    On `log since`, `--lines` is a DISPLAY cap (default 2000), not a read bound: the whole
    window is paged and filtered first, so `count` is the complete match count and `--grep`
    cannot miss a match. If more matched than are listed you get `omitted` + a warning, and
    the NEWEST are the ones kept (`--keep oldest` to flip it). Before this, `--lines` went
    straight to the plugin, which fills its quota from the OLDEST record forward -- so the
    default 200 discarded the newest end of the window and a grep over it answered `count: 0`
    while 10 matches sat in it.

RECIPES
  Click an on-screen button by label (one call):
    uap click "VR TRAINING"
  ...or the underlying chain (what `uap click` does), e.g. to click a precise spot, and to
  PROVE it landed -- the second read-ui showing different content is the evidence:
    uap read-ui                                   # find the element's x,y
    uap input mouse move <x> <y>                  # `hit` names the widget really under it
    uap input mouse click
    uap read-ui                                   # MUST now show the new screen
  Do NOT use `rc InjectMouseMove` + `rc InjectMouseButton` for this. Those take the click
  position from the OS cursor, which a captured PIE viewport pins -- they report ok and click
  the top-left corner (ClickUp 17tm466fbyj).
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

SPEED: a uap call costs ~0.6s before it reaches the editor (PowerShell host + the launcher's
engine resolve + Python imports), and `exec` used to re-run the whole node-discovery handshake
every time on top of that. Two things to know:
  * `uap batch` runs many commands in ONE process and pays that once. 20 steps measured 11.0s as
    separate calls and 0.75s as a batch. Same verbs, same lease/machine-lock guards, one JSON
    line streamed per step plus a summary. Reach for it for any multi-step sequence.
        uap batch "pie start --mode vr" "exec print(1)" "rc GetPIEPhase" "pie stop"
        uap batch --file steps.txt      # one command per line, or a JSON array of arg arrays
  * the editor `exec` talks to is remembered between calls, so discovery is one ping instead of
    a ~1s handshake plus an identity probe per answering process. $UAP_NODE_CACHE=0 disables it.
  Neither changes what any verb PROVES: `pie stop` still does not return until teardown is
  confirmed, and `exec` still refuses to run against a process that does not match --project.

FLAGS: --project <name>, --instance <sel> and --agent <token> are accepted by EVERY verb
(ignored by the ones that don't touch the editor), so you can pass the same set on every call.
  --project picks the PROJECT; --instance picks WHICH PROCESS of it. Every verb targets that
  project's EDITOR by default. A `-game` standalone client (launch_2p_standalone.ps1) answers
  the same discovery under the same project name, so it used to be selectable by accident --
  it answered `None` for every editor subsystem, which reads as a broken editor. It is now
  refused by name unless you ask for it: --instance Context_2 / --instance pid:53164.
  `uap instances` lists every process that answers and says which one verbs will select.

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

# ...and, on top of that, wait out the OTHER PROJECT on this machine -- but only for the verbs
# that contend for something the whole workstation shares: PIE, real input injection, and the
# foreground window (a screenshot of a background editor is a 3fps stale frame, and `read-ui`
# needs focus to answer at all on this setup).
#
# Deliberately NOT here: `status`, `log`, `helpers`, `sample` (read-only -- they must answer while
# you diagnose), and `rc` / `exec` / `exec-file`. Those last three CAN do anything, but they are
# the generic transport for ordinary project-scoped work; gating every one of them on the other
# project's PIE session would make two projects serialize almost completely, which is a far bigger
# behaviour change than the clash being fixed. They remain governed by the per-project lease.
_MACHINE_GUARDED = {"pie", "input", "screenshot", "click", "tab", "nav", "read-ui"}

# `uap lease acquire --reason <r>` for one of these is a declaration that the agent is about to
# hold the foreground across several calls, so it takes the machine lock too. `rebuild` is absent
# on purpose: a rebuild is CPU-heavy but it neither takes the keyboard nor runs a game window, and
# blocking the other project for a 20-minute rebuild TTL would be a worse trade than the
# contention it avoids.
_FOREGROUND_REASONS = ("pie", "level", "input", "screenshot", "click", "nav")


def _pie_state_for_lease(project: str | None) -> tuple[bool | None, str]:
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
    try:
        return _lease_inner(args)
    except _coord.CoordinationError as exc:
        # The lease could not be decided safely, so it was not decided. Say that as a structured
        # refusal rather than a traceback: a traceback through a PowerShell tool comes back as
        # NativeCommandError with the message stripped, which reads as a silent failure.
        _emit({"ok": False, "coordination_unsafe": True, "cmd": f"lease {args.lease_cmd}",
               "error": str(exc),
               "hint": "nothing was granted or released. Retry; if it persists, `uap lease "
                       "status` and check no agent is wedged holding the editor."})
        return 1


def _lease_inner(args) -> int:
    proj = getattr(args, "project", "") or _env_project()
    cmd = args.lease_cmd
    if cmd == "acquire":
        res = _coord.acquire(proj, args.mode, reason=args.reason, agent=args.agent,
                             pid=args.pid, wait=args.wait, ttl=args.ttl)
        # A declared foreground hold (pie / level / input ...) is a claim on the whole
        # workstation, not just this project's editor, so take the machine turn as well. If the
        # OTHER project has it, give the project lease straight back rather than sitting on a
        # half-held claim that blocks this project's other agents for nothing.
        if res.get("granted") and _is_foreground_reason(args.reason) \
                and _coord.machine_lock_enabled():
            mach = _coord.acquire_machine(proj, reason=args.reason, agent=res["agent"],
                                          pid=args.pid, wait=args.wait, ttl=args.ttl)
            if mach.get("busy"):
                _coord.release(proj, agent=res["agent"])
                res = dict(mach)
                res["hint"] = ("another PROJECT on this machine holds the foreground; your "
                               "project lease was released again so it does not block your own "
                               "team. `uap lease machine-status` names the holder.")
            else:
                res["machine"] = mach
    elif cmd == "machine-status":
        res = _coord.machine_status()
    elif cmd == "machine-release":
        res = _coord.release_machine(agent=args.agent, project=None if args.force else proj,
                                     force=args.force)
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
            # Releasing the editor also gives the machine back: the agent has just proved PIE is
            # down, which is the whole reason the other project was waiting.
            res["machine"] = _coord.release_machine(agent=res["agent"], project=proj)
    elif cmd == "heartbeat":
        res = _coord.heartbeat(proj, agent=args.agent)
    else:  # status
        res = _coord.status(proj)
        # Contention is only legible with both scopes side by side: "my project lease is free"
        # explains nothing when what is actually blocking you is the other project's PIE.
        res["machine"] = _coord.machine_status()
    _emit(res)
    return 0 if res.get("ok", True) else 1


def _is_foreground_reason(reason: str) -> bool:
    """Does `--reason` declare a hold on the keyboard / foreground window / a PIE session?"""
    return (reason or "").strip().lower().split(":")[0] in _FOREGROUND_REASONS


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
    # --project picks the PROJECT. It does not pick the INSTANCE: a `-game` standalone client
    # (what launch_2p_standalone.ps1 starts) reports the same project file path as the editor
    # and answers the same discovery, so pinning the project alone let `exec` land on a client
    # and answer None for every editor subsystem. The default is now the editor, always;
    # this flag is how you ask for something else on purpose.
    proj.add_argument("--instance", default=os.environ.get("UAP_INSTANCE") or None,
                      help="which instance of that project: 'editor' (default), 'pid:<n>', or "
                           "a substring of the process command line (e.g. Context_2). "
                           "`uap instances` lists what is answering.")
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
    # The report slot is ONE per machine. Starting a report while ANOTHER agent's is running is
    # refused, because the take used to be silent and the first agent's evidence simply stopped
    # being recorded. --takeover is the deliberate override for a slot left by a dead session.
    rs.add_argument("--takeover", action="store_true",
                    help="take the report slot even though another agent's report is still "
                         "running (closes theirs as `incomplete` and renders it). Only for a "
                         "slot left behind by a session that is gone")
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
    rf.add_argument("--no-open", dest="no_open", action="store_true",
                    help="render the report but do not show it in a browser (same as "
                         "UAP_REPORT_NO_OPEN=1). The path is still printed; by default the "
                         "report REPLACES the window the previous report opened rather than "
                         "piling up a tab per run")
    rf.set_defaults(func=_report_finish)

    st = sub.add_parser("status", parents=[proj])
    st.set_defaults(func=_status)
    rcp = sub.add_parser("rc", parents=[proj])
    rcp.add_argument("rc_func")
    rcp.add_argument("params", nargs="*",
                     help="key=value pairs (e.g. Command=stat fps KeyName=E bPressed=true) "
                          "or a single JSON object")
    rcp.set_defaults(func=_rc)
    inst = sub.add_parser("instances", parents=[proj],
                          help="list every Unreal process answering remote-exec discovery "
                               "(editor vs -game client), and which one verbs will target")
    inst.set_defaults(func=_instances)
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
    ps.add_argument("--no-wait", action="store_true",
                    help="return as soon as the session is QUEUED instead of waiting for the "
                         "play world. Fire-and-forget: the world does not exist when it returns, "
                         "so follow it with `uap pie wait <seconds>` before reading anything and "
                         "NEVER end a turn on it. Only for a caller that genuinely has other work "
                         "to do while PIE comes up.")
    ps.add_argument("--timeout", type=float, default=None,
                    help="max seconds to wait for the play world to be LIVE (default 60, "
                         "$UAP_PIE_START_TIMEOUT). A normal start is 1-5s. On timeout the verb "
                         "FAILS and says the session may still be queued, rather than acking a "
                         "start that has not happened.")
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
    # --window: capture a STANDALONE game client's window (launch_2p_standalone.ps1) from the OS
    # side instead of the editor viewport. No editor is touched, so no lease is consulted.
    sc.add_argument("--window", default=None,
                    help="capture a standalone client's window instead of the editor viewport: "
                         "a Dev Auth context (Context_2), pid:<n>, or a window-title substring. "
                         "Ambiguous matches are refused with the candidates listed.")
    sc.add_argument("--frames", type=int, default=1,
                    help=f"with --window: capture N frames (1..{_MAX_FRAMES}) as motion proof")
    sc.add_argument("--interval-ms", dest="interval_ms", type=int, default=500,
                    help="with --window --frames: milliseconds between frames (default 500)")
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
                         "read game state WHILE it is held; the result then carries "
                         "ends_in_seconds / ends_at_epoch)")
    ih.add_argument("--overlap", action="store_true",
                    help="allow this hold to run ALONGSIDE one already active (e.g. two stick "
                         "axes at once). Without it a hold on a DIFFERENT key while another is "
                         "still running is refused, because back-to-back holds used to overlap "
                         "silently and the pawn kept moving after the caller thought it stopped")
    ih.set_defaults(func=_input)
    ia = inps.add_parser("axis", parents=[proj],
                         help="drive an analog axis FKey for N seconds (VR/gamepad sticks)")
    ia.add_argument("key", help="axis FKey, e.g. OculusTouch_Left_Thumbstick_Y or Gamepad_LeftY")
    ia.add_argument("value", type=float, help="-1.0 .. 1.0")
    ia.add_argument("--seconds", type=float, default=1.0)
    ia.add_argument("--wait", action="store_true", help="block until the hold expires")
    ia.add_argument("--overlap", action="store_true",
                    help="allow this hold to run ALONGSIDE one already active -- which is the "
                         "normal thing for two stick axes (X and Y together). Without it a hold "
                         "on a DIFFERENT key while another is still running is refused")
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

    # Mouse position inside a CAPTURED viewport. Not a hold -- a position, which is a different
    # problem: while PIE holds the mouse the pointer cannot be moved at all (Win32 SetCursorPos
    # and FSlateApplication::SetCursorPos are both inert), so the plugin stamps the position
    # onto the injected pointer events instead. See _input_mouse.
    im = inps.add_parser("mouse", help="position/click the mouse inside a captured PIE viewport")
    ims = im.add_subparsers(dest="mouse_cmd", required=True)
    imm = ims.add_parser("move", parents=[proj], help="move the agent cursor to x,y")
    imm.add_argument("x", type=float, help="ABSOLUTE screen pixels -- what `read-ui` reports")
    imm.add_argument("y", type=float)
    imm.set_defaults(func=_input_mouse)
    imc = ims.add_parser("click", parents=[proj],
                         help="press+release at the agent cursor, or at x y if given")
    imc.add_argument("x", type=float, nargs="?", default=None,
                     help="optional; omit to click where `mouse move` left the cursor")
    imc.add_argument("y", type=float, nargs="?", default=None)
    imc.add_argument("--button", default="left",
                     choices=["left", "right", "middle", "xbutton1", "xbutton2"])
    imc.set_defaults(func=_input_mouse)

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
        # For `tail` this is the WINDOW WIDTH ("the last N records") and always has been.
        # For `since` the window is fixed by the cursor, so it is only how many of the matches
        # to LIST -- it no longer bounds what is searched, and `count` is complete either way.
        # 2000 rather than 200 because a whole-session sweep is the documented use and the old
        # default silently hid the newest end of the window; agents had taken to passing
        # `--lines 30000` to defend against that, which should not have been necessary.
        lp.add_argument("--lines", type=int, default=200 if name == "tail" else 2000,
                        help=("how many of the last N records to read (window width)"
                              if name == "tail" else
                              "how many matching records to LIST (display cap, default 2000). "
                              "The whole window is scanned and filtered first, so `count` is "
                              "complete regardless of this."))
        if name == "since":
            lp.add_argument("--keep", choices=["newest", "oldest"], default="newest",
                            help="which end to list when more records match than --lines "
                                 "(default newest -- the records your action just caused)")
            lp.add_argument("--max-scan", dest="max_scan", type=int, default=_LOG_SCAN_MAX,
                            help="hard bound on how many records one scan will page through; "
                                 "hitting it sets scan_incomplete and makes `count` a floor")
        lp.add_argument("--grep", default="",
                        help="case-insensitive regex over verbosity + category + message, "
                             "so `--grep 'Error|Warning'` finds records CLASSIFIED that way")
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
        if name == "tail":
            # Same trap, other verb: `uap log tail 400` died with "unrecognized arguments: 400"
            # and required `--lines 400`. `since` already accepted its positional; this did not,
            # which makes the inconsistency itself the trap (ClickUp 17tm466ft7z).
            lp.add_argument("count", type=int, nargs="?", default=None,
                            help="how many lines (same as --lines)")
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
    lzs.add_parser("status", parents=[proj],
                   help="this project's lease + who holds the machine-wide foreground lock"
                   ).set_defaults(func=_lease)
    # The machine-wide lock: one per WORKSTATION, above every project lease. Inspect and break
    # only -- it is taken and given back automatically by the verbs that need the foreground.
    lzs.add_parser("machine-status", parents=[proj],
                   help="which PROJECT currently owns this machine's foreground (PIE / input / "
                        "screenshots), and whether the lock is enabled"
                   ).set_defaults(func=_lease)
    lzm = lzs.add_parser("machine-release", parents=[proj],
                         help="hand the machine-wide foreground lock back (default: only if it "
                              "is yours)")
    lzm.add_argument("--force", action="store_true",
                     help="break the lock whoever holds it. For a session that died without "
                          "releasing and has not aged out yet -- check `lease machine-status` "
                          "first, because the other project may simply still be playing.")
    lzm.set_defaults(func=_lease)

    # --- batch --------------------------------------------------------------------------
    b = sub.add_parser(
        "batch", parents=[proj],
        help="run several uap commands in ONE process, paying the startup cost once",
        description="Run several uap commands in one process. Each step is exactly the verb you "
                    "would have typed, runs under the same lease/machine-lock guards, and emits "
                    "its own JSON line as it finishes; a final line summarises the run. The "
                    "batch's --project/--agent are inherited by every step that does not set "
                    "its own. Steps come from arguments, --file, or stdin, as either one shell "
                    "line per command or a JSON array of argument arrays.")
    b.add_argument("steps", nargs="*",
                   help='one command per argument, e.g. "exec print(1)" "rc GetPIEPhase"')
    b.add_argument("--file", default=None,
                   help="read the steps from this file ('-' for stdin)")
    b.add_argument("--keep-going", action="store_true",
                   help="run every step even after one fails (default: stop, because a sequence "
                        "normally assumes the step before it worked)")
    b.set_defaults(func=_batch)
    return p


# --- batch: many commands, one process ---------------------------------------------------
# The per-call price of `uap` is mostly fixed and mostly not the editor. Measured on this
# workstation (ClickUp 17tm466ft35): ~195ms to start a PowerShell host, ~170ms for the launcher's
# engine resolve, ~235ms for the Python interpreter plus this package's imports -- roughly 0.6s
# before a single packet leaves the machine -- and then, for `exec`, a full discovery handshake.
# An agent running a 20-step sequence paid every one of those 20 times.
#
# `uap batch` pays them once. It is deliberately NOT a daemon: no background process to leak, no
# lifetime to manage, no new way for a session to be left running. Each step runs through exactly
# the same guard path as a standalone invocation (`_run_parsed`), so the lease, the machine lock
# and every verb's semantics are unchanged -- what is saved is the setup, not the safety.


def _batch_steps(args) -> list[list[str]]:
    """The steps to run, from --file / stdin / inline arguments.

    Two accepted shapes, because quoting is where a batch format goes wrong. A JSON array of
    argument arrays is unambiguous and is what a script should emit; plain lines are for a human
    and are split with shell rules.
    """
    if args.steps:
        raw = "\n".join(args.steps)
    elif args.file and args.file != "-":
        raw = pathlib.Path(args.file).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    raw = raw.strip()
    if raw.startswith("["):
        parsed = json.loads(raw)
        return [list(s) if isinstance(s, list) else shlex.split(str(s)) for s in parsed]
    return [shlex.split(line) for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _batch_run_step(args) -> int:
    """Run one batch step exactly as a standalone `uap` invocation would.

    A named seam, not indirection for its own sake: it is what makes "a step is the same verb you
    would have typed" testable, and it is the single place a step's guard path could ever diverge
    from a standalone one -- so if it does, it does so visibly.
    """
    return _run_parsed(args)


def _batch(args) -> int:
    parser = build_parser()
    try:
        steps = _batch_steps(args)
    except (OSError, ValueError) as exc:
        _emit({"ok": False, "error": f"could not read the batch steps: {exc}"})
        return 2
    if not steps:
        _emit({"ok": False, "error": "no steps to run"})
        return 2

    results: list[dict] = []
    failed = 0
    t0 = time.monotonic()
    for i, argv in enumerate(steps):
        # Inherit the batch's own --project/--agent unless the step names its own, so a caller
        # does not have to repeat the lease token on all twenty lines (and cannot forget it on
        # one, which is how an agent locks itself out of its own lease).
        if "--project" not in argv and args.project:
            argv = [*argv, "--project", args.project]
        if "--agent" not in argv and args.agent:
            argv = [*argv, "--agent", args.agent]
        step: dict = {"step": i + 1, "argv": argv}
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = _batch_run_step(parser.parse_args(argv))
        except SystemExit as exc:            # argparse refused the step's own arguments
            code = int(exc.code or 2)
        except Exception as exc:             # a bad step must not kill the whole batch
            code = 1
            step["error"] = str(exc)
        out = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
        step["exit_code"] = code
        step["seconds"] = round(time.monotonic() - t0, 2)
        try:
            step["body"] = json.loads(out[-1]) if out else None
        except (ValueError, IndexError):
            step["body"] = None
            step["output"] = out
        results.append(step)
        _emit(step)                          # stream, so a long batch is never silent
        if code != 0:
            failed += 1
            if not args.keep_going:
                break
    summary = {"ok": failed == 0, "batch": True, "steps": len(steps), "ran": len(results),
               "failed": failed, "seconds": round(time.monotonic() - t0, 2),
               "results": results}
    if failed and not args.keep_going and len(results) < len(steps):
        summary["stopped_early"] = True
        summary["not_run"] = len(steps) - len(results)
        summary["hint"] = ("a step failed and the rest were NOT run, because a sequence normally "
                           "assumes the step before it worked. Pass --keep-going to run them all "
                           "regardless.")
    _emit(summary)
    return 0 if failed == 0 else 1


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
    return _run_parsed(build_parser().parse_args(argv))


def _run_parsed(args) -> int:
    cmd = getattr(args, "cmd", None)
    project = getattr(args, "project", "") or _env_project()
    # `screenshot --window` captures a standalone game client from the OS side. It touches no
    # editor, takes no input and needs no foreground, so it must not queue behind a rebuild, an
    # exclusive lease or the machine lock -- a two-client test holding its own lease would
    # otherwise wait on itself, and a rebuild of the editor has nothing to do with the client.
    if cmd == "screenshot" and getattr(args, "window", None):
        return args.func(args)
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

    # ...and finally the MACHINE turn, for the verbs that contend for the one keyboard / one
    # foreground window / one GPU this workstation has. Without it, a project lease said nothing
    # about the OTHER project's editor: a Project Broken Wings agent could start PIE while a
    # School's Out VR agent was mid-session, and neither lease noticed.
    return _run_under_machine_lock(args, cmd, project)


def _machine_reason(args, cmd: str) -> str:
    """What the machine-lock holder record says it is doing, e.g. `pie:start`, `screenshot`."""
    sub = getattr(args, f"{cmd.replace('-', '_')}_cmd", "") or ""
    return f"{cmd}:{sub}" if sub else cmd


def _release_machine_quiet(**kw) -> None:
    """Fail-open release: the machine lock must never be the reason a command reports failure."""
    try:
        _coord.release_machine(**kw)
    except Exception:
        pass


def _run_under_machine_lock(args, cmd: str | None, project: str) -> int:
    """Take the machine-wide foreground turn (if this verb needs it), run the verb, hand it back.

    Two hold shapes, because two lifetimes:
      * `pie start` takes a STICKY hold (pid 0, so it outlives this short-lived process) -- what it
        is holding is the PIE SESSION, which lives on after the command returns. `pie stop` frees
        it; otherwise the TTL does, the same way an abandoned project lease is reclaimed.
      * every other guarded verb takes a TRANSIENT hold, released in `finally` -- it only needs the
        foreground for the duration of the call.
    A call whose project already holds the lock passes straight through and releases nothing.
    """
    sticky = cmd == "pie" and getattr(args, "pie_cmd", "") == "start"
    machine = None
    if cmd in _MACHINE_GUARDED and _coord.machine_lock_enabled():
        agent = getattr(args, "agent", None) or _coord.default_agent_id()
        try:
            machine = _coord.acquire_machine(
                project, reason=_machine_reason(args, cmd), agent=agent,
                pid=0 if sticky else None, wait=_lease_wait_cap(),
                ttl=_coord.MACHINE_TTL if sticky else _coord.MACHINE_TRANSIENT_TTL)
        except Exception:
            machine = None          # fail-open: a coordination bug must not brick uap
        if machine is not None and machine.get("busy"):
            _emit({"ok": False, "busy": True, "blocked_by": machine.get("blocked_by"),
                   "project": machine.get("project"), "reason": machine.get("reason"),
                   "scope": "machine", "cmd": cmd, "agent": agent,
                   "hint": "another PROJECT on this machine holds the foreground (PIE / input / "
                           "screenshots); this is the machine-wide lock, not your project lease. "
                           "Wait for it, or if that session is gone: "
                           "`uap lease machine-status`, then "
                           "`uap lease machine-release --force`. $UAP_MACHINE_LOCK=0 disables it."})
            return 1
    try:
        rc = args.func(args)
    finally:
        if machine is not None and machine.get("acquired") and not sticky:
            _release_machine_quiet(agent=machine.get("agent"))
    # A CONFIRMED `pie stop` is what ends the sticky hold: the session the other project was
    # waiting on is genuinely over. Only on success -- a stop that failed left PIE running.
    if rc == 0 and cmd == "pie" and getattr(args, "pie_cmd", "") == "stop":
        _release_machine_quiet(agent=getattr(args, "agent", None) or _coord.default_agent_id(),
                               project=project)
    return rc


if __name__ == "__main__":
    sys.exit(main())
