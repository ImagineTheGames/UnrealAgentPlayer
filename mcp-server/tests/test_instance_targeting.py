"""Editor vs `-game` standalone client of the SAME project.

Regression cover for a real, expensive failure. `launch_2p_standalone.ps1` starts
`UnrealEditor.exe -game` clients of the project under test. Those clients answer Python
remote-exec discovery and report the SAME `unreal.Paths.get_project_file_path()` as the
editor, so pinning `--project` did not distinguish them. A `uap exec` landed on
`Standalone_Context_2`, returned `es: None` / `gw: None` -- a `-game` process has no editor
subsystems -- and that read as a transient editor glitch rather than a wrong target. The
reading taken from the "working around it" path was silently invalidated.

Two properties are under test:
  1. The editor is the only implicit target. A non-editor node is never selected by accident.
  2. Landing nowhere is LOUD and NAMES what answered, instead of a confident empty answer.
"""

import pytest

from unreal_agent_player.errors import AgentError, ErrorCode
from unreal_agent_player.transport import PythonRemoteExecClient

EDITOR = {
    "node": "n-editor",
    "project": "../../../../SchoolsOutVR/SchoolsOut.uproject",
    "role": "editor",
    "pid": 88836,
    "cmdline": "UnrealEditor.exe SchoolsOut.uproject",
}
CLIENT_2 = {
    "node": "n-client2",
    "project": "../../../../SchoolsOutVR/SchoolsOut.uproject",
    "role": "game",
    "pid": 53164,
    "cmdline": "UnrealEditor.exe SchoolsOut.uproject -game -DevAuthToolName=Context_2 -nohmd",
}
OTHER_PROJECT = {
    "node": "n-pbw",
    "project": "../../../../ProjectBrokenWings/ProjectBrokenWings.uproject",
    "role": "editor",
    "pid": 102648,
    "cmdline": "UnrealEditor.exe ProjectBrokenWings.uproject",
}


def _client(monkeypatch, answering, **kwargs):
    """A client whose discovery answers `answering`, without touching a socket."""
    client = PythonRemoteExecClient(**kwargs)
    by_node = {a["node"]: a for a in answering}
    monkeypatch.setattr(client, "_discover_nodes", lambda *_: list(by_node))
    monkeypatch.setattr(client, "_probe_node", lambda _m, _d, n: dict(by_node[n]))
    return client


def test_editor_is_selected_when_a_same_project_game_client_answers_first(monkeypatch):
    # Discovery order puts the standalone client first -- exactly the case that misfired.
    client = _client(monkeypatch, [CLIENT_2, EDITOR], node_project_substr="SchoolsOut")
    assert client._select_node(None, None, [CLIENT_2["node"], EDITOR["node"]]) == "n-editor"
    assert client.last_target["pid"] == 88836


def test_game_client_is_never_selected_even_when_it_is_the_only_answer(monkeypatch):
    """The heart of it: the wrong process must be a refusal, not a degraded answer."""
    client = _client(monkeypatch, [CLIENT_2], node_project_substr="SchoolsOut")
    assert client._select_node(None, None, [CLIENT_2["node"]]) is None
    assert client.last_target is None


def test_refusal_names_the_process_it_found_and_how_to_target_it(monkeypatch):
    client = _client(monkeypatch, [CLIENT_2], node_project_substr="SchoolsOut")
    with pytest.raises(AgentError) as excinfo:
        client.exec_python("print('x')")
    err = excinfo.value
    assert err.code is ErrorCode.UE_WRONG_INSTANCE
    msg = err.message
    assert "53164" in msg                      # names the pid it actually found
    assert "Context_2" in msg                  # and enough command line to recognise it
    assert "--instance" in msg                 # and the one flag that targets it on purpose
    assert "No EDITOR answered" in msg


def test_instance_selector_targets_the_client_deliberately(monkeypatch):
    client = _client(monkeypatch, [EDITOR, CLIENT_2],
                     node_project_substr="SchoolsOut", node_instance="Context_2")
    assert client._select_node(None, None, [EDITOR["node"], CLIENT_2["node"]]) == "n-client2"
    assert client.last_target["role"] == "game"


def test_instance_selector_by_pid(monkeypatch):
    client = _client(monkeypatch, [EDITOR, CLIENT_2],
                     node_project_substr="SchoolsOut", node_instance="pid:53164")
    assert client._select_node(None, None, [EDITOR["node"], CLIENT_2["node"]]) == "n-client2"


def test_project_filter_still_excludes_another_projects_editor(monkeypatch):
    """The older cross-targeting bug (PIE started in the wrong project) stays fixed."""
    client = _client(monkeypatch, [OTHER_PROJECT], node_project_substr="SchoolsOut")
    assert client._select_node(None, None, [OTHER_PROJECT["node"]]) is None


def test_no_project_filter_still_will_not_take_a_game_client(monkeypatch):
    """`first responder wins` is gone. With no --project, an editor is still required."""
    client = _client(monkeypatch, [CLIENT_2, OTHER_PROJECT])
    assert client._select_node(None, None, [CLIENT_2["node"], OTHER_PROJECT["node"]]) == "n-pbw"


def test_a_node_that_will_not_identify_itself_is_not_selected(monkeypatch):
    client = PythonRemoteExecClient(node_project_substr="SchoolsOut")
    monkeypatch.setattr(client, "_discover_nodes", lambda *_: ["n-mystery"])
    monkeypatch.setattr(client, "_probe_node", lambda *_: None)
    assert client._select_node(None, None, ["n-mystery"]) is None
    with pytest.raises(AgentError) as excinfo:
        client.exec_python("print('x')")
    assert "did not answer the identity probe" in excinfo.value.message


def test_describe_node_is_readable_and_bounded():
    text = PythonRemoteExecClient.describe_node(CLIENT_2)
    assert "pid 53164" in text
    assert "role=game" in text
    assert "project=SchoolsOut.uproject" in text
    long = dict(CLIENT_2, cmdline="x" * 500)
    assert len(PythonRemoteExecClient.describe_node(long)) < 300


def test_a_target_that_dies_mid_exec_is_reported_as_gone(monkeypatch):
    """`uap exec` cannot itself kill a process, but the code it runs can.

    `unreal.EditorLevelLibrary.get_editor_world()` executed inside a `UnrealEditor.exe -game`
    process ends that process immediately -- GEditor is null there, and the call takes the
    whole process down with no crash report and no clean-shutdown line. Verified live on
    2026-09-02 against a throwaway client, isolated to that single call (`print('alive')` and
    `get_editor_subsystem(UnrealEditorSubsystem)` both ran fine in the same process seconds
    before). The retry then re-runs discovery and finds nothing, which must not be reported as
    "nothing matched" -- the caller needs to know the thing under test is GONE, because every
    reading taken after that moment is void.
    """
    client = PythonRemoteExecClient(node_project_substr="SchoolsOut", node_instance="Context_2")
    client.last_target = dict(CLIENT_2)
    monkeypatch.setattr(client, "_discover_nodes", lambda *_: [EDITOR["node"]])
    monkeypatch.setattr(client, "_probe_node", lambda _m, _d, _n: dict(EDITOR))
    with pytest.raises(AgentError) as excinfo:
        client.exec_python("print('x')")
    msg = excinfo.value.message
    assert "has gone" in msg
    assert "53164" in msg
    assert "not evidence" in msg
