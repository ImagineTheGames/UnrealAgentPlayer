from __future__ import annotations

import asyncio
import time
from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import SUBSYSTEM_OBJECT_PATH, RemoteControlClient


async def _get_phase(rc: RemoteControlClient) -> str:
    resp = await rc.call_function(SUBSYSTEM_OBJECT_PATH, "GetPIEPhase", parameters={})
    return str(resp.get("ReturnValue", "NotPlaying"))


async def _get_elapsed(rc: RemoteControlClient) -> float:
    resp = await rc.call_function(SUBSYSTEM_OBJECT_PATH, "GetPIEElapsedSeconds", parameters={})
    return float(resp.get("ReturnValue", 0.0))


async def pie_status(*, rc: RemoteControlClient, py_exec: Any) -> dict[str, Any]:
    return {"ok": True, "phase": await _get_phase(rc), "elapsed": await _get_elapsed(rc)}


PIE_START_TIMEOUT_SECONDS = 60.0
_PIE_START_POLL_SECONDS = 0.5
# A LIVE play world. Paused counts: the world exists, which is what "did the start take?" asks.
_LIVE_PHASES = {"Playing", "Paused"}


async def pie_start(
    *,
    rc: RemoteControlClient,
    py_exec: Any,
    mode: str = "PlayInViewport",
    map_path: str | None = None,
    start_location: list[float] | None = None,
    wait: bool = True,
    timeout: float = PIE_START_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Start PIE and, by DEFAULT, block until the play world is live.

    The CLI twin (`uap pie start`) had the identical shape and the identical incident: it returned
    the instant the session was QUEUED, labelled `queued: true, confirmed: false`. Those fields
    were accurate and still wrong -- they imply an ASYNC JOB, so callers registered watchers and
    ended their turns over an operation that takes 1-5 seconds, leaving PIE live and unattended
    three times in one day (2026-08-28, docs/known-issues.md #29). A hint printed beside those
    fields did not help; the DEFAULT had to change.

    So the blocking path reports `playing: true` + `waited_seconds`, the same shape `pie_stop`
    reports with. The async vocabulary survives only under `wait=False` and on the timeout path,
    where a queued session really is outstanding.
    """
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
    t0 = time.monotonic()
    py_exec.exec_python("\n".join(lines))
    if not wait:
        # Opt-in fire-and-forget. RequestPlaySession only QUEUES the session and the editor tick
        # creates the play world later, so the phase below is not "the world exists" -- which is
        # exactly why this is no longer the default.
        return {"ok": True, "queued": True, "confirmed": False,
                "phase": await _get_phase(rc), "elapsed": await _get_elapsed(rc),
                "warning": ("wait=False: an ack of a QUEUED start, not a live world. Poll "
                            "pie_status until the phase is Playing before reading game state or "
                            "capturing a frame, and never leave PIE running unattended.")}
    deadline = t0 + max(0.0, timeout)
    phase = await _get_phase(rc)
    while phase not in _LIVE_PHASES and time.monotonic() < deadline:
        await asyncio.sleep(_PIE_START_POLL_SECONDS)
        phase = await _get_phase(rc)
    playing = phase in _LIVE_PHASES
    out: dict[str, Any] = {"ok": playing, "playing": playing, "phase": phase,
                           "elapsed": await _get_elapsed(rc),
                           "waited_seconds": round(time.monotonic() - t0, 2)}
    if not playing:
        out["queued"] = True
        out["confirmed"] = False
        out["error"] = (
            f"PIE was requested but the play world was not live {timeout}s later -- the phase is "
            f"still {phase!r}. A start is QUEUED work, so it may come up AFTER this returns: the "
            "editor is NOT idle and NOT safe to walk away from. Call pie_stop (it cancels a "
            "queued start and confirms the teardown), then retry, or raise `timeout` if this map "
            "is simply slow to load. A normal start is 1-5s."
        )
    return out


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
