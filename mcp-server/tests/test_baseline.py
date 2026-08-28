import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.baselines import BaselineStore
from unreal_agent_player.tools.baseline import (
    compare_metrics,
    perf_baseline_compare,
    perf_baseline_save,
)
from unreal_agent_player.transport import RemoteControlClient

# --- BaselineStore ---

def test_save_and_load_roundtrip(tmp_path):
    store = BaselineStore(tmp_path / "baselines.json")
    store.save("arena_idle", {"frame_ms": 8.2, "gpu_ms": 6.1})
    assert store.load("arena_idle") == {"frame_ms": 8.2, "gpu_ms": 6.1}


def test_load_missing_returns_none(tmp_path):
    store = BaselineStore(tmp_path / "b.json")
    assert store.load("nope") is None


def test_list_names(tmp_path):
    store = BaselineStore(tmp_path / "b.json")
    store.save("a", {"frame_ms": 1.0})
    store.save("b", {"frame_ms": 2.0})
    assert sorted(store.list_names()) == ["a", "b"]


# --- compare_metrics ---

def test_compare_within_tolerance_passes():
    result = compare_metrics(
        baseline={"frame_ms": 8.0, "gpu_ms": 6.0},
        current={"frame_ms": 8.3, "gpu_ms": 6.1},
        tolerance_pct=10.0)
    assert result["regressed"] is False
    assert result["deltas"]["frame_ms"]["pct"] == pytest.approx(3.75, abs=0.01)


def test_compare_over_tolerance_flags_regression():
    result = compare_metrics(
        baseline={"frame_ms": 8.0}, current={"frame_ms": 10.0}, tolerance_pct=10.0)
    assert result["regressed"] is True
    assert "frame_ms" in result["regressions"]


def test_compare_missing_metric_ignored():
    result = compare_metrics(
        baseline={"frame_ms": 8.0, "gpu_ms": 6.0},
        current={"frame_ms": 8.1},
        tolerance_pct=10.0)
    assert result["regressed"] is False
    assert "gpu_ms" not in result["deltas"]


# --- tools ---

@pytest.mark.asyncio
async def test_perf_baseline_save_records(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(json={"ReturnValue": "Frame: 8.20 ms\nGame: 3.10 ms\nDraw: 5.00 ms\nGPU: 6.00 ms"})
    rc = RemoteControlClient()
    store = BaselineStore(tmp_path / "b.json")
    result = await perf_baseline_save(rc=rc, store=store, name="idle")
    assert result["ok"] is True
    assert result["metrics"]["frame_ms"] == 8.2
    assert store.load("idle")["gpu_ms"] == 6.0
    await rc.aclose()


@pytest.mark.asyncio
async def test_perf_baseline_compare_no_baseline_raises(tmp_path):
    from unreal_agent_player.errors import AgentError, ErrorCode
    rc = RemoteControlClient()
    store = BaselineStore(tmp_path / "b.json")
    with pytest.raises(AgentError) as excinfo:
        await perf_baseline_compare(rc=rc, store=store, name="missing")
    assert excinfo.value.code == ErrorCode.ACTOR_NOT_FOUND
    await rc.aclose()


@pytest.mark.asyncio
async def test_perf_baseline_compare_detects_regression(httpx_mock: HTTPXMock, tmp_path):
    store = BaselineStore(tmp_path / "b.json")
    store.save("idle", {"frame_ms": 8.0, "gpu_ms": 6.0})
    httpx_mock.add_response(json={"ReturnValue": "Frame: 12.00 ms\nGame: 3.10 ms\nDraw: 5.00 ms\nGPU: 6.10 ms"})
    rc = RemoteControlClient()
    result = await perf_baseline_compare(rc=rc, store=store, name="idle", tolerance_pct=10.0)
    assert result["ok"] is True
    assert result["regressed"] is True
    assert "frame_ms" in result["regressions"]
    await rc.aclose()
