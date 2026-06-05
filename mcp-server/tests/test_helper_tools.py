import json
import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.tools.helpers import helper_call, helper_list
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_helper_list(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={
        "ReturnValue": [
            {
                "Name": "USchoolsOutTestHelpers::IsDoorOpen",
                "Category": "Agent|Doors",
                "Tooltip": "door open?",
                "PhaseRequirement": "Playing",
                "ArgSchemaJson": '{"type":"object","properties":{"DoorTag":{"type":"string"}},"required":["DoorTag"]}',
                "ReturnSchemaJson": '{"type":"boolean"}',
                "bSupported": True,
                "UnsupportedReason": "",
            }
        ],
    })
    rc = RemoteControlClient()
    result = await helper_list(rc=rc, py_exec=None)
    assert result["ok"] is True
    assert len(result["helpers"]) == 1
    h = result["helpers"][0]
    assert h["name"] == "USchoolsOutTestHelpers::IsDoorOpen"
    assert h["arg_schema"]["properties"]["DoorTag"]["type"] == "string"
    await rc.aclose()


@pytest.mark.asyncio
async def test_helper_call_success(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={"ReturnValue": json.dumps({"ok": True, "result": True})}
    )
    rc = RemoteControlClient()
    result = await helper_call(rc=rc, py_exec=None,
                               name="USchoolsOutTestHelpers::IsDoorOpen",
                               args={"DoorTag": "Gym"})
    assert result == {"ok": True, "result": True}
    await rc.aclose()


@pytest.mark.asyncio
async def test_helper_call_unknown(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={"ReturnValue": json.dumps({
            "ok": False,
            "error": {"code": "HELPER_UNKNOWN", "message": "not found"}
        })}
    )
    rc = RemoteControlClient()
    result = await helper_call(rc=rc, py_exec=None, name="Nope::None", args={})
    assert result["ok"] is False
    assert result["error"]["code"] == "HELPER_UNKNOWN"
    assert result["error"]["domain"] == "ue_side"
    assert result["error"]["recoverable"] is False
    await rc.aclose()
