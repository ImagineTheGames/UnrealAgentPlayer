import pytest
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_call_preset_unwraps_returnvalue(httpx_mock):
    httpx_mock.add_response(
        method="PUT",
        url="http://127.0.0.1:30010/remote/preset/UAP_Preset/function/GetPluginVersion",
        json={"ReturnedValues": [{"ReturnValue": "0.0.1"}]},
    )
    rc = RemoteControlClient()
    try:
        out = await rc.call_preset("GetPluginVersion", {})
        assert out == "0.0.1"
    finally:
        await rc.aclose()
