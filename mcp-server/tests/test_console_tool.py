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
