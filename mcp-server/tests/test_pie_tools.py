import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.tools.pie import pie_pause, pie_resume, pie_start, pie_status, pie_stop
from unreal_agent_player.transport import RemoteControlClient


class FakePy:
    def __init__(self):
        self.calls: list[str] = []
    def exec_python(self, code, unattended=True):
        self.calls.append(code)
        return {"result": "success", "output": []}


@pytest.mark.asyncio
async def test_pie_status(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Playing"})
    httpx_mock.add_response(json={"ReturnValue": 12.4})
    rc = RemoteControlClient()
    result = await pie_status(rc=rc, py_exec=None)
    assert result == {"ok": True, "phase": "Playing", "elapsed": 12.4}
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_start_runs_python_and_reads_phase(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Playing"})
    httpx_mock.add_response(json={"ReturnValue": 0.0})
    rc = RemoteControlClient()
    py = FakePy()
    result = await pie_start(rc=rc, py_exec=py, mode="PlayInViewport")
    assert result["ok"] is True
    assert any("editor_request_begin_play" in c for c in py.calls)
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_start_with_map_and_location(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Starting"})
    httpx_mock.add_response(json={"ReturnValue": 0.0})
    rc = RemoteControlClient()
    py = FakePy()
    await pie_start(rc=rc, py_exec=py, mode="PlayInViewport",
                    map_path="/Game/Maps/Test", start_location=[10, 20, 30])
    assert any("load_map" in c and "/Game/Maps/Test" in c for c in py.calls)
    assert any("10" in c and "20" in c and "30" in c for c in py.calls)
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_stop(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Ending"})
    rc = RemoteControlClient()
    py = FakePy()
    result = await pie_stop(rc=rc, py_exec=py)
    assert result == {"ok": True, "phase": "Ending"}
    assert any("editor_request_end_play" in c for c in py.calls)
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_pause(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Paused"})
    rc = RemoteControlClient()
    py = FakePy()
    result = await pie_pause(rc=rc, py_exec=py)
    assert result == {"ok": True, "phase": "Paused"}
    assert any("set_game_paused" in c and "True" in c for c in py.calls)
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_resume(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Playing"})
    rc = RemoteControlClient()
    py = FakePy()
    result = await pie_resume(rc=rc, py_exec=py)
    assert result == {"ok": True, "phase": "Playing"}
    assert any("set_game_paused" in c and "False" in c for c in py.calls)
    await rc.aclose()
