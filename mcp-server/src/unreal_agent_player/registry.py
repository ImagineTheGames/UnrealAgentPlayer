"""Tool registration wiring."""

from __future__ import annotations

import json
import time as _time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from unreal_agent_player.baselines import BaselineStore
from unreal_agent_player.errors import AgentError
from unreal_agent_player.instances import InstanceRegistry
from unreal_agent_player.reporting import session as report_session
from unreal_agent_player.reporting.session import record_call
from unreal_agent_player.tools.actor import (
    actor_call_function,
    actor_find,
    actor_get_properties,
    actor_set_property,
)
from unreal_agent_player.tools.baseline import perf_baseline_compare, perf_baseline_save
from unreal_agent_player.tools.console import exec_console as tool_exec_console
from unreal_agent_player.tools.game import game_attach, game_launch, game_list, game_stop
from unreal_agent_player.tools.helpers import helper_call, helper_list
from unreal_agent_player.tools.input import (
    input_axis,
    input_gamepad,
    input_key,
    input_mouse_button,
    input_mouse_move,
    input_sequence,
    input_xr_button,
    input_xr_clear,
    input_xr_pose,
)
from unreal_agent_player.tools.log import log_since, log_tail
from unreal_agent_player.tools.perf import perf_stat, perf_trace_start, perf_trace_stop
from unreal_agent_player.tools.pie import (
    pie_pause,
    pie_resume,
    pie_start,
    pie_status,
    pie_stop,
)
from unreal_agent_player.tools.python_exec import exec_python as tool_exec_python
from unreal_agent_player.tools.report import (
    report_assert,
    report_caption,
    report_finish,
    report_note,
    report_start,
)
from unreal_agent_player.tools.screenshot import screenshot_viewport
from unreal_agent_player.tools.status import bridge_status
from unreal_agent_player.tools.ui import ui_find_window, ui_list_menus, ui_menu_click
from unreal_agent_player.tools.ui_read import read_viewport_ui
from unreal_agent_player.transport import (
    SUBSYSTEM_OBJECT_PATH,
    PythonRemoteExecClient,
    RemoteControlClient,
    rc_for_port,
)
from unreal_agent_player.uia import UIADriver

_GAME_RUNTIME_OBJ = "/Script/UnrealAgentPlayerRuntime.Default__UAPAgentRuntimeSubsystem"


ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


def register_all(
    server: Server,
    *,
    rc: RemoteControlClient,
    py_exec: PythonRemoteExecClient,
    store: BaselineStore,
    ui_driver: UIADriver,
) -> None:
    tools: list[Tool] = [
        Tool(
            name="bridge_status",
            description=(
                "Preflight: check Unreal Editor is reachable via Remote Control and "
                "Python Remote Execution. Returns per-channel diagnostics and plugin version."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="pie_start",
            description=("QUEUE a Play-In-Editor session. Optional map_path and start_location. "
                         "Returns queued:true, confirmed:false + phase/elapsed -- the play world "
                         "does not exist yet, so poll pie_status for phase Playing before reading "
                         "game state or capturing a frame."),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["PlayInViewport", "PlayInNewWindow", "Simulate"], "default": "PlayInViewport"},
                    "map_path": {"type": "string"},
                    "start_location": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                },
                "additionalProperties": False,
            },
        ),
        Tool(name="pie_stop",
             description=("Stop PIE and WAIT until the teardown is confirmed. ok:true + "
                          "stopped:true is the only 'PIE is gone'; on timeout it returns ok:false "
                          "saying the editor is not free, never a bare ack of the request."),
             inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        Tool(name="pie_pause",  description="Pause PIE.",  inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        Tool(name="pie_resume", description="Resume PIE.", inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        Tool(name="pie_status", description="Current PIE phase + elapsed seconds.", inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
        Tool(
            name="exec_console",
            description="Run a UE console command (exec, stat, showflag, etc.). Returns textual output.",
            inputSchema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="exec_python",
            description="Run arbitrary Python in the editor via Python Remote Execution.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "context": {"type": "string", "enum": ["editor", "pie"], "default": "editor"},
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        ),
        Tool(name="actor_find",
             description="Find actors by class, tag, or name pattern in the editor or PIE world.",
             inputSchema={"type": "object",
                 "properties": {"class_": {"type": "string"}, "tag": {"type": "string"},
                                "name_pattern": {"type": "string"},
                                "world": {"type": "string", "enum": ["editor", "pie"], "default": "editor"}},
                 "additionalProperties": False}),
        Tool(name="actor_get_properties",
             description="Get editor-exposed properties of an object at path.",
             inputSchema={"type": "object",
                 "properties": {"path": {"type": "string"}, "property_names": {"type": "array", "items": {"type": "string"}}},
                 "required": ["path"], "additionalProperties": False}),
        Tool(name="actor_set_property",
             description="Set one editor-exposed property on an object at path.",
             inputSchema={"type": "object",
                 "properties": {"path": {"type": "string"}, "name": {"type": "string"}, "value": {}},
                 "required": ["path", "name", "value"], "additionalProperties": False}),
        Tool(name="actor_call_function",
             description="Call a UFUNCTION on an object at path with named args.",
             inputSchema={"type": "object",
                 "properties": {"path": {"type": "string"}, "function": {"type": "string"}, "args": {"type": "object"}},
                 "required": ["path", "function"], "additionalProperties": False}),
        Tool(
            name="screenshot_viewport",
            description=("Capture the PIE/editor viewport. Default (ui=true) grabs the composited "
                         "backbuffer INCLUDING on-screen UMG (window resolution). Set ui=false for a "
                         "HighResShot of the 3D scene only (no UMG, but arbitrary resolution/HDR). "
                         "Returns file path; set inline=true for base64."),
            inputSchema={
                "type": "object",
                "properties": {
                    "resolution": {"type": "string", "default": "1920x1080", "pattern": r"^\d{2,5}x\d{2,5}$"},
                    "hdr": {"type": "boolean", "default": False},
                    "ui": {"type": "boolean", "default": True},
                    "inline": {"type": "boolean", "default": False},
                    "target": {"type": "string", "description": "Instance id or 'editor' (default)"},
                },
                "additionalProperties": False,
            },
        ),
        Tool(name="log_tail",
             description="Return the most recent N log lines.",
             inputSchema={"type": "object",
                 "properties": {"lines": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
                                "category": {"type": "string", "default": ""},
                                "min_verbosity": {"type": "string", "enum": ["Fatal", "Error", "Warning", "Display", "Log", "Verbose", "VeryVerbose"], "default": "Log"},
                                "target": {"type": "string", "description": "Instance id or 'editor' (default)"}},
                 "additionalProperties": False}),
        Tool(name="log_since",
             description="Return log lines newer than `cursor`. Use with cursor from a prior call for streaming.",
             inputSchema={"type": "object",
                 "properties": {"cursor": {"type": "integer", "minimum": 0},
                                "max_lines": {"type": "integer", "default": 500, "minimum": 1, "maximum": 5000},
                                "category": {"type": "string", "default": ""},
                                "min_verbosity": {"type": "string", "enum": ["Fatal", "Error", "Warning", "Display", "Log", "Verbose", "VeryVerbose"], "default": "Log"},
                                "target": {"type": "string", "description": "Instance id or 'editor' (default)"}},
                 "required": ["cursor"], "additionalProperties": False}),
        Tool(name="input_key",
             description="Press or release a key. Optional duration_ms auto-releases.",
             inputSchema={"type":"object",
                 "properties": {"key":{"type":"string"}, "pressed":{"type":"boolean"},
                                 "repeat":{"type":"boolean","default":False},
                                 "duration_ms":{"type":"integer","minimum":1,"maximum":10000},
                                 "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["key","pressed"], "additionalProperties":False}),
        Tool(name="input_mouse_move",
             description="Move mouse relative (dx,dy) or absolute (x,y, absolute=true).",
             inputSchema={"type":"object",
                 "properties": {"dx":{"type":"number"}, "dy":{"type":"number"},
                                 "x":{"type":"number"},  "y":{"type":"number"},
                                 "absolute":{"type":"boolean","default":False},
                                 "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "additionalProperties":False}),
        Tool(name="input_mouse_button",
             description="Press or release a mouse button.",
             inputSchema={"type":"object",
                 "properties": {"button":{"type":"string","enum":["left","right","middle","x1","x2"]},
                                 "pressed":{"type":"boolean"},
                                 "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["button","pressed"], "additionalProperties":False}),
        Tool(name="input_axis",
             description="Inject a named axis value (triggers axis input mappings).",
             inputSchema={"type":"object",
                 "properties": {"axis_name":{"type":"string"}, "value":{"type":"number"},
                                 "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["axis_name","value"], "additionalProperties":False}),
        Tool(name="input_gamepad",
             description="Press/release or set analog value for a gamepad control.",
             inputSchema={"type":"object",
                 "properties": {"button":{"type":"string"}, "pressed":{"type":"boolean"},
                                 "analog":{"type":"number","default":1.0},
                                 "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["button","pressed"], "additionalProperties":False}),
        Tool(name="input_sequence",
             description="Run a sequence of input steps with per-step wait_ms. Cheaper than many calls.",
             inputSchema={"type":"object",
                 "properties": {"steps":{"type":"array","items":{"type":"object"}},
                                 "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["steps"], "additionalProperties":False}),
        Tool(name="input_xr_button",
             description="Press/release a Quest Touch controller button (OculusTouch_* FKey) for a hand. VR (V2).",
             inputSchema={"type":"object",
                 "properties": {"hand":{"type":"string","enum":["Left","Right"]},
                                "key":{"type":"string"}, "pressed":{"type":"boolean"},
                                "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["hand","key","pressed"], "additionalProperties":False}),
        Tool(name="input_xr_pose",
             description="Override a motion-controller pose (position cm + orientation pitch/yaw/roll) for a hand. VR (V2).",
             inputSchema={"type":"object",
                 "properties": {"hand":{"type":"string","enum":["Left","Right"]},
                                "position":{"type":"array","items":{"type":"number"},"minItems":3,"maxItems":3},
                                "orientation":{"type":"array","items":{"type":"number"},"minItems":3,"maxItems":3},
                                "tracked":{"type":"boolean","default":True},
                                "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["hand","position","orientation"], "additionalProperties":False}),
        Tool(name="input_xr_clear",
             description="Clear the agent pose override for a hand so real devices win again. VR (V2).",
             inputSchema={"type":"object",
                 "properties": {"hand":{"type":"string","enum":["Left","Right"]},
                                "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["hand"], "additionalProperties":False}),
        Tool(name="helper_list",
             description="List all AgentTestHelper UFUNCTIONs with arg/return JSON schemas.",
             inputSchema={"type":"object",
                 "properties":{"category":{"type":"string"},
                               "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "additionalProperties":False}),
        Tool(name="helper_call",
             description="Invoke an AgentTestHelper by name with JSON args. Returns its result.",
             inputSchema={"type":"object",
                 "properties":{"name":{"type":"string"}, "args":{"type":"object"},
                               "target":{"type":"string","description":"Instance id or 'editor' (default)"}},
                 "required":["name"], "additionalProperties":False}),
        Tool(name="perf_stat",
             description="Read stat unit or fps and return parsed values + raw text.",
             inputSchema={"type":"object",
                 "properties":{"stat_group":{"type":"string","enum":["unit","fps"],"default":"unit"}},
                 "additionalProperties":False}),
        Tool(name="perf_trace_start",
             description="Start an Unreal Insights trace. Returns trace_id.",
             inputSchema={"type":"object",
                 "properties":{"channels":{"type":"array","items":{"type":"string"}}},
                 "additionalProperties":False}),
        Tool(name="perf_trace_stop",
             description="Stop the named trace. Returns .utrace path; summary is a stub in v1.",
             inputSchema={"type":"object",
                 "properties":{"trace_id":{"type":"string"}},
                 "required":["trace_id"], "additionalProperties":False}),
        Tool(name="perf_baseline_save",
             description="Capture current perf stats and store them as a named baseline for later regression checks.",
             inputSchema={"type":"object",
                 "properties":{"name":{"type":"string"},
                               "stat_group":{"type":"string","enum":["unit","fps"],"default":"unit"}},
                 "required":["name"], "additionalProperties":False}),
        Tool(name="perf_baseline_compare",
             description="Compare current perf stats against a named baseline; flags metrics exceeding tolerance_pct.",
             inputSchema={"type":"object",
                 "properties":{"name":{"type":"string"},
                               "stat_group":{"type":"string","enum":["unit","fps"],"default":"unit"},
                               "tolerance_pct":{"type":"number","default":10.0}},
                 "required":["name"], "additionalProperties":False}),
        Tool(name="ui_menu_click",
             description="Click an editor menu by name path (e.g. ['Window','Viewports','Viewport 1']) via Windows UIAutomation. Windows only (V3).",
             inputSchema={"type":"object",
                 "properties":{"window_title":{"type":"string"},
                               "path":{"type":"array","items":{"type":"string"},"minItems":1}},
                 "required":["window_title","path"], "additionalProperties":False}),
        Tool(name="ui_find_window",
             description="Check whether a top-level editor window with a title substring exists. Windows only (V3).",
             inputSchema={"type":"object",
                 "properties":{"window_title":{"type":"string"}},
                 "required":["window_title"], "additionalProperties":False}),
        Tool(name="ui_list_menus",
             description="List menu-item names exposed by an editor window via UIAutomation. Windows only (V3).",
             inputSchema={"type":"object",
                 "properties":{"window_title":{"type":"string"}},
                 "required":["window_title"], "additionalProperties":False}),
        Tool(name="read_viewport_ui",
             description=(
                 "Read the on-screen UMG of the running PIE game: visible text, screen position, "
                 "and which element is focused. Use this to read prompts/labels (e.g. 'Press E') "
                 "and menu highlight before injecting input, instead of guessing. "
                 "Returns {available, count, focused, texts:[{text,x,y,focused}]}."),
             inputSchema={"type":"object","properties":{},"additionalProperties":False}),
        Tool(name="report_start",
             description="Begin an agent run report. Auto-captures subsequent tool calls + screenshots until report_finish.",
             inputSchema={"type":"object","properties":{"task":{"type":"string"},"project":{"type":"string"}},
                 "required":["task"],"additionalProperties":False}),
        Tool(name="report_assert",
             description="Record a pass/fail check with evidence into the active report.",
             inputSchema={"type":"object","properties":{"label":{"type":"string"},"passed":{"type":"boolean"},"evidence":{"type":"string"}},
                 "required":["label","passed"],"additionalProperties":False}),
        Tool(name="report_note",
             description="Add a curated note to the active report (optional section heading).",
             inputSchema={"type":"object","properties":{"text":{"type":"string"},"section":{"type":"string"}},
                 "required":["text"],"additionalProperties":False}),
        Tool(name="report_caption",
             description="Set a caption on a gallery screenshot (by index/filename; omit for most recent).",
             inputSchema={"type":"object","properties":{"caption":{"type":"string"},"screenshot":{}},
                 "required":["caption"],"additionalProperties":False}),
        Tool(name="report_finish",
             description="Finish the report: verdict 'pass'/'fail' + summary. Renders tabbed HTML and opens it.",
             inputSchema={"type":"object","properties":{"verdict":{"type":"string","enum":["pass","fail"]},"summary":{"type":"string"}},
                 "required":["verdict","summary"],"additionalProperties":False}),
        Tool(name="game_launch",
             description=("Launch a standalone game instance (its own RemoteControl port) and drive it like a "
                          "player. Returns instance_id + port for use as `target` in play tools. no_vr=true "
                          "(default) runs flat without a headset (-nohmd) for auto-testing; best with the "
                          "editor closed to avoid GPU contention."),
             inputSchema={"type":"object",
                 "properties":{"target":{"type":"string","default":"standalone"},
                               "map":{"type":"string"},
                               "no_vr":{"type":"boolean","default":True},
                               "extra_args":{"type":"array","items":{"type":"string"}}},
                 "additionalProperties":False}),
        Tool(name="game_attach",
             description="Attach to an already-running game instance by its RemoteControl port.",
             inputSchema={"type":"object",
                 "properties":{"port":{"type":"integer"}},
                 "required":["port"],"additionalProperties":False}),
        Tool(name="game_list",
             description="List all registered game instances.",
             inputSchema={"type":"object","properties":{},"additionalProperties":False}),
        Tool(name="game_stop",
             description="Stop and deregister a game instance by instance_id.",
             inputSchema={"type":"object",
                 "properties":{"instance_id":{"type":"string"}},
                 "required":["instance_id"],"additionalProperties":False}),
    ]

    registry = InstanceRegistry(editor_port=30010)

    def _resolve_rc_and_obj(kw: dict) -> tuple:
        """Pop target from kw; return (use_rc, obj_path)."""
        target = kw.pop("target", None)
        port = registry.resolve_port(target)
        if port == 30010:
            return rc, SUBSYSTEM_OBJECT_PATH
        return rc_for_port(port), _GAME_RUNTIME_OBJ

    def _wrap(fn):
        async def handler(**kw):
            use_rc, obj = _resolve_rc_and_obj(kw)
            return await fn(rc=use_rc, py_exec=py_exec, _object_path=obj, **kw)
        return handler

    handlers: dict[str, ToolHandler] = {
        "bridge_status": lambda **_: bridge_status(rc=rc, py_exec=py_exec),
        "pie_start":  lambda **kw: pie_start(rc=rc, py_exec=py_exec, **kw),
        "pie_stop":   lambda **_:  pie_stop(rc=rc, py_exec=py_exec),
        "pie_pause":  lambda **_:  pie_pause(rc=rc, py_exec=py_exec),
        "pie_resume": lambda **_:  pie_resume(rc=rc, py_exec=py_exec),
        "pie_status": lambda **_:  pie_status(rc=rc, py_exec=py_exec),
        "exec_console": lambda **kw: tool_exec_console(rc=rc, py_exec=py_exec, **kw),
        "exec_python": lambda **kw: tool_exec_python(rc=rc, py_exec=py_exec, **kw),
        "log_tail":  _wrap(log_tail),
        "log_since": _wrap(log_since),
        "screenshot_viewport": _wrap(screenshot_viewport),
        "actor_find":           lambda **kw: actor_find(rc=rc, py_exec=py_exec, **kw),
        "actor_get_properties": lambda **kw: actor_get_properties(rc=rc, py_exec=py_exec, **kw),
        "actor_set_property":   lambda **kw: actor_set_property(rc=rc, py_exec=py_exec, **kw),
        "actor_call_function":  lambda **kw: actor_call_function(rc=rc, py_exec=py_exec, **kw),
        "input_key":          _wrap(input_key),
        "input_mouse_move":   _wrap(input_mouse_move),
        "input_mouse_button": _wrap(input_mouse_button),
        "input_axis":         _wrap(input_axis),
        "input_gamepad":      _wrap(input_gamepad),
        "input_sequence":     _wrap(input_sequence),
        "input_xr_button":    _wrap(input_xr_button),
        "input_xr_pose":      _wrap(input_xr_pose),
        "input_xr_clear":     _wrap(input_xr_clear),
        "helper_list": _wrap(helper_list),
        "helper_call": _wrap(helper_call),
        "perf_stat":        lambda **kw: perf_stat(rc=rc, py_exec=py_exec, **kw),
        "perf_trace_start": lambda **kw: perf_trace_start(rc=rc, py_exec=py_exec, **kw),
        "perf_trace_stop":  lambda **kw: perf_trace_stop(rc=rc, py_exec=py_exec, **kw),
        "perf_baseline_save":    lambda **kw: perf_baseline_save(rc=rc, py_exec=py_exec, store=store, **kw),
        "perf_baseline_compare": lambda **kw: perf_baseline_compare(rc=rc, py_exec=py_exec, store=store, **kw),
        "ui_menu_click":  lambda **kw: ui_menu_click(driver=ui_driver, **kw),
        "ui_find_window": lambda **kw: ui_find_window(driver=ui_driver, **kw),
        "ui_list_menus":  lambda **kw: ui_list_menus(driver=ui_driver, **kw),
        "read_viewport_ui": _wrap(read_viewport_ui),
        "report_start":   lambda **kw: report_start(rc=rc, py_exec=py_exec, **kw),
        "report_assert":  lambda **kw: report_assert(rc=rc, py_exec=py_exec, **kw),
        "report_note":    lambda **kw: report_note(rc=rc, py_exec=py_exec, **kw),
        "report_caption": lambda **kw: report_caption(rc=rc, py_exec=py_exec, **kw),
        "report_finish":  lambda **kw: report_finish(rc=rc, py_exec=py_exec, **kw),
        "game_launch": lambda **kw: game_launch(registry=registry, rc=rc, py_exec=py_exec, **kw),
        "game_attach": lambda **kw: game_attach(registry=registry, rc=rc, py_exec=py_exec, **kw),
        "game_list":   lambda **_: game_list(registry=registry, rc=rc, py_exec=py_exec),
        "game_stop":   lambda **kw: game_stop(registry=registry, rc=rc, py_exec=py_exec, **kw),
    }

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        handler = handlers.get(name)
        if handler is None:
            body = {
                "ok": False,
                "error": {
                    "code": "TOOL_UNKNOWN",
                    "message": name,
                    "domain": "mcp_side",
                    "recoverable": False,
                },
            }
        else:
            try:
                _t0 = _time.monotonic()
                body = await handler(**arguments)
                _ms = int((_time.monotonic() - _t0) * 1000)
                _sess = report_session.active()
                if _sess is not None and name != "report_finish":
                    record_call(_sess, name, arguments, body, _ms)
            except AgentError as exc:
                body = exc.to_response()
            except Exception as exc:
                body = {
                    "ok": False,
                    "error": {
                        "code": "UNHANDLED_EXCEPTION",
                        "message": str(exc),
                        "domain": "mcp_side",
                        "recoverable": False,
                    },
                }
        return [TextContent(type="text", text=json.dumps(body))]
