from __future__ import annotations

import re
import time
from typing import Any

from unreal_agent_player.tools.pie import _LIVE_PHASES, _get_phase
from unreal_agent_player.transport import RemoteControlClient

# Net emulation with no PIE/game world asserts in UnrealEngine.cpp (HandleInvalidWorldContext <-
# NetEmulationHelper::ApplySimulationSettingsOnNetDrivers) and takes the shared editor down
# (2026-11-15 `NetEmulation.Off` after PIE stopped).
_NET_EMULATION_RE = re.compile(
    r"^\s*(netemulation\.|net\s+pkt|pkt(lag|loss|dup|order))",
    re.IGNORECASE)

NET_EMULATION_REFUSAL = ("refused: net emulation needs a live PIE world (crashes editor otherwise). "
                         "Start PIE first; the command was NOT run.")

# Same commands anywhere inside a blob of Python (`execute_console_command("NetEmulation.Off")`).
NET_EMULATION_SCAN_RE = re.compile(
    r"\b(netemulation\.\w+|net\s+pkt\w+|pkt(lag|loss|dup|order)\w*)", re.IGNORECASE)


def is_net_emulation_command(command: str) -> bool:
    return bool(_NET_EMULATION_RE.match(command or ""))


async def exec_console(
    *, rc: RemoteControlClient, py_exec: Any, command: str,
) -> dict[str, Any]:
    t0 = time.monotonic()
    if is_net_emulation_command(command) and await _get_phase(rc) not in _LIVE_PHASES:
        return {"ok": False, "error": NET_EMULATION_REFUSAL, "duration_ms": 0}
    output = await rc.exec_console(command)
    dt_ms = int((time.monotonic() - t0) * 1000)
    return {"ok": True, "output": output, "duration_ms": dt_ms}
