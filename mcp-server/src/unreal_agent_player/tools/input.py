from __future__ import annotations

import asyncio
from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import SUBSYSTEM_OBJECT_PATH, RemoteControlClient


async def _call(rc: RemoteControlClient, fn: str, params: dict[str, Any],
               object_path: str = SUBSYSTEM_OBJECT_PATH) -> bool:
    resp = await rc.call_function(object_path, fn, parameters=params)
    val = resp.get("ReturnValue")
    if val is False:
        # A bool cannot carry a reason. The plugin logs the real one (no live viewport, or a
        # Slate user index nothing is registered on), so point at it rather than asserting the
        # first guess -- a guessed reason once sent a live investigation the wrong way entirely.
        raise AgentError(
            ErrorCode.INPUT_NO_VIEWPORT,
            f"{fn} returned false. Usually no active PIE viewport; it is also what a "
            f"slate_user Slate has no user for gives. The plugin logged the actual reason -- "
            f"read it with `uap log since <cursor> --grep {fn}`.")
    return True


async def input_key(
    *, rc: RemoteControlClient, py_exec: Any,
    key: str, pressed: bool, repeat: bool = False, duration_ms: int | None = None,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    await _call(rc, "InjectKey", {"KeyName": key, "bPressed": pressed, "bRepeat": repeat}, _object_path)
    if duration_ms and pressed:
        await asyncio.sleep(duration_ms / 1000.0)
        await _call(rc, "InjectKey", {"KeyName": key, "bPressed": False, "bRepeat": False}, _object_path)
    return {"ok": True}


async def input_mouse_move(
    *, rc: RemoteControlClient, py_exec: Any,
    dx: float | None = None, dy: float | None = None,
    x: float | None = None, y: float | None = None, absolute: bool = False,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    if absolute:
        if x is None or y is None:
            raise AgentError(ErrorCode.SCHEMA_VALIDATION, "absolute=True requires x,y", recoverable=False)
        await _call(rc, "InjectMouseMove", {"X": x, "Y": y, "bAbsolute": True}, _object_path)
    else:
        if dx is None or dy is None:
            raise AgentError(ErrorCode.SCHEMA_VALIDATION, "relative move requires dx,dy", recoverable=False)
        await _call(rc, "InjectMouseMove", {"X": dx, "Y": dy, "bAbsolute": False}, _object_path)
    return {"ok": True}


async def input_mouse_button(
    *, rc: RemoteControlClient, py_exec: Any, button: str, pressed: bool,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    btn_map = {"left": "Left", "right": "Right", "middle": "Middle", "x1": "XButton1", "x2": "XButton2"}
    if button not in btn_map:
        raise AgentError(ErrorCode.SCHEMA_VALIDATION, f"Bad mouse button: {button}", recoverable=False)
    await _call(rc, "InjectMouseButton", {"Button": btn_map[button], "bPressed": pressed}, _object_path)
    return {"ok": True}


async def input_axis(
    *, rc: RemoteControlClient, py_exec: Any, axis_name: str, value: float,
    slate_user: int | None = None,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    """slate_user routes the sample through Slate as that user -- the only route an analog or
    virtual cursor (anything on RegisterInputPreProcessor) can see, and Slate DISCARDS an event
    whose user index does not match the handler's owner. Omit it for the game-viewport route
    (gameplay / Enhanced Input), which is the historical behaviour."""
    params: dict[str, Any] = {"AxisName": axis_name, "Value": value}
    if slate_user is not None:
        params["SlateUser"] = str(slate_user)
    await _call(rc, "InjectAxis", params, _object_path)
    return {"ok": True, "route": "slate" if slate_user is not None else "viewport"}


_GAMEPAD_BUTTONS = {
    "FaceBottom", "FaceRight", "FaceLeft", "FaceTop",
    "ShoulderLeft", "ShoulderRight",
    "TriggerLeft", "TriggerRight",
    "ThumbLeft", "ThumbRight",
    "DPadUp", "DPadDown", "DPadLeft", "DPadRight",
    "Start", "Back", "Special",
    "LeftStickX", "LeftStickY", "RightStickX", "RightStickY",
}


async def input_gamepad(
    *, rc: RemoteControlClient, py_exec: Any,
    button: str, pressed: bool, analog: float = 1.0, slate_user: int | None = None,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    if button not in _GAMEPAD_BUTTONS:
        raise AgentError(
            ErrorCode.SCHEMA_VALIDATION, f"Unknown gamepad button: {button!r}",
            recoverable=False,
        )
    params: dict[str, Any] = {"Button": button, "bPressed": pressed, "AnalogValue": analog}
    if slate_user is not None:
        params["SlateUser"] = str(slate_user)   # see input_axis
    await _call(rc, "InjectGamepad", params, _object_path)
    return {"ok": True}


async def input_xr_button(
    *, rc: RemoteControlClient, py_exec: Any = None,
    hand: str, key: str, pressed: bool,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    # Quest Touch buttons (OculusTouch_*) are regular FKeys; route via the Slate path.
    await _call(rc, "InjectXRButton", {"Hand": hand, "ButtonKeyName": key, "bPressed": pressed}, _object_path)
    return {"ok": True}


async def input_xr_pose(
    *, rc: RemoteControlClient, py_exec: Any = None,
    hand: str, position: list[float], orientation: list[float], tracked: bool = True,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    pos = {"X": position[0], "Y": position[1], "Z": position[2]}
    rot = {"Pitch": orientation[0], "Yaw": orientation[1], "Roll": orientation[2]}
    resp = await rc.call_function(
        _object_path, "InjectXRControllerPose",
        parameters={"Hand": hand, "Position": pos, "Orientation": rot, "bTracked": tracked},
    )
    return {"ok": True, "applied": bool(resp.get("ReturnValue", False))}


async def input_xr_clear(
    *, rc: RemoteControlClient, py_exec: Any = None, hand: str,
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    resp = await rc.call_function(
        _object_path, "ClearXRControllerOverride", parameters={"Hand": hand}
    )
    return {"ok": True, "cleared": bool(resp.get("ReturnValue", False))}


_HANDLERS = {
    "input_key": input_key,
    "input_mouse_move": input_mouse_move,
    "input_mouse_button": input_mouse_button,
    "input_axis": input_axis,
    "input_gamepad": input_gamepad,
    "input_xr_button": input_xr_button,
}


async def input_sequence(
    *, rc: RemoteControlClient, py_exec: Any, steps: list[dict[str, Any]],
    _object_path: str = SUBSYSTEM_OBJECT_PATH,
) -> dict[str, Any]:
    for step in steps:
        action = step.get("action")
        args = step.get("args", {})
        wait_ms = step.get("wait_ms", 0)
        handler = _HANDLERS.get(action)
        if handler is None:
            raise AgentError(
                ErrorCode.SCHEMA_VALIDATION, f"Unknown action in sequence: {action}",
                recoverable=False,
            )
        await handler(rc=rc, py_exec=py_exec, _object_path=_object_path, **args)
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000.0)
    return {"ok": True}
