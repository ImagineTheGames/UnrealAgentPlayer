"""The proven-node cache: the same targeting guarantees, without re-proving them every call.

Discovery ran in full on EVERY `uap exec` -- multicast ping, a 0.8s settle window to catch every
responder, then an identity probe (itself a round-trip that executes Python in the editor) per
node -- before the caller's code ran at all. Measured with two editors open: ~1.05s plus two probe
round-trips, per call. None of it answers a new question after the first time.

What is under test is that making it faster did not make it looser. The property that must hold is
the one two real incidents were paid for: a call NEVER lands on a node that does not match the
selector. So the cache is only ever written after a real probe matched, and only ever used when
that exact node id answers and its process is alive. ClickUp 17tm466ft35.
"""

import json

import pytest

from unreal_agent_player.errors import AgentError
from unreal_agent_player.transport import PythonRemoteExecClient

EDITOR = {
    "node": "n-editor",
    "project": "E:/ImagineGames/SchoolsOutVR/SchoolsOut.uproject",
    "role": "editor",
    "pid": 4242,
    "cmdline": "UnrealEditor.exe SchoolsOut.uproject",
}
CLIENT_2 = {
    "node": "n-client2",
    "project": "E:/ImagineGames/SchoolsOutVR/SchoolsOut.uproject",
    "role": "game",
    "pid": 5353,
    "cmdline": "UnrealEditor.exe SchoolsOut.uproject -game -DevAuthToolName=Context_2",
}
RESULT = {"success": True, "result": "ok", "output": []}


@pytest.fixture(autouse=True)
def _alive(monkeypatch):
    """Every pid in these tests is a live process unless a test says otherwise."""
    monkeypatch.setattr("unreal_agent_player.transport._pid_alive", lambda pid: True)


def _client(monkeypatch, answering, counters, **kwargs):
    client = PythonRemoteExecClient(**kwargs)
    by_node = {a["node"]: a for a in answering}

    def discover(*_a):
        counters["discover"] += 1
        return list(by_node)

    def probe(_m, _d, n):
        counters["probe"] += 1
        return dict(by_node[n])

    def run(_m, _d, node, *_a):
        counters["run"] += 1
        counters["ran_on"].append(node)
        return dict(RESULT)

    monkeypatch.setattr(client, "_discover_nodes", discover)
    monkeypatch.setattr(client, "_probe_node", probe)
    monkeypatch.setattr(client, "_run_on_node", run)
    monkeypatch.setattr(client, "_make_multicast_socket", lambda: _FakeSock())
    monkeypatch.setattr(client, "_ping_node",
                        lambda _m, _d, node: counters.setdefault("ping", 0) == 0
                        or node in by_node)
    return client


class _FakeSock:
    def settimeout(self, _t): ...
    def sendto(self, *_a): ...
    def close(self): ...


def _counters():
    return {"discover": 0, "probe": 0, "run": 0, "ran_on": []}


def test_second_call_skips_discovery_and_both_probes(monkeypatch):
    c = _counters()
    cl = _client(monkeypatch, [CLIENT_2, EDITOR], c, node_project_substr="SchoolsOut")
    cl.exec_python("print(1)")
    assert (c["discover"], c["probe"]) == (1, 2)      # first call proves it the slow way
    cl2 = _client(monkeypatch, [CLIENT_2, EDITOR], c, node_project_substr="SchoolsOut")
    cl2.exec_python("print(2)")
    assert c["discover"] == 1                          # ...and the second pays none of it again
    assert c["probe"] == 2
    assert c["ran_on"] == ["n-editor", "n-editor"]


def test_a_cached_entry_never_redirects_a_different_selector(monkeypatch):
    """The cache is keyed by the selector that proved it, so a `--instance` change re-proves."""
    c = _counters()
    _client(monkeypatch, [CLIENT_2, EDITOR], c,
            node_project_substr="SchoolsOut").exec_python("print(1)")
    c2 = _counters()
    cl = _client(monkeypatch, [CLIENT_2, EDITOR], c2,
                 node_project_substr="SchoolsOut", node_instance="Context_2")
    cl.exec_python("print(1)")
    assert c2["discover"] == 1                         # different selector -> full path
    assert c2["ran_on"] == ["n-client2"]


def test_an_entry_whose_process_is_gone_is_dropped(monkeypatch):
    c = _counters()
    _client(monkeypatch, [EDITOR], c, node_project_substr="SchoolsOut").exec_python("print(1)")
    monkeypatch.setattr("unreal_agent_player.transport._pid_alive", lambda pid: False)
    c2 = _counters()
    _client(monkeypatch, [EDITOR], c2, node_project_substr="SchoolsOut").exec_python("print(1)")
    assert c2["discover"] == 1                         # editor restarted -> re-discover


def test_a_hand_edited_entry_that_no_longer_matches_is_refused(monkeypatch, tmp_path):
    """Belt and braces: even a cache file naming a `-game` client cannot make `exec` use one."""
    c = _counters()
    cl = _client(monkeypatch, [EDITOR], c, node_project_substr="SchoolsOut")
    cl.exec_python("print(1)")
    cl._cache_file().write_text(json.dumps(CLIENT_2), encoding="utf-8")
    c2 = _counters()
    cl2 = _client(monkeypatch, [CLIENT_2, EDITOR], c2, node_project_substr="SchoolsOut")
    cl2.exec_python("print(1)")
    assert c2["ran_on"] == ["n-editor"]                # not the client the file named


def test_nothing_is_cached_when_nothing_matched(monkeypatch):
    c = _counters()
    cl = _client(monkeypatch, [CLIENT_2], c, node_project_substr="SchoolsOut")
    with pytest.raises(AgentError):
        cl.exec_python("print(1)")
    assert cl._cached_node() is None


def test_the_cache_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("UAP_NODE_CACHE", "0")
    c = _counters()
    _client(monkeypatch, [EDITOR], c, node_project_substr="SchoolsOut").exec_python("print(1)")
    _client(monkeypatch, [EDITOR], c, node_project_substr="SchoolsOut").exec_python("print(1)")
    assert c["discover"] == 2
