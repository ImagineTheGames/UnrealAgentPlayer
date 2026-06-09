import base64
import os
import tempfile
from typing import Any

import pytest

from unreal_agent_player.tools.screenshot import screenshot_viewport


class FakeRC:
    def __init__(self, tmp_file: str):
        self.tmp_file = tmp_file
        self.console_calls: list[str] = []
        self.fn_calls: list[tuple[str, dict]] = []

    def _write(self) -> None:
        with open(self.tmp_file, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    async def exec_console(self, cmd: str) -> str:
        self.console_calls.append(cmd)
        self._write()
        return ""

    async def call_function(self, object_path: str, function_name: str,
                            *, parameters: dict | None = None, **_: Any) -> dict:
        self.fn_calls.append((function_name, parameters or {}))
        self._write()
        return {"ReturnValue": True}


@pytest.mark.asyncio
async def test_default_uses_capture_with_ui():
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "uap_shot.png")
        rc = FakeRC(target)
        result = await screenshot_viewport(
            rc=rc, py_exec=None, resolution="1280x720",
            _output_path_override=target,  # ui defaults to True
        )
        assert result["ok"] is True
        assert result["path"] == target
        assert result["size_bytes"] > 0
        # Default path is the UMG-inclusive backbuffer capture, not HighResShot.
        assert any(fn == "CaptureViewportWithUI" for fn, _ in rc.fn_calls)
        assert rc.console_calls == []


@pytest.mark.asyncio
async def test_ui_false_uses_highresshot():
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "uap_shot.png")
        rc = FakeRC(target)
        result = await screenshot_viewport(
            rc=rc, py_exec=None, resolution="1280x720", ui=False,
            _output_path_override=target,
        )
        assert result["ok"] is True
        assert any("HighResShot" in c for c in rc.console_calls)
        assert rc.fn_calls == []


@pytest.mark.asyncio
async def test_screenshot_inline_base64():
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "uap_shot.png")
        rc = FakeRC(target)
        result = await screenshot_viewport(
            rc=rc, py_exec=None, resolution="640x480", inline=True,
            _output_path_override=target,
        )
        assert result["ok"] is True
        assert "image_base64" in result
        assert base64.b64decode(result["image_base64"])[:4] == b"\x89PNG"
        assert result["image_mime"] == "image/png"
