import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import RemoteControlClient, SUBSYSTEM_OBJECT_PATH


@pytest.mark.asyncio
async def test_call_function_roundtrip(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call",
        method="PUT",
        json={"ReturnValue": 42},
    )
    client = RemoteControlClient(host="127.0.0.1", port=30010)
    result = await client.call_function(
        SUBSYSTEM_OBJECT_PATH, "GetLogCursor", parameters={}
    )
    assert result == {"ReturnValue": 42}
    await client.aclose()


@pytest.mark.asyncio
async def test_call_function_connection_error_maps_to_ue_unreachable(httpx_mock: HTTPXMock):
    import httpx
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    client = RemoteControlClient(host="127.0.0.1", port=30010)
    with pytest.raises(AgentError) as excinfo:
        await client.call_function(SUBSYSTEM_OBJECT_PATH, "Foo", parameters={})
    assert excinfo.value.code == ErrorCode.UE_UNREACHABLE
    await client.aclose()


@pytest.mark.asyncio
async def test_exec_console_command_uses_standard_endpoint(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call",
        method="PUT",
        json={"ReturnValue": ""},
    )
    client = RemoteControlClient()
    await client.exec_console("stat fps")
    request = httpx_mock.get_requests()[0]
    assert b"ExecuteConsoleCommand" in request.content
    assert b"stat fps" in request.content
    await client.aclose()


@pytest.mark.asyncio
async def test_remote_control_client_context_manager(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call",
        method="PUT",
        json={"ReturnValue": "ok"},
    )
    async with RemoteControlClient() as client:
        result = await client.call_function(SUBSYSTEM_OBJECT_PATH, "Foo", parameters={})
    assert result == {"ReturnValue": "ok"}


@pytest.mark.asyncio
async def test_call_function_404_maps_to_object_not_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call",
        method="PUT",
        status_code=404,
        text="Object not found",
    )
    client = RemoteControlClient()
    with pytest.raises(AgentError) as excinfo:
        await client.call_function(SUBSYSTEM_OBJECT_PATH, "NoSuchFn", parameters={})
    assert excinfo.value.code == ErrorCode.UE_OBJECT_NOT_FOUND
    await client.aclose()
