import json

import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.tools.ui_read import read_viewport_ui
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_read_viewport_ui_parses_json(httpx_mock: HTTPXMock):
    payload = {
        "available": True,
        "count": 2,
        "focused": "START",
        "texts": [
            {"text": "Press E to open", "x": 960.0, "y": 540.0, "focused": False},
            {"text": "START", "x": 100.0, "y": 700.0, "focused": True},
        ],
    }
    httpx_mock.add_response(json={"ReturnValue": json.dumps(payload)})
    rc = RemoteControlClient()
    result = await read_viewport_ui(rc=rc, py_exec=None)
    assert result["available"] is True
    assert result["count"] == 2
    assert result["focused"] == "START"
    assert result["texts"][0]["text"] == "Press E to open"
    request = httpx_mock.get_requests()[0]
    assert b"DumpViewportUI" in request.content
    await rc.aclose()


@pytest.mark.asyncio
async def test_read_viewport_ui_empty_return_means_unavailable(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": ""})
    rc = RemoteControlClient()
    result = await read_viewport_ui(rc=rc, py_exec=None)
    assert result == {"available": False, "count": 0, "focused": "", "texts": []}
    await rc.aclose()


@pytest.mark.asyncio
async def test_read_viewport_ui_accepts_dict_return(httpx_mock: HTTPXMock):
    payload = {"available": True, "count": 0, "focused": "", "texts": []}
    httpx_mock.add_response(json={"ReturnValue": payload})
    rc = RemoteControlClient()
    result = await read_viewport_ui(rc=rc, py_exec=None)
    assert result == payload
    await rc.aclose()


@pytest.mark.asyncio
async def test_read_viewport_ui_non_json_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "not json {"})
    rc = RemoteControlClient()
    with pytest.raises(AgentError) as excinfo:
        await read_viewport_ui(rc=rc, py_exec=None)
    assert excinfo.value.code == ErrorCode.UE_UNREACHABLE
    await rc.aclose()
