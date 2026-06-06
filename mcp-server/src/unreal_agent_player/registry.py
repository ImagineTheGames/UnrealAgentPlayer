"""Tool registration wiring."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from mcp.server import Server
from mcp.types import TextContent, Tool

from unreal_agent_player.errors import AgentError
from unreal_agent_player.tools.console import exec_console as tool_exec_console
from unreal_agent_player.tools.pie import (
    pie_pause, pie_resume, pie_start, pie_status, pie_stop,
)
from unreal_agent_player.tools.python_exec import exec_python as tool_exec_python
from unreal_agent_player.tools.actor import (
    actor_call_function, actor_find, actor_get_properties, actor_set_property,
)
from unreal_agent_player.tools.log import log_since, log_tail
from unreal_agent_player.tools.screenshot import screenshot_viewport
from unreal_agent_player.tools.status import bridge_status
from unreal_agent_player.tools.input import (
    input_axis, input_gamepad, input_key, input_mouse_button,
    input_mouse_move, input_sequence,
    input_xr_button, input_xr_clear, input_xr_pose,
)
from unreal_agent_player.tools.helpers import helper_call, helper_list
from unreal_agent_player.tools.perf import perf_stat, perf_trace_start, perf_trace_stop
from unreal_agent_player.tools.baseline import perf_baseline_save, perf_baseline_compare
from unreal_agent_player.tools.ui import ui_find_window, ui_list_menus, ui_menu_click
from unreal_agent_player.baselines import BaselineStore
from unreal_agent_player.uia import UIADriver
from unreal_agent_player.tools.ui_read import read_viewport_ui
from unreal_agent_player.transport import PythonRemoteExecClient, RemoteControlClient


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
            description="Start Play-In-Editor. Optional map_path and start_location. Returns phase + elapsed.",
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
        Tool(name="pie_stop",   description="Stop PIE.",   inputSchema={"type": "object", "properties": {}, "additionalProperties": False}),
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
            description="Capture the PIE/editor viewport via HighResShot. Returns file path; set inline=true for base64.",
            inputSchema={
                "type": "object",
                "properties": {
                    "resolution": {"type": "string", "default": "1920x1080", "pattern": r"^\d{2,5}x\d{2,5}$"},
                    "hdr": {"type": "boolean", "default": False},
                    "ui": {"type": "boolean", "default": False},
                    "inline": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        ),
        Tool(name="log_tail",
             description="Return the most recent N log lines.",
             inputSchema={"type": "object",
                 "properties": {"lines": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000},
                                "category": {"type": "string", "default": ""},
                                "min_verbosity": {"type": "string", "enum": ["Fatal", "Error", "Warning", "Display", "Log", "Verbose", "VeryVerbose"], "default": "Log"}},
                 "additionalProperties": False}),
        Tool(name="log_since",
             description="Return log lines newer than `cursor`. Use with cursor from a prior call for streaming.",
             inputSchema={"type": "object",
                 "properties": {"cursor": {"type": "integer", "minimum": 0},
                                "max_lines": {"type": "integer", "default": 500, "minimum": 1, "maximum": 5000},
                                "category": {"type": "string", "default": ""},
                                "min_verbosity": {"type": "string", "enum": ["Fatal", "Error", "Warning", "Display", "Log", "Verbose", "VeryVerbose"], "default": "Log"}},
                 "required": ["cursor"], "additionalProperties": False}),
        Tool(name="input_key",
             description="Press or release a key. Optional duration_ms auto-releases.",
             inputSchema={"type":"object",
                 "properties": {"key":{"type":"string"}, "pressed":{"type":"boolean"},
                                 "repeat":{"type":"boolean","default":False},
                                 "duration_ms":{"type":"integer","minimum":1,"maximum":10000}},
                 "required":["key","pressed"], "additionalProperties":False}),
        Tool(name="input_mouse_move",
             description="Move mouse relative (dx,dy) or absolute (x,y, absolute=true).",
             inputSchema={"type":"object",
                 "properties": {"dx":{"type":"number"}, "dy":{"type":"number"},
                                 "x":{"type":"number"},  "y":{"type":"number"},
                                 "absolute":{"type":"boolean","default":False}},
                 "additionalProperties":False}),
        Tool(name="input_mouse_button",
             description="Press or release a mouse button.",
             inputSchema={"type":"object",
                 "properties": {"button":{"type":"string","enum":["left","right","middle","x1","x2"]},
                                 "pressed":{"type":"boolean"}},
                 "required":["button","pressed"], "additionalProperties":False}),
        Tool(name="input_axis",
             description="Inject a named axis value (triggers axis input mappings).",
             inputSchema={"type":"object",
                 "properties": {"axis_name":{"type":"string"}, "value":{"type":"number"}},
                 "required":["axis_name","value"], "additionalProperties":False}),
        Tool(name="input_gamepad",
             description="Press/release or set analog value for a gamepad control.",
             inputSchema={"type":"object",
                 "properties": {"button":{"type":"string"}, "pressed":{"type":"boolean"},
                                 "analog":{"type":"number","default":1.0}},
                 "required":["button","pressed"], "additionalProperties":False}),
        Tool(name="input_sequence",
             description="Run a sequence of input steps with per-step wait_ms. Cheaper than many calls.",
             inputSchema={"type":"object",
                 "properties": {"steps":{"type":"array","items":{"type":"object"}}},
                 "required":["steps"], "additionalProperties":False}),
        Tool(name="input_xr_button",
             description="Press/release a Quest Touch controller button (OculusTouch_* FKey) for a hand. VR (V2).",
             inputSchema={"type":"object",
                 "properties": {"hand":{"type":"string","enum":["Left","Right"]},
                                "key":{"type":"string"}, "pressed":{"type":"boolean"}},
                 "required":["hand","key","pressed"], "additionalProperties":False}),
        Tool(name="input_xr_pose",
             description="Override a motion-controller pose (position cm + orientation pitch/yaw/roll) for a hand. VR (V2).",
             inputSchema={"type":"object",
                 "properties": {"hand":{"type":"string","enum":["Left","Right"]},
                                "position":{"type":"array","items":{"type":"number"},"minItems":3,"maxItems":3},
                                "orientation":{"type":"array","items":{"type":"number"},"minItems":3,"maxItems":3},
                                "tracked":{"type":"boolean","default":True}},
                 "required":["hand","position","orientation"], "additionalProperties":False}),
        Tool(name="input_xr_clear",
             description="Clear the agent pose override for a hand so real devices win again. VR (V2).",
             inputSchema={"type":"object",
                 "properties": {"hand":{"type":"string","enum":["Left","Right"]}},
                 "required":["hand"], "additionalProperties":False}),
        Tool(name="helper_list",
             description="List all AgentTestHelper UFUNCTIONs with arg/return JSON schemas.",
             inputSchema={"type":"object",
                 "properties":{"category":{"type":"string"}},
                 "additionalProperties":False}),
        Tool(name="helper_call",
             description="Invoke an AgentTestHelper by name with JSON args. Returns its result.",
             inputSchema={"type":"object",
                 "properties":{"name":{"type":"string"}, "args":{"type":"object"}},
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
    ]

    handlers: dict[str, ToolHandler] = {
        "bridge_status": lambda **_: bridge_status(rc=rc, py_exec=py_exec),
        "pie_start":  lambda **kw: pie_start(rc=rc, py_exec=py_exec, **kw),
        "pie_stop":   lambda **_:  pie_stop(rc=rc, py_exec=py_exec),
        "pie_pause":  lambda **_:  pie_pause(rc=rc, py_exec=py_exec),
        "pie_resume": lambda **_:  pie_resume(rc=rc, py_exec=py_exec),
        "pie_status": lambda **_:  pie_status(rc=rc, py_exec=py_exec),
        "exec_console": lambda **kw: tool_exec_console(rc=rc, py_exec=py_exec, **kw),
        "exec_python": lambda **kw: tool_exec_python(rc=rc, py_exec=py_exec, **kw),
        "log_tail":  lambda **kw: log_tail(rc=rc, py_exec=py_exec, **kw),
        "log_since": lambda **kw: log_since(rc=rc, py_exec=py_exec, **kw),
        "screenshot_viewport": lambda **kw: screenshot_viewport(rc=rc, py_exec=py_exec, **kw),
        "actor_find":           lambda **kw: actor_find(rc=rc, py_exec=py_exec, **kw),
        "actor_get_properties": lambda **kw: actor_get_properties(rc=rc, py_exec=py_exec, **kw),
        "actor_set_property":   lambda **kw: actor_set_property(rc=rc, py_exec=py_exec, **kw),
        "actor_call_function":  lambda **kw: actor_call_function(rc=rc, py_exec=py_exec, **kw),
        "input_key":          lambda **kw: input_key(rc=rc, py_exec=py_exec, **kw),
        "input_mouse_move":   lambda **kw: input_mouse_move(rc=rc, py_exec=py_exec, **kw),
        "input_mouse_button": lambda **kw: input_mouse_button(rc=rc, py_exec=py_exec, **kw),
        "input_axis":         lambda **kw: input_axis(rc=rc, py_exec=py_exec, **kw),
        "input_gamepad":      lambda **kw: input_gamepad(rc=rc, py_exec=py_exec, **kw),
        "input_sequence":     lambda **kw: input_sequence(rc=rc, py_exec=py_exec, **kw),
        "input_xr_button":    lambda **kw: input_xr_button(rc=rc, py_exec=py_exec, **kw),
        "input_xr_pose":      lambda **kw: input_xr_pose(rc=rc, py_exec=py_exec, **kw),
        "input_xr_clear":     lambda **kw: input_xr_clear(rc=rc, py_exec=py_exec, **kw),
        "helper_list": lambda **kw: helper_list(rc=rc, py_exec=py_exec, **kw),
        "helper_call": lambda **kw: helper_call(rc=rc, py_exec=py_exec, **kw),
        "perf_stat":        lambda **kw: perf_stat(rc=rc, py_exec=py_exec, **kw),
        "perf_trace_start": lambda **kw: perf_trace_start(rc=rc, py_exec=py_exec, **kw),
        "perf_trace_stop":  lambda **kw: perf_trace_stop(rc=rc, py_exec=py_exec, **kw),
        "perf_baseline_save":    lambda **kw: perf_baseline_save(rc=rc, py_exec=py_exec, store=store, **kw),
        "perf_baseline_compare": lambda **kw: perf_baseline_compare(rc=rc, py_exec=py_exec, store=store, **kw),
        "ui_menu_click":  lambda **kw: ui_menu_click(driver=ui_driver, **kw),
        "ui_find_window": lambda **kw: ui_find_window(driver=ui_driver, **kw),
        "ui_list_menus":  lambda **kw: ui_list_menus(driver=ui_driver, **kw),
        "read_viewport_ui": lambda **kw: read_viewport_ui(rc=rc, py_exec=py_exec, **kw),
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
                body = await handler(**arguments)
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
