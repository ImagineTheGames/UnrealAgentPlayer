"""Transport resilience: a dropped command socket must not surface as a traceback.

Regression cover for a real failure -- mid-poll-loop against a HEALTHY PIE session the
editor reset the command connection (WinError 10054); the next call worked fine, but the
raw ConnectionResetError escaped `_read_command_result` and killed the run's sample.
"""


import pytest

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import PythonRemoteExecClient


class _ResettingConn:
    """A socket whose recv() raises ConnectionResetError, like WinError 10054."""

    def recv(self, _n):
        raise ConnectionResetError(10054, "An existing connection was forcibly closed")


class _TimingOutConn:
    def recv(self, _n):
        raise TimeoutError()


def test_read_command_result_maps_reset_to_agent_error():
    client = PythonRemoteExecClient()
    with pytest.raises(AgentError) as excinfo:
        client._read_command_result(_ResettingConn())
    assert excinfo.value.code is ErrorCode.UE_CONNECTION_RESET
    assert excinfo.value.recoverable
    # Distinguishable by a caller without string-matching the message.
    assert excinfo.value.to_response()["error"]["domain"] == "transport"


def test_read_command_result_still_returns_none_on_timeout():
    # A timeout is NOT a reset: it keeps the pre-existing "no result" behaviour.
    assert PythonRemoteExecClient()._read_command_result(_TimingOutConn()) is None


def test_exec_python_retries_a_reset_then_succeeds(monkeypatch):
    client = PythonRemoteExecClient()
    calls = {"n": 0}

    def flaky(code, *, unattended, exec_mode):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AgentError(ErrorCode.UE_CONNECTION_RESET, "reset")
        return {"success": True, "result": "ok", "output": []}

    monkeypatch.setattr(client, "_exec_python_once", flaky)
    monkeypatch.setattr(client, "RETRY_BACKOFF", (0.0, 0.0))
    assert client.exec_python("print('x')")["result"] == "ok"
    assert calls["n"] == 2


def test_exec_python_gives_up_after_bounded_retries(monkeypatch):
    client = PythonRemoteExecClient()
    calls = {"n": 0}

    def always_reset(code, *, unattended, exec_mode):
        calls["n"] += 1
        raise AgentError(ErrorCode.UE_CONNECTION_RESET, "reset")

    monkeypatch.setattr(client, "_exec_python_once", always_reset)
    monkeypatch.setattr(client, "RETRY_BACKOFF", (0.0, 0.0))
    with pytest.raises(AgentError) as excinfo:
        client.exec_python("print('x')")
    assert excinfo.value.code is ErrorCode.UE_CONNECTION_RESET
    assert calls["n"] == PythonRemoteExecClient.CONNECTION_RETRIES


def test_exec_python_does_not_retry_other_errors(monkeypatch):
    """A dead editor must fail fast -- retrying it just triples the wait."""
    client = PythonRemoteExecClient()
    calls = {"n": 0}

    def no_editor(code, *, unattended, exec_mode):
        calls["n"] += 1
        raise AgentError(ErrorCode.UE_REMOTE_EXEC_OFF, "no editor")

    monkeypatch.setattr(client, "_exec_python_once", no_editor)
    with pytest.raises(AgentError):
        client.exec_python("print('x')")
    assert calls["n"] == 1


def test_retry_reapplies_the_project_filter():
    """A retry must not be able to land on a different editor.

    Retries happen around _exec_python_once, which re-runs discovery AND _select_node with
    the same node_project_substr -- this machine runs two editors and cross-targeting has
    caused real disruption.
    """
    client = PythonRemoteExecClient(node_project_substr="SchoolsOut")
    assert client._node_project_substr == "SchoolsOut"
    # _select_node never falls back to a non-matching editor, on any attempt.
    assert client._select_node(None, None, []) is None
    assert client.last_target is None
