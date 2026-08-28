from __future__ import annotations

import asyncio
import time
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
    # An ack of QUEUED work, said out loud: RequestPlaySession only queues the session and the
    # editor tick creates the play world later, so the phase below is not "the world exists".
    # Poll pie_status until the phase is Playing before reading game state or capturing a frame.
    return {"ok": True, "queued": True, "confirmed": False,
            "phase": await _get_phase(rc), "elapsed": await _get_elapsed(rc)}


PIE_STOP_TIMEOUT_SECONDS = 30.0
_PIE_STOP_POLL_SECONDS = 0.5


async def pie_stop(*, rc: RemoteControlClient, py_exec: Any,
                   timeout: float = PIE_STOP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Stop PIE and do not return ok:true until the world is actually gone.

    Two things this must not do, both of which it used to (see known-issues #25):

    * Call `LevelEditorSubsystem.editor_request_end_play()` directly. That is a NO-OP unless the
      play world already exists, so a stop landing between "start queued" and "play world created"
      did nothing and the queued start then brought PIE up afterwards. The plugin's `stop_pie()`
      CANCELS a queued start first, which serialises the stop against the engine's queued work.
    * Report the phase read immediately after the request. Teardown happens on a later editor tick,
      so that read is an ack of the request, not a result -- poll until the phase settles.
    """
    py_exec.exec_python(
        "import unreal\n"
        "unreal.get_editor_subsystem(unreal.UAPAgentSubsystem).stop_pie()"
    )
    deadline = time.monotonic() + max(0.0, timeout)
    phase = await _get_phase(rc)
    while phase != "NotPlaying" and time.monotonic() < deadline:
        await asyncio.sleep(_PIE_STOP_POLL_SECONDS)
        phase = await _get_phase(rc)
    stopped = phase == "NotPlaying"
    out: dict[str, Any] = {"ok": stopped, "stopped": stopped, "phase": phase}
    if not stopped:
        out["error"] = (f"PIE still in phase {phase!r} {timeout}s after the stop request -- the "
                        "editor is NOT free. Do not hand it to another agent; retry the stop.")
    return out


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
