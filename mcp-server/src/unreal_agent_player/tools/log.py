from __future__ import annotations

import json
from typing import Any

from unreal_agent_player.transport import SUBSYSTEM_OBJECT_PATH, RemoteControlClient


async def _call_logs_since(
    rc: RemoteControlClient, after_cursor: int, max_lines: int,
    category: str, min_verbosity: str,
    object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    resp = await rc.call_function(
        object_path, "GetLogsSince",
        parameters={
            "AfterCursor": after_cursor,
            "MaxLines": max_lines,
            "CategoryFilter": category,
            "MinVerbosity": min_verbosity,
        },
    )
    raw = str(resp.get("ReturnValue", "{}"))
    return json.loads(raw)


async def log_since(
    *, rc: RemoteControlClient, py_exec: Any,
    cursor: int, max_lines: int = 500,
    category: str = "", min_verbosity: str = "Log",
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    body = await _call_logs_since(rc, cursor, max_lines, category, min_verbosity, _object_path)
    return {"ok": True, "cursor": body.get("cursor", cursor), "lines": body.get("lines", [])}


async def log_tail(
    *, rc: RemoteControlClient, py_exec: Any,
    lines: int = 200, category: str = "", min_verbosity: str = "Log",
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    cur_resp = await rc.call_function(_object_path, "GetLogCursor", parameters={})
    current = int(cur_resp.get("ReturnValue", 0))
    after = max(0, current - lines)
    body = await _call_logs_since(rc, after, lines, category, min_verbosity, _object_path)
    return {"ok": True, "cursor": body.get("cursor", current), "lines": body.get("lines", [])}
