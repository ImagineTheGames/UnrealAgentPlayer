import pytest
from unreal_agent_player.errors import ErrorCode
from unreal_agent_player.instances import InstanceRegistry
from unreal_agent_player.tools import game as game_mod


class FakeProc:
    def __init__(self): self.pid = 4321; self._alive = True
    def poll(self): return None if self._alive else 0
    def terminate(self): self._alive = False
    def kill(self): self._alive = False


@pytest.mark.asyncio
async def test_game_launch_registers(monkeypatch):
    reg = InstanceRegistry(editor_port=30010)
    monkeypatch.setattr(game_mod, "_spawn", lambda port, mapname, extra, no_vr=True: FakeProc())
    async def _ready(port): return True
    monkeypatch.setattr(game_mod, "_wait_rc_ready", _ready)
    r = await game_mod.game_launch(registry=reg, target="standalone", map="/Game/M", extra_args=None)
    assert r["ok"] and r["port"] >= 30100 and r["instance_id"].startswith("inst")
    assert reg.resolve_port(r["instance_id"]) == r["port"]


@pytest.mark.asyncio
async def test_game_launch_timeout_kills(monkeypatch):
    reg = InstanceRegistry(editor_port=30010)
    proc = FakeProc()
    monkeypatch.setattr(game_mod, "_spawn", lambda port, mapname, extra, no_vr=True: proc)
    async def _never(port): return False
    monkeypatch.setattr(game_mod, "_wait_rc_ready", _never)
    r = await game_mod.game_launch(registry=reg, target="standalone", map=None, extra_args=None)
    assert not r["ok"] and r["error"]["code"] == ErrorCode.GAME_LAUNCH_FAILED.value
    assert proc.poll() is not None and reg.list() == []


@pytest.mark.asyncio
async def test_game_attach():
    reg = InstanceRegistry(editor_port=30010)
    r = await game_mod.game_attach(registry=reg, port=30055)
    assert r["ok"] and reg.resolve_port(r["instance_id"]) == 30055


@pytest.mark.asyncio
async def test_game_list_and_stop():
    reg = InstanceRegistry(editor_port=30010)
    proc = FakeProc()
    iid = reg.register(port=30060, pid=proc.pid)
    game_mod._track_proc(iid, proc)
    listed = await game_mod.game_list(registry=reg)
    assert listed["ok"]
    r = await game_mod.game_stop(registry=reg, instance_id=iid)
    assert r["ok"] and reg.get(iid) is None


@pytest.mark.asyncio
async def test_game_tools_registered(tmp_path):
    from unreal_agent_player.registry import register_all
    from unreal_agent_player.baselines import BaselineStore
    from unreal_agent_player.uia import UIADriver

    class FS:
        def __init__(self): self.h = {}
        def list_tools(self):
            def d(fn): self.h["list"] = fn; return fn
            return d
        def call_tool(self):
            def d(fn): self.h["call"] = fn; return fn
            return d

    fs = FS()
    register_all(fs, rc=None, py_exec=None, store=BaselineStore(tmp_path / "b.json"), ui_driver=UIADriver())
    tools = await fs.h["list"]()
    names = {t.name for t in tools}
    assert {"game_launch", "game_attach", "game_list", "game_stop"} <= names
    assert any(t.name == "input_key" and "target" in t.inputSchema.get("properties", {}) for t in tools)
