import httpx
import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.tools.status import bridge_status
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_bridge_status_all_good(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call",
        method="PUT",
        json={"ReturnValue": "0.0.1"},
    )
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call",
        method="PUT",
        json={"ReturnValue": "NotPlaying"},
    )
    rc = RemoteControlClient()

    class FakePyExec:
        def _discover_endpoint(self):
            return ("127.0.0.1", 12345)

    result = await bridge_status(rc=rc, py_exec=FakePyExec())
    assert result["ok"] is True
    assert result["ue_running"] is True
    assert result["rc_reachable"] is True
    assert result["remote_exec_reachable"] is True
    assert result["plugin_version"] == "0.0.1"
    assert result["pie_phase"] == "NotPlaying"
    await rc.aclose()


@pytest.mark.asyncio
async def test_bridge_status_rc_down(httpx_mock: HTTPXMock):
    # Use httpx.ConnectError, not bare Exception. The RC client only catches
    # httpx-family exceptions since Phase 2 fixes.
    httpx_mock.add_exception(httpx.ConnectError("refused"))
    rc = RemoteControlClient()

    class FakePyExec:
        def _discover_endpoint(self):
            return None

    result = await bridge_status(rc=rc, py_exec=FakePyExec())
    assert result["ok"] is True
    assert result["rc_reachable"] is False
    assert result["remote_exec_reachable"] is False
    await rc.aclose()
