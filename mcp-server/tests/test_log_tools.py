import json

import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player.tools.log import log_since, log_tail
from unreal_agent_player.transport import RemoteControlClient


@pytest.mark.asyncio
async def test_log_since_parses_json(httpx_mock: HTTPXMock):
    payload = json.dumps({
        "cursor": 1050,
        "lines": [
            {"cursor": 1049, "timestamp": 1712.0, "category": "LogTemp", "verbosity": "Log", "message": "hi"},
            {"cursor": 1050, "timestamp": 1712.1, "category": "LogTemp", "verbosity": "Log", "message": "there"},
        ],
    })
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call", method="PUT",
        json={"ReturnValue": payload},
    )
    rc = RemoteControlClient()
    result = await log_since(rc=rc, py_exec=None, cursor=1040, max_lines=500)
    assert result["ok"] is True
    assert result["cursor"] == 1050
    assert len(result["lines"]) == 2
    await rc.aclose()


@pytest.mark.asyncio
async def test_log_tail_reads_last_N(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call", method="PUT",
        json={"ReturnValue": 2000},
    )
    payload = json.dumps({"cursor": 2000, "lines": []})
    httpx_mock.add_response(
        url="http://127.0.0.1:30010/remote/object/call", method="PUT",
        json={"ReturnValue": payload},
    )
    rc = RemoteControlClient()
    result = await log_tail(rc=rc, py_exec=None, lines=200)
    assert result["ok"] is True
    assert result["cursor"] == 2000
    await rc.aclose()
