from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.throttle import throttle_annotation
from unreal_agent_player.transport import SUBSYSTEM_OBJECT_PATH, RemoteControlClient

_UNIT_RE = re.compile(r"(Frame|Game|Draw|GPU):\s*([0-9.]+)\s*ms", re.IGNORECASE)
_FPS_RE = re.compile(r"FPS:\s*([0-9.]+)", re.IGNORECASE)


def _parse_unit(text: str) -> dict[str, float]:
    out = {}
    for m in _UNIT_RE.finditer(text):
        out[m.group(1).lower() + "_ms"] = float(m.group(2))
    return out


def _parse_fps(text: str) -> dict[str, float]:
    m = _FPS_RE.search(text)
    return {"fps": float(m.group(1))} if m else {}


async def perf_stat(
    *, rc: RemoteControlClient, py_exec: Any, stat_group: str = "unit",
) -> dict[str, Any]:
    if stat_group not in ("unit", "fps"):
        raise AgentError(
            ErrorCode.SCHEMA_VALIDATION,
            f"stat_group {stat_group!r} not implemented in v1 (supported: unit, fps)",
            recoverable=False,
        )
    resp = await rc.call_function(
        SUBSYSTEM_OBJECT_PATH, "GetStatGroupText", parameters={"GroupName": stat_group}
    )
    raw = str(resp.get("ReturnValue", ""))
    parsed = _parse_unit(raw) if stat_group == "unit" else _parse_fps(raw)
    # An implausibly low rate is the editor's not-foreground throttle, not the game's
    # performance. Explain it on the result itself -- the caller is looking at the number,
    # not at the docs. Annotations sit beside `parsed`, never inside it, so baseline
    # comparison keeps seeing a dict of pure numbers.
    return {"ok": True, "parsed": parsed, "raw": raw, **throttle_annotation(parsed)}


_TRACE_PATH = os.path.join(tempfile.gettempdir(), "uap_trace.utrace")


async def perf_trace_start(
    *, rc: RemoteControlClient, py_exec: Any, channels: list[str] | None = None,
) -> dict[str, Any]:
    chans = ",".join(channels or ["cpu", "gpu", "frame"])
    await rc.exec_console(f'Trace.Start File "{_TRACE_PATH}" {chans}')
    return {"ok": True, "trace_id": "uap_trace"}


async def perf_trace_stop(
    *, rc: RemoteControlClient, py_exec: Any, trace_id: str,
) -> dict[str, Any]:
    await rc.exec_console("Trace.Stop")
    return {"ok": True, "utrace_path": _TRACE_PATH,
            "summary": {"avg_ms": None, "p99_ms": None, "hitches": None}}
