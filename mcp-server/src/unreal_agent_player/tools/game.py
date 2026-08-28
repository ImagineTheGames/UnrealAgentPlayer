from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any

import httpx

from unreal_agent_player.errors import ErrorCode, error_response, ok_response
from unreal_agent_player.instances import InstanceRegistry

_PROCS: dict[str, subprocess.Popen] = {}
_RUNTIME_OBJ = "/Script/UnrealAgentPlayerRuntime.Default__UAPAgentRuntimeSubsystem"


def _track_proc(instance_id: str, proc: Any) -> None:
    _PROCS[instance_id] = proc


def _spawn(port: int, mapname: str | None, extra: list[str] | None,
           no_vr: bool = True) -> subprocess.Popen:
    exe = os.environ.get("UAP_EDITOR_EXE", r"E:\ImagineGames\IG_MetaEngine\Engine\Binaries\Win64\UnrealEditor.exe")
    uproject = os.environ.get("UAP_UPROJECT", r"E:\ImagineGames\SchoolsOutVR\SchoolsOut.uproject")
    args = [exe, uproject]
    if mapname:
        args.append(mapname)
    args += ["-game", "-windowed", "-ResX=1280", "-ResY=720", "-RCWebControlEnable", f"-UAPRCPort={port}"]
    if no_vr:
        # Run flat (no HMD) so the boot flow takes the desktop/FPS path and doesn't wait
        # for a headset to be worn -- required for headless agent auto-testing. The game's
        # BootGameMode already has a no-HMD path; -nohmd makes IsHeadMountedDisplayEnabled
        # false so it triggers. (Run with the editor closed to avoid GPU contention.)
        args.append("-nohmd")
    if extra:
        args += extra
    return subprocess.Popen(args)


async def _wait_rc_ready(port: int, timeout: float = 90.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    url = f"http://127.0.0.1:{port}/remote/object/call"
    body = {"objectPath": _RUNTIME_OBJ, "functionName": "GetPluginVersion", "parameters": {}}
    async with httpx.AsyncClient(timeout=4.0) as client:
        while loop.time() < deadline:
            try:
                resp = await client.put(url, json=body)
                if resp.status_code < 400:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2.0)
    return False


async def game_launch(
    *, registry: InstanceRegistry, rc: Any = None, py_exec: Any = None,
    target: str = "standalone", map: str | None = None,
    extra_args: list[str] | None = None, no_vr: bool = True,
) -> dict[str, Any]:
    port = registry.next_free_port()
    proc = _spawn(port, map, extra_args, no_vr)
    if not await _wait_rc_ready(port):
        try:
            proc.kill()
        except Exception:
            pass
        return error_response(ErrorCode.GAME_LAUNCH_FAILED, f"Instance on port {port} did not answer RemoteControl in time.")
    iid = registry.register(port=port, pid=getattr(proc, "pid", None))
    _track_proc(iid, proc)
    return ok_response({"instance_id": iid, "port": port, "pid": getattr(proc, "pid", None)})


async def game_attach(
    *, registry: InstanceRegistry, rc: Any = None, py_exec: Any = None, port: int,
) -> dict[str, Any]:
    iid = registry.register(port=port, pid=None)
    return ok_response({"instance_id": iid, "port": port})


async def game_list(
    *, registry: InstanceRegistry, rc: Any = None, py_exec: Any = None,
) -> dict[str, Any]:
    out = []
    for info in registry.list():
        proc = _PROCS.get(info["instance_id"])
        alive = (proc.poll() is None) if proc else None
        out.append({**info, "alive": alive})
    return ok_response({"instances": out})


async def game_stop(
    *, registry: InstanceRegistry, rc: Any = None, py_exec: Any = None, instance_id: str,
) -> dict[str, Any]:
    if registry.get(instance_id) is None:
        return error_response(ErrorCode.INSTANCE_NOT_FOUND, f"Unknown instance {instance_id!r}.")
    proc = _PROCS.pop(instance_id, None)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    registry.remove(instance_id)
    return ok_response({"stopped": instance_id})
