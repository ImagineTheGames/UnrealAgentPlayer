import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.tools.perf import perf_stat
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_perf_stat_unit_parses(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={"ReturnValue": "Frame: 8.21 ms\nGame: 3.02 ms\nDraw: 2.45 ms\nGPU: 4.80 ms"}
    )
    rc = RemoteControlClient()
    result = await perf_stat(rc=rc, py_exec=None, stat_group="unit")
    assert result["ok"] is True
    p = result["parsed"]
    assert round(p["frame_ms"], 2) == 8.21
    assert round(p["game_ms"], 2) == 3.02
    assert round(p["draw_ms"], 2) == 2.45
    assert round(p["gpu_ms"], 2) == 4.80
    await rc.aclose()


@pytest.mark.asyncio
async def test_perf_stat_fps_parses(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "FPS: 120.5"})
    rc = RemoteControlClient()
    result = await perf_stat(rc=rc, py_exec=None, stat_group="fps")
    assert result["parsed"]["fps"] == 120.5
    await rc.aclose()


@pytest.mark.asyncio
async def test_perf_stat_unknown_group_raises():
    from unreal_agent_player.errors import AgentError, ErrorCode
    rc = RemoteControlClient()
    with pytest.raises(AgentError) as excinfo:
        await perf_stat(rc=rc, py_exec=None, stat_group="memory")
    assert excinfo.value.code == ErrorCode.SCHEMA_VALIDATION
    await rc.aclose()
