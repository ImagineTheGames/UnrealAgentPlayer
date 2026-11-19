import json
import os
import time

import pytest

from unreal_agent_player import launch_registry as reg
from unreal_agent_player.instances import InstanceRegistry
from unreal_agent_player.tools import game as game_mod

FIELDS = {"id", "kind", "pid", "pidStart", "device", "package", "appId", "hostName",
          "launcher", "owner", "ownerKind", "ownerPid", "started"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("SOVR_JOB_ID", "UAP_AGENT", "UAP_AGENT_ID"):
        monkeypatch.delenv(k, raising=False)


def test_write_matches_schema(tmp_path):
    f = reg.register_launch(4242, "agent-x", directory=tmp_path / "new" / "dir", force=True)
    assert f and f.exists() and f.parent.name == "dir"
    e = json.loads(f.read_text(encoding="utf-8"))
    assert set(e) == FIELDS
    assert e["kind"] == "pc" and e["pid"] == 4242 and e["launcher"] == "uap"
    assert e["owner"] == "agent-x" and e["ownerKind"] == "agent" and e["ownerPid"] == os.getpid()
    assert e["id"].startswith("pc-4242-") and f.name == e["id"] + ".json"
    assert e["started"].endswith("Z")


def test_owner_precedence(monkeypatch):
    assert reg.launch_owner(None) == ("uap", "agent")
    assert reg.launch_owner("tok") == ("tok", "agent")
    monkeypatch.setenv("UAP_AGENT", "envagent")
    assert reg.launch_owner("tok") == ("envagent", "agent")
    monkeypatch.setenv("SOVR_JOB_ID", "job-7")
    assert reg.launch_owner("tok") == ("job-7", "job")


def test_remove_and_noop(tmp_path):
    f = reg.register_launch(1, directory=tmp_path, force=True)
    reg.unregister_launch(f)
    assert not f.exists()
    reg.unregister_launch(f)          # already gone: harmless
    reg.unregister_launch(None)


def test_unwritable_dir_is_harmless(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert reg.register_launch(1, directory=blocker / "sub", force=True) is None


def test_non_windows_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(reg.os, "name", "posix")
    assert reg.register_launch(5, directory=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_removed_on_process_exit(tmp_path):
    class P:
        def wait(self): return 0
    f = reg.register_launch(9, directory=tmp_path, force=True)
    reg.unregister_on_exit(P(), f)
    for _ in range(50):
        if not f.exists():
            break
        time.sleep(0.05)
    assert not f.exists()


class FakeProc:
    pid = 4321
    def __init__(self): self.gate = False
    def poll(self): return None
    def terminate(self): pass
    def kill(self): pass
    def wait(self):
        while not self.gate:
            time.sleep(0.02)


@pytest.mark.asyncio
async def test_launch_registers_and_stop_removes(tmp_path, monkeypatch):
    monkeypatch.setenv("SOVR_LAUNCH_REGISTRY", str(tmp_path))
    monkeypatch.setattr(reg, "_pid_start_ticks", lambda pid: 0)
    monkeypatch.setattr(reg.os, "name", "nt")
    r = InstanceRegistry(editor_port=30010)
    proc = FakeProc()
    monkeypatch.setattr(game_mod, "_spawn", lambda *a, **k: proc)
    async def _ready(port): return True
    monkeypatch.setattr(game_mod, "_wait_rc_ready", _ready)
    out = await game_mod.game_launch(registry=r)
    assert out["ok"]
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["pid"] == 4321
    await game_mod.game_stop(registry=r, instance_id=out["instance_id"])
    assert list(tmp_path.glob("*.json")) == []
    proc.gate = True
