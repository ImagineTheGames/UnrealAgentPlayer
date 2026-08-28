from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server

from unreal_agent_player.baselines import BaselineStore
from unreal_agent_player.registry import register_all
from unreal_agent_player.transport import PythonRemoteExecClient, RemoteControlClient
from unreal_agent_player.uia import UIADriver

logger = logging.getLogger(__name__)


def build_server(
    rc: RemoteControlClient | None = None,
    py_exec: PythonRemoteExecClient | None = None,
    store: BaselineStore | None = None,
    ui_driver: UIADriver | None = None,
) -> Server:
    server = Server("unreal-agent-player")
    register_all(
        server,
        rc=rc or RemoteControlClient(),
        py_exec=py_exec or PythonRemoteExecClient(),
        store=store or BaselineStore(Path.home() / ".uap-baselines.json"),
        ui_driver=ui_driver or UIADriver(),
    )
    return server


async def _run() -> None:
    server = build_server()
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())
