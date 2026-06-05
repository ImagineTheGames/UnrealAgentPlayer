import base64
import os
import tempfile

import pytest

from unreal_agent_player.tools.screenshot import screenshot_viewport


class FakeRC:
    def __init__(self, tmp_file: str):
        self.tmp_file = tmp_file
        self.called: list[str] = []
    async def exec_console(self, cmd: str) -> str:
        self.called.append(cmd)
        with open(self.tmp_file, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return ""


@pytest.mark.asyncio
async def test_screenshot_returns_path():
    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "uap_shot.png")
        rc = FakeRC(target)
        result = await screenshot_viewport(
            rc=rc, py_exec=None, resolution="1280x720",
            _output_path_override=target,
        )
        assert result["ok"] is True
        assert result["path"] == target
        assert result["width"] == 1280
        assert result["height"] == 720
        assert result["size_bytes"] > 0
        assert "image_base64" not in result
        assert any("HighResShot" in c for c in rc.called)


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
