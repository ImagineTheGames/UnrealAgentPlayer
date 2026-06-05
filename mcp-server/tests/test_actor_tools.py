import json
import pytest

from unreal_agent_player.tools.actor import (
    actor_call_function, actor_find, actor_get_properties, actor_set_property,
)


class FakePy:
    def __init__(self, script_output: str):
        self.output = script_output
        self.code_seen: str | None = None
    def exec_python(self, code, unattended=True):
        self.code_seen = code
        return {
            "result": "success",
            "output": [{"type": "Info", "output": self.output + "\n"}],
        }


@pytest.mark.asyncio
async def test_actor_find_parses_list():
    fake_json = json.dumps({
        "actors": [
            {"path": "/Game/Maps/Test.Test:PersistentLevel.BP_Door_0",
             "class": "BP_Door_C", "location": [0, 0, 0], "tags": ["Gym"]},
        ],
    })
    py = FakePy(fake_json)
    result = await actor_find(rc=None, py_exec=py, class_="BP_Door_C", world="pie")
    assert result["ok"] is True
    assert len(result["actors"]) == 1
    assert result["actors"][0]["class"] == "BP_Door_C"
    assert "BP_Door_C" in py.code_seen


@pytest.mark.asyncio
async def test_actor_get_properties():
    py = FakePy(json.dumps({"properties": {"bOpen": True, "health": 100}}))
    result = await actor_get_properties(
        rc=None, py_exec=py, path="/Game/X.Map:PersistentLevel.A_0",
        property_names=["bOpen", "health"],
    )
    assert result["ok"] is True
    assert result["properties"]["bOpen"] is True


@pytest.mark.asyncio
async def test_actor_set_property():
    py = FakePy(json.dumps({"ok": True}))
    result = await actor_set_property(
        rc=None, py_exec=py, path="/Game/A.A:B.C_0", name="bOpen", value=True,
    )
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_actor_call_function():
    py = FakePy(json.dumps({"result": "done"}))
    result = await actor_call_function(
        rc=None, py_exec=py, path="/Game/A.A:B.C_0", function="OpenDoor",
        args={"speed": 2.0},
    )
    assert result["ok"] is True
    assert result["result"] == "done"
