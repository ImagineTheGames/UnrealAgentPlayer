import pytest

from unreal_agent_player.errors import ErrorCode
from unreal_agent_player.tools.python_exec import exec_python


class FakePy:
    def __init__(self, response):
        self.response = response
        self.code_seen: str | None = None
    def exec_python(self, code, unattended=True):
        self.code_seen = code
        return self.response


@pytest.mark.asyncio
async def test_exec_python_success():
    py = FakePy({"result": "success", "output": [{"type": "Info", "output": "hi\n"}]})
    result = await exec_python(rc=None, py_exec=py, code="print('hi')")
    assert result["ok"] is True
    assert "hi" in result["stdout"]
    assert result["stderr"] == ""


@pytest.mark.asyncio
async def test_exec_python_syntax_error():
    py = FakePy({
        "result": "failed",
        "output": [{"type": "Error", "output": "SyntaxError: invalid syntax\n"}],
    })
    result = await exec_python(rc=None, py_exec=py, code="1 = x")
    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.PYTHON_SYNTAX.value


@pytest.mark.asyncio
async def test_exec_python_runtime_error():
    py = FakePy({
        "result": "failed",
        "output": [{"type": "Error", "output": "NameError: boom\n"}],
    })
    result = await exec_python(rc=None, py_exec=py, code="undefined_name")
    assert result["ok"] is False
    assert result["error"]["code"] == ErrorCode.PYTHON_RUNTIME.value
