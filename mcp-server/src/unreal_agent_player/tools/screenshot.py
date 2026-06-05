from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
import time
from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import RemoteControlClient


_RES_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")


def _parse_resolution(res: str) -> tuple[int, int]:
    m = _RES_RE.match(res.strip())
    if not m:
        raise AgentError(ErrorCode.SCHEMA_VALIDATION, f"Bad resolution: {res!r}", recoverable=False)
    return int(m.group(1)), int(m.group(2))


async def screenshot_viewport(
    *, rc: RemoteControlClient, py_exec: Any,
    resolution: str = "1920x1080",
    hdr: bool = False,
    ui: bool = False,
    inline: bool = False,
    _output_path_override: str | None = None,
) -> dict[str, Any]:
    width, height = _parse_resolution(resolution)

    if _output_path_override:
        out_path = _output_path_override
    else:
        out_path = os.path.join(
            tempfile.gettempdir(),
            f"uap_{int(time.time() * 1000)}.png",
        )

    flags: list[str] = []
    if ui:  flags.append("ForceUISharingLayer=1")
    if hdr: flags.append("HDR=1")
    cmd = f"HighResShot {width}x{height} filename=\"{out_path}\" {' '.join(flags)}".strip()
    await rc.exec_console(cmd)

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            break
        await asyncio.sleep(0.05)
    else:
        raise AgentError(
            ErrorCode.TIMEOUT, f"Screenshot not written within 8s: {out_path}",
        )

    size_bytes = os.path.getsize(out_path)
    body: dict[str, Any] = {
        "ok": True, "path": out_path,
        "width": width, "height": height, "size_bytes": size_bytes,
    }
    if inline:
        with open(out_path, "rb") as f:
            body["image_base64"] = base64.b64encode(f.read()).decode("ascii")
            body["image_mime"] = "image/png"
    return body
