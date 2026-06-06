"""Read the on-screen UMG layer of the running PIE game.

Lets an agent see prompts/labels (e.g. "Press E to open") and which element is
focused, so it can respond to on-screen UI instead of injecting input blind.
Backed by the plugin's DumpViewportUI UFUNCTION over Remote Control.
"""

from __future__ import annotations

import json
from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import RemoteControlClient, SUBSYSTEM_OBJECT_PATH


async def read_viewport_ui(*, rc: RemoteControlClient, py_exec: Any) -> dict[str, Any]:
    """Return visible UMG text with screen position + focus.

    Shape: {available, count, focused, texts: [{text, x, y, focused}, ...]}.
    `available` is False when there is no active PIE session.
    """
    resp = await rc.call_function(SUBSYSTEM_OBJECT_PATH, "DumpViewportUI", parameters={})
    raw = resp.get("ReturnValue")
    if not raw:
        return {"available": False, "count": 0, "focused": "", "texts": []}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AgentError(
            ErrorCode.UE_UNREACHABLE,
            f"DumpViewportUI returned non-JSON: {raw!r}",
        ) from exc
