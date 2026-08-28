from __future__ import annotations

from typing import Any

from unreal_agent_player.errors import AgentError
from unreal_agent_player.transport import SUBSYSTEM_OBJECT_PATH, RemoteControlClient


async def bridge_status(*, rc: RemoteControlClient, py_exec: Any) -> dict[str, Any]:
    rc_reachable = False
    plugin_version: str | None = None
    pie_phase: str | None = None  # populated later, in Phase 5
    try:
        resp = await rc.call_function(SUBSYSTEM_OBJECT_PATH, "GetPluginVersion", parameters={})
        plugin_version = str(resp.get("ReturnValue", ""))
        rc_reachable = True
        try:
            phase_resp = await rc.call_function(
                SUBSYSTEM_OBJECT_PATH, "GetPIEPhase", parameters={}
            )
            pie_phase = str(phase_resp.get("ReturnValue", ""))
        except AgentError:
            pie_phase = None
    except AgentError:
        pass

    remote_exec_reachable = False
    try:
        endpoint = py_exec._discover_endpoint()
        remote_exec_reachable = endpoint is not None
    except Exception:
        pass

    return {
        "ok": True,
        "ue_running": rc_reachable or remote_exec_reachable,
        "rc_reachable": rc_reachable,
        "remote_exec_reachable": remote_exec_reachable,
        "plugin_version": plugin_version,
        "pie_phase": pie_phase,
    }
