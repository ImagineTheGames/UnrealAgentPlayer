from __future__ import annotations

import json
from typing import Any

from unreal_agent_player.transport import SUBSYSTEM_OBJECT_PATH, RemoteControlClient


async def helper_list(
    *, rc: RemoteControlClient, py_exec: Any, category: str | None = None,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    resp = await rc.call_function(_object_path, "ListTestHelpers", parameters={})
    raw = resp.get("ReturnValue") or []
    out = []
    for d in raw:
        if category and not d.get("Category", "").startswith(category):
            continue
        out.append({
            "name": d.get("Name"),
            "category": d.get("Category"),
            "tooltip": d.get("Tooltip"),
            "phase_requirement": d.get("PhaseRequirement"),
            "arg_schema": json.loads(d.get("ArgSchemaJson") or "null"),
            "return_schema": json.loads(d.get("ReturnSchemaJson") or "null"),
            "supported": d.get("bSupported", True),
            "unsupported_reason": d.get("UnsupportedReason", ""),
        })
    return {"ok": True, "helpers": out}


async def helper_call(
    *, rc: RemoteControlClient, py_exec: Any,
    name: str, args: dict[str, Any] | None = None,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    resp = await rc.call_function(
        _object_path, "CallTestHelper",
        parameters={"Name": name, "JsonArgs": json.dumps(args or {})},
    )
    raw = str(resp.get("ReturnValue", "{}"))
    body = json.loads(raw)
    if body.get("ok") is False:
        err = body.get("error", {})
        # Fill in missing envelope fields the C++ side doesn't populate.
        err.setdefault("domain", "ue_side")
        err.setdefault("recoverable", err.get("code") not in {"HELPER_UNKNOWN", "HELPER_UNSUPPORTED_ARG"})
        err.setdefault("retry_hint", None)
        body["error"] = err
    return body
