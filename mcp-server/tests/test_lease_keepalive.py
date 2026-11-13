"""17tm466zzn0: the editor lease expired while an agent drove standalone -game clients.

The holder ran `lease acquire exclusive --reason pie` to keep PIE off the editor, then spent more
than the 10 minute TTL sending every verb to `--instance pid:<client>` (input, read-ui, rc,
screenshot --window, exec-file). Those verbs skip the editor guard chain on purpose (they must not
wait behind a lease), so nothing heartbeat the lease; it aged out and a second agent started PIE
in the same Photon hub mid-run.

Fix: any uap call made under an explicit --agent refreshes that agent's lease, whatever it targets.
"""

import types

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player import cli
from unreal_agent_player.cli import main

_CLIENT = {"pid": 22, "role": "game", "project": "E:/p/Demo.uproject",
           "cmdline": "UnrealEditor.exe Demo.uproject -game -UAPRCPort=30102"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "1")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)


def _hold(agent="HOLDER", reason="pie"):
    assert main(["lease", "acquire", "exclusive", "--reason", reason, "--agent", agent,
                 "--project", "demo", "--wait", "0"]) == 0
    return co._load("demo")["exclusive"]["heartbeat_at"]


def _later(monkeypatch, seconds):
    t = co._now() + seconds
    monkeypatch.setattr(co, "_now", lambda: t)
    return t


def _args(cmd, agent, instance=None, window=None):
    return types.SimpleNamespace(cmd=cmd, instance=instance, window=window, project="demo",
                                 func=lambda a: 0, client=None, agent=agent, task=None)


def _standalone_only(monkeypatch):
    from unreal_agent_player.transport import PythonRemoteExecClient as Real

    class FakeProbe(Real):
        def list_nodes(self):
            return [_CLIENT]
    monkeypatch.setattr(cli, "PythonRemoteExecClient", FakeProbe)


@pytest.mark.parametrize("cmd", ["input", "read-ui", "rc", "sample", "log", "exec", "exec-file"])
def test_a_standalone_instance_verb_under_agent_refreshes_the_lease(monkeypatch, cmd):
    _standalone_only(monkeypatch)
    before = _hold()
    t = _later(monkeypatch, 500.0)
    assert cli._run_parsed(_args(cmd, "HOLDER", instance="pid:22")) == 0
    assert co._load("demo")["exclusive"]["heartbeat_at"] == t > before


def test_screenshot_window_under_agent_refreshes_the_lease(monkeypatch):
    before = _hold()
    t = _later(monkeypatch, 500.0)
    assert cli._run_parsed(_args("screenshot", "HOLDER", window="pid:22")) == 0
    assert co._load("demo")["exclusive"]["heartbeat_at"] == t > before


def test_a_non_guarded_verb_under_agent_refreshes_the_lease(monkeypatch):
    before = _hold()
    t = _later(monkeypatch, 500.0)
    assert cli._run_parsed(_args("report", "HOLDER")) == 0
    assert co._load("demo")["exclusive"]["heartbeat_at"] == t > before


def test_agent_from_the_environment_counts(monkeypatch):
    _standalone_only(monkeypatch)
    before = _hold()
    monkeypatch.setenv("UAP_AGENT_ID", "HOLDER")
    t = _later(monkeypatch, 500.0)
    assert cli._run_parsed(_args("input", None, instance="pid:22")) == 0
    assert co._load("demo")["exclusive"]["heartbeat_at"] == t > before


def test_an_anonymous_call_refreshes_nothing(monkeypatch):
    _standalone_only(monkeypatch)
    before = _hold()
    _later(monkeypatch, 500.0)
    assert cli._run_parsed(_args("input", None, instance="pid:22")) == 0
    assert co._load("demo")["exclusive"]["heartbeat_at"] == before


def test_another_agents_call_never_refreshes_the_holders_lease(monkeypatch):
    _standalone_only(monkeypatch)
    before = _hold()
    _later(monkeypatch, 500.0)
    assert cli._run_parsed(_args("input", "OTHER", instance="pid:22")) == 0
    assert co._load("demo")["exclusive"]["heartbeat_at"] == before


def test_a_lease_that_already_aged_out_is_not_revived(monkeypatch):
    _standalone_only(monkeypatch)
    _hold()
    _later(monkeypatch, 5000.0)                      # well past the 600s pie TTL
    assert cli._run_parsed(_args("input", "HOLDER", instance="pid:22")) == 0
    assert co._load("demo").get("exclusive") is None
    # ...and the next agent gets the editor, exactly as before the change.
    assert co.acquire("demo", "exclusive", agent="NEXT", wait=0)["granted"]


def test_the_eviction_notice_is_still_delivered_to_the_next_guarded_call(monkeypatch):
    _standalone_only(monkeypatch)
    _hold()
    monkeypatch.setattr(co, "_editor_probe", lambda project: False)   # the editor died
    assert cli._run_parsed(_args("input", "HOLDER", instance="pid:22")) == 0
    note = co.take_eviction_notice("demo", agent="HOLDER")
    assert note, "keepalive must not swallow the lease_evicted notice"


def test_lease_verbs_manage_themselves(monkeypatch):
    called = []
    monkeypatch.setattr(co, "keepalive", lambda *a, **k: called.append(1) or 0)
    cli._keepalive_lease(types.SimpleNamespace(cmd="lease", agent="HOLDER"), "demo")
    assert called == []


def test_keepalive_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(co, "_refresh", boom)
    assert co.keepalive("demo", agent="HOLDER") == 0
    monkeypatch.setattr(co, "keepalive", boom)
    cli._keepalive_lease(types.SimpleNamespace(cmd="input", agent="HOLDER"), "demo")


def test_shared_holds_are_refreshed_too(monkeypatch):
    assert co.acquire("demo", "shared", agent="READER", wait=0)["granted"]
    before = co._load("demo")["shared"][0]["heartbeat_at"]
    t = _later(monkeypatch, 30.0)
    assert co.keepalive("demo", agent="READER") == 1
    assert co._load("demo")["shared"][0]["heartbeat_at"] == t > before
