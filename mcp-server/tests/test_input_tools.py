import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.tools.input import (
    input_gamepad, input_key, input_sequence,
)
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_input_key(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": True})
    rc = RemoteControlClient()
    result = await input_key(rc=rc, py_exec=None, key="W", pressed=True)
    assert result == {"ok": True}
    request = httpx_mock.get_requests()[0]
    assert b"InjectKey" in request.content
    await rc.aclose()


@pytest.mark.asyncio
async def test_input_key_with_duration_auto_releases(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": True})
    httpx_mock.add_response(json={"ReturnValue": True})
    rc = RemoteControlClient()
    result = await input_key(rc=rc, py_exec=None, key="W", pressed=True, duration_ms=10)
    assert result == {"ok": True}
    assert len(httpx_mock.get_requests()) == 2
    await rc.aclose()


@pytest.mark.asyncio
async def test_input_sequence(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(json={"ReturnValue": True})
    rc = RemoteControlClient()
    steps = [
        {"action": "input_key", "args": {"key": "W", "pressed": True}, "wait_ms": 5},
        {"action": "input_key", "args": {"key": "W", "pressed": False}, "wait_ms": 0},
        {"action": "input_mouse_move", "args": {"dx": 10, "dy": 0}, "wait_ms": 0},
    ]
    result = await input_sequence(rc=rc, py_exec=None, steps=steps)
    assert result == {"ok": True}
    assert len(httpx_mock.get_requests()) == 3
    await rc.aclose()


@pytest.mark.asyncio
async def test_input_gamepad_unknown_raises():
    from unreal_agent_player.errors import AgentError, ErrorCode
    rc = RemoteControlClient()
    with pytest.raises(AgentError) as excinfo:
        await input_gamepad(rc=rc, py_exec=None, button="NotAThing", pressed=True)
    assert excinfo.value.code == ErrorCode.SCHEMA_VALIDATION
    await rc.aclose()


@pytest.mark.asyncio
async def test_input_xr_button(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": True})
    rc = RemoteControlClient()
    from unreal_agent_player.tools.input import input_xr_button
    result = await input_xr_button(
        rc=rc, py_exec=None, hand="Left", key="OculusTouch_Left_X_Click", pressed=True)
    assert result == {"ok": True}
    request = httpx_mock.get_requests()[0]
    assert b"InjectXRButton" in request.content
    assert b"OculusTouch_Left_X_Click" in request.content
    await rc.aclose()


@pytest.mark.asyncio
async def test_input_xr_button_unknown_key_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": False})
    from unreal_agent_player.errors import AgentError, ErrorCode
    from unreal_agent_player.tools.input import input_xr_button
    rc = RemoteControlClient()
    with pytest.raises(AgentError) as excinfo:
        await input_xr_button(rc=rc, py_exec=None, hand="Left", key="Bogus_Key", pressed=True)
    assert excinfo.value.code == ErrorCode.INPUT_NO_VIEWPORT
    await rc.aclose()


@pytest.mark.asyncio
async def test_input_xr_pose(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": True})
    rc = RemoteControlClient()
    from unreal_agent_player.tools.input import input_xr_pose
    result = await input_xr_pose(
        rc=rc, py_exec=None, hand="Right",
        position=[10, 0, 120], orientation=[0, 90, 0], tracked=True)
    assert result["ok"] is True
    assert result["applied"] is True
    request = httpx_mock.get_requests()[0]
    assert b"InjectXRControllerPose" in request.content
    assert b"Pitch" in request.content
    await rc.aclose()


@pytest.mark.asyncio
async def test_input_xr_clear(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": True})
    rc = RemoteControlClient()
    from unreal_agent_player.tools.input import input_xr_clear
    result = await input_xr_clear(rc=rc, py_exec=None, hand="Left")
    assert result["ok"] is True
    assert result["cleared"] is True
    request = httpx_mock.get_requests()[0]
    assert b"ClearXRControllerOverride" in request.content
    await rc.aclose()
