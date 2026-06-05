from __future__ import annotations

from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import RemoteControlClient, SUBSYSTEM_OBJECT_PATH


async def _get_phase(rc: RemoteControlClient) -> str:
    resp = await rc.call_function(SUBSYSTEM_OBJECT_PATH, "GetPIEPhase", parameters={})
    return str(resp.get("ReturnValue", "NotPlaying"))


async def _get_elapsed(rc: RemoteControlClient) -> float:
    resp = await rc.call_function(SUBSYSTEM_OBJECT_PATH, "GetPIEElapsedSeconds", parameters={})
    return float(resp.get("ReturnValue", 0.0))


async def pie_status(*, rc: RemoteControlClient, py_exec: Any) -> dict[str, Any]:
    return {"ok": True, "phase": await _get_phase(rc), "elapsed": await _get_elapsed(rc)}


async def pie_start(
    *,
    rc: RemoteControlClient,
    py_exec: Any,
    mode: str = "PlayInViewport",
    map_path: str | None = None,
    start_location: list[float] | None = None,
) -> dict[str, Any]:
    if mode not in {"PlayInViewport", "PlayInNewWindow", "Simulate"}:
        raise AgentError(
            ErrorCode.SCHEMA_VALIDATION, f"Unknown PIE mode: {mode}", recoverable=False
        )
    lines = ["import unreal"]
    if map_path:
        lines.append(f"unreal.EditorLoadingAndSavingUtils.load_map('{map_path}')")
    if start_location:
        sx, sy, sz = start_location
        lines.append(
            "ps = unreal.get_default_object(unreal.LevelEditorPlaySettings)\n"
            f"ps.set_editor_property('custom_start_location', unreal.Vector({sx},{sy},{sz}))\n"
            "ps.set_editor_property('play_from_here', True)"
        )
    lines.append("_les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)")
    if mode == "Simulate":
        lines.append("_les.editor_play_simulate()")
    else:
        # UE 5.7: editor_play() does not exist; editor_request_begin_play() starts PIE.
        lines.append("_les.editor_request_begin_play()")
    py_exec.exec_python("\n".join(lines))
    return {"ok": True, "phase": await _get_phase(rc), "elapsed": await _get_elapsed(rc)}


async def pie_stop(*, rc: RemoteControlClient, py_exec: Any) -> dict[str, Any]:
    py_exec.exec_python(
        "import unreal\n"
        "unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_request_end_play()"
    )
    return {"ok": True, "phase": await _get_phase(rc)}


async def pie_pause(*, rc: RemoteControlClient, py_exec: Any) -> dict[str, Any]:
    py_exec.exec_python(
        "import unreal\n"
        "w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()\n"
        "if w:\n"
        "    unreal.GameplayStatics.set_game_paused(w, True)"
    )
    return {"ok": True, "phase": await _get_phase(rc)}


async def pie_resume(*, rc: RemoteControlClient, py_exec: Any) -> dict[str, Any]:
    py_exec.exec_python(
        "import unreal\n"
        "w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()\n"
        "if w:\n"
        "    unreal.GameplayStatics.set_game_paused(w, False)"
    )
    return {"ok": True, "phase": await _get_phase(rc)}
