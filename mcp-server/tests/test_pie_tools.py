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
async def test_pie_start_blocks_until_the_world_is_live(httpx_mock: HTTPXMock):
    """The MCP twin of `uap pie start`, and it had the same shape: it acked a QUEUED session, so
    a caller could act on -- or walk away from -- a world that did not exist yet. It now waits and
    reports `playing` + `waited_seconds`, the same vocabulary pie_stop reports with."""
    httpx_mock.add_response(json={"ReturnValue": "Starting"})
    httpx_mock.add_response(json={"ReturnValue": "Playing"})
    httpx_mock.add_response(json={"ReturnValue": 0.0})
    rc = RemoteControlClient()
    py = FakePy()
    result = await pie_start(rc=rc, py_exec=py, mode="PlayInViewport")
    assert result["ok"] is True and result["playing"] is True
    assert "waited_seconds" in result
    # The misleading async vocabulary is gone from the path where the world IS live.
    assert "queued" not in result and "confirmed" not in result
    assert any("editor_request_begin_play" in c for c in py.calls)
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_start_that_never_comes_up_fails_instead_of_acking(httpx_mock: HTTPXMock):
    """Never a cheerful ok on timeout: the editor is not idle, the queued session may still create
    a play world afterwards, and the caller is told to stop PIE rather than walk away."""
    httpx_mock.add_response(json={"ReturnValue": "NotPlaying"})   # phase
    httpx_mock.add_response(json={"ReturnValue": 0.0})            # elapsed
    rc = RemoteControlClient()
    result = await pie_start(rc=rc, py_exec=FakePy(), mode="PlayInViewport", timeout=0.0)
    assert result["ok"] is False and result["playing"] is False
    assert result["queued"] is True and result["confirmed"] is False
    assert "pie_stop" in result["error"]
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_start_with_map_and_location(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Playing"})
    httpx_mock.add_response(json={"ReturnValue": 0.0})
    rc = RemoteControlClient()
    py = FakePy()
    await pie_start(rc=rc, py_exec=py, mode="PlayInViewport",
                    map_path="/Game/Maps/Test", start_location=[10, 20, 30])
    assert any("load_map" in c and "/Game/Maps/Test" in c for c in py.calls)
    assert any("10" in c and "20" in c and "30" in c for c in py.calls)
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_stop_waits_for_the_teardown(httpx_mock: HTTPXMock):
    """It must not report the phase read straight after the request -- teardown happens on a later
    editor tick, so that read is an ack of the request, not a result (known-issues #25)."""
    httpx_mock.add_response(json={"ReturnValue": "Ending"})
    httpx_mock.add_response(json={"ReturnValue": "NotPlaying"})
    rc = RemoteControlClient()
    py = FakePy()
    result = await pie_stop(rc=rc, py_exec=py)
    assert result == {"ok": True, "stopped": True, "phase": "NotPlaying"}
    # Through the plugin verb, which CANCELS a queued start; the raw engine call cannot.
    assert any("stop_pie()" in c for c in py.calls)
    assert not any("editor_request_end_play" in c for c in py.calls)
    await rc.aclose()


@pytest.mark.asyncio
async def test_pie_stop_that_never_completes_fails_instead_of_acking(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Playing"}, is_reusable=True)
    rc = RemoteControlClient()
    py = FakePy()
    result = await pie_stop(rc=rc, py_exec=py, timeout=0.0)
    assert result["ok"] is False and result["stopped"] is False
    assert "NOT free" in result["error"]
    await rc.aclose()


@pytest.mark.asyncio
async def test_only_wait_false_labels_itself_as_a_queued_ack(httpx_mock: HTTPXMock):
    """`queued`/`confirmed: false` imply an async job you come back to, which is what agents acted
    on for a 1-5 second call. They are kept only where they are true: the explicit opt-out."""
    httpx_mock.add_response(json={"ReturnValue": "NotPlaying"})
    httpx_mock.add_response(json={"ReturnValue": 0.0})
    rc = RemoteControlClient()
    result = await pie_start(rc=rc, py_exec=FakePy(), mode="PlayInViewport", wait=False)
    assert result["queued"] is True and result["confirmed"] is False
    assert "never leave PIE running unattended" in result["warning"]
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
