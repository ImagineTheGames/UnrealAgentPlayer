from __future__ import annotations

import time
from typing import Any

from unreal_agent_player.transport import RemoteControlClient


async def exec_console(
    *, rc: RemoteControlClient, py_exec: Any, command: str,
) -> dict[str, Any]:
    t0 = time.monotonic()
    output = await rc.exec_console(command)
    dt_ms = int((time.monotonic() - t0) * 1000)
    return {"ok": True, "output": output, "duration_ms": dt_ms}
