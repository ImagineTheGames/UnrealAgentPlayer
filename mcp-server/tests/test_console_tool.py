import re

import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.tools.console import exec_console
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_exec_console_returns_output(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/preset/UAP_Preset/function/ExecuteConsoleCommand", method="PUT",
        json={"ReturnedValues": [{"ReturnValue": "Frame: 8.2 ms"}]},
    )
    rc = RemoteControlClient()
    result = await exec_console(rc=rc, py_exec=None, command="stat unit")
    assert result["ok"] is True
    assert result["output"] == "Frame: 8.2 ms"
    assert result["duration_ms"] >= 0
    await rc.aclose()


def _phase(httpx_mock: HTTPXMock, phase: str):
    httpx_mock.add_response(url=re.compile(r".*/remote/object/call$"), json={"ReturnValue": phase})


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["NetEmulation.Off", "net PktLag=100", "PktLoss=5", "netemulation.PktLag 50"])
async def test_net_emulation_refused_without_pie(httpx_mock: HTTPXMock, cmd):
    _phase(httpx_mock, "NotPlaying")  # no ExecuteConsoleCommand mock: it must not be sent
    rc = RemoteControlClient()
    result = await exec_console(rc=rc, py_exec=None, command=cmd)
    assert result["ok"] is False
    assert "needs a live PIE world" in result["error"]
    await rc.aclose()


@pytest.mark.asyncio
async def test_net_emulation_allowed_with_pie(httpx_mock: HTTPXMock):
    _phase(httpx_mock, "Playing")
    httpx_mock.add_response(method="PUT", url=re.compile(r".*/function/ExecuteConsoleCommand$"),
                            json={"ReturnedValues": [{"ReturnValue": ""}]})
    rc = RemoteControlClient()
    result = await exec_console(rc=rc, py_exec=None, command="NetEmulation.Off")
    assert result["ok"] is True
    await rc.aclose()


@pytest.mark.asyncio
async def test_open_refused_without_pie(httpx_mock: HTTPXMock):
    _phase(httpx_mock, "NotPlaying")
    rc = RemoteControlClient()
    result = await exec_console(rc=rc, py_exec=None, command="open /Game/Maps/X")
    assert result["ok"] is False and "live PIE world" in result["error"]
    await rc.aclose()
