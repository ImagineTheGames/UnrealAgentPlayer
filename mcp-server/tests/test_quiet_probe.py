"""uap's own probes read their answer back through the command RESULT, not print().

Every `print()` in remote-exec Python is a `LogPython:` line in the editor's Output Log and in
Saved/Logs. The node-identity probe printed `UAPNODE:{...}` on nearly every CLI call -- 235
lines in one day's SchoolsOut.log while several agents worked (ClickUp 17tm466jt8t) -- and
`UAPRCPORT:`, `UAPPROJ:` and `UAPDIAG:` did the same. These tests pin the quiet route.
"""

import inspect
import json
from typing import ClassVar

from unreal_agent_player import cli
from unreal_agent_player.transport import PythonRemoteExecClient


def test_quiet_expr_evaluates_to_the_bodys_answer_and_leaks_nothing():
    """The expression must work under eval() (UE's EvaluateStatement is Py_eval_input) and
    leave nothing in the caller's globals -- the remote-exec globals dict is shared by the whole
    editor session, and a UWorld left in it kills the editor on the next level load."""
    shared: dict = {}
    body = "w = object()\nimport os\n_uap_out = 'pid=' + str(os.getpid() > 0)\n"
    value = eval(PythonRemoteExecClient.quiet_expr(body), shared)
    assert value == "pid=True"
    assert set(shared) <= {"__builtins__"}, f"leaked into the shared globals: {set(shared)}"


def test_eval_result_round_trips_backslashes_and_quotes():
    """UE returns an evaluated value as its repr. A Windows command line carries backslashes
    and quotes, which a naive split of the repr would mangle."""
    backslash = chr(92)
    cmdline = f'C:{backslash}UE{backslash}UnrealEditor.exe "E:{backslash}P{backslash}S.uproject"'
    ident = json.dumps({"project": "E:/P/SchoolsOut.uproject", "cmdline": cmdline})
    assert PythonRemoteExecClient.decode_eval_result({"result": repr(ident)}) == ident
    assert PythonRemoteExecClient.decode_eval_result({"result": "None"}) is None
    assert PythonRemoteExecClient.decode_eval_result({"result": ""}) is None
    assert PythonRemoteExecClient.decode_eval_result({"result": "Traceback ..."}) is None


def test_the_node_probe_body_prints_nothing():
    body = PythonRemoteExecClient.NODE_PROBE_BODY
    assert "print(" not in body
    assert "print(" not in PythonRemoteExecClient.quiet_expr(body)
    assert "UAPNODE" in body          # the marker the fake nodes recognise the probe by


class _EditorStub(PythonRemoteExecClient):
    """Answers the way a real editor does for EvaluateStatement: the value comes back as its
    repr in `result`, and nothing is printed. Records what it was asked."""

    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, node_project_substr=None):
        super().__init__(node_project_substr=node_project_substr)

    def exec_python(self, code, *, unattended=True, exec_mode="ExecuteFile"):
        _EditorStub.calls.append((exec_mode, code))
        if "get_remote_control_port" in code:
            value = 30035
        elif "get_project_file_path" in code:
            value = "SchoolsOut"
        else:
            value = None
        return {"success": True, "result": repr(value), "output": []}


def test_rc_port_and_project_name_are_read_without_a_print(monkeypatch):
    _EditorStub.calls = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _EditorStub)
    assert cli._exec_rc_port("SchoolsOut") == 30035
    assert cli._exec_project_name("SchoolsOut") == "SchoolsOut"
    assert len(_EditorStub.calls) == 2
    for mode, code in _EditorStub.calls:
        assert mode == "EvaluateStatement"
        assert "print(" not in code, "a print() here is a LogPython line on every call"


def test_no_uap_probe_in_the_cli_prints_a_tagged_line():
    """Source-level guard: the tagged-print pattern must not come back in a new helper."""
    src = inspect.getsource(cli)
    for tag in ("UAPRCPORT", "UAPPROJ", "UAPDIAG"):
        assert f"print('{tag}:" not in src
