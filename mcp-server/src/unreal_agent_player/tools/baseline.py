from __future__ import annotations

from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import RemoteControlClient, SUBSYSTEM_OBJECT_PATH
from unreal_agent_player.tools.perf import _parse_unit


def compare_metrics(
    *, baseline: dict[str, float], current: dict[str, float], tolerance_pct: float
) -> dict[str, Any]:
    """Compare current perf metrics against a baseline.

    A metric regresses when current exceeds baseline by more than tolerance_pct
    (higher = worse for frame/ms timings). Metrics absent from current are skipped.
    """
    deltas: dict[str, Any] = {}
    regressions: list[str] = []
    for metric, base_val in baseline.items():
        if metric not in current or base_val == 0:
            continue
        cur_val = current[metric]
        pct = (cur_val - base_val) / base_val * 100.0
        deltas[metric] = {"baseline": base_val, "current": cur_val, "pct": pct}
        if pct > tolerance_pct:
            regressions.append(metric)
    return {"regressed": bool(regressions), "regressions": regressions, "deltas": deltas}


async def _fetch_metrics(rc: RemoteControlClient, stat_group: str) -> dict[str, float]:
    resp = await rc.call_function(
        SUBSYSTEM_OBJECT_PATH, "GetStatGroupText", parameters={"GroupName": stat_group}
    )
    return _parse_unit(str(resp.get("ReturnValue", "")))


async def perf_baseline_save(
    *, rc: RemoteControlClient, py_exec: Any = None, store,
    name: str, stat_group: str = "unit",
) -> dict[str, Any]:
    metrics = await _fetch_metrics(rc, stat_group)
    store.save(name, metrics)
    return {"ok": True, "name": name, "metrics": metrics}


async def perf_baseline_compare(
    *, rc: RemoteControlClient, py_exec: Any = None, store,
    name: str, stat_group: str = "unit", tolerance_pct: float = 10.0,
) -> dict[str, Any]:
    baseline = store.load(name)
    if baseline is None:
        raise AgentError(
            ErrorCode.ACTOR_NOT_FOUND, f"no baseline named '{name}'", recoverable=False
        )
    current = await _fetch_metrics(rc, stat_group)
    cmp = compare_metrics(baseline=baseline, current=current, tolerance_pct=tolerance_pct)
    return {"ok": True, **cmp}
