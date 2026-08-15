from unreal_agent_player.cli import main
from unreal_agent_player import cli as cli_mod
import unreal_agent_player.coordination as co


def test_cli_lease_persists_across_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    base = ["--project", "demo", "--wait", "0"]

    # A standalone `lease acquire` must default to a TTL-only hold (pid 0) so it survives the
    # command's process exit -- otherwise PID-death would evict it immediately (regression guard).
    assert main(["lease", "acquire", "exclusive", "--reason", "rebuild", "--agent", "A"] + base) == 0
    state = co._load("demo")
    assert state["exclusive"]["agent"] == "A"
    assert state["exclusive"]["pid"] == 0

    # A different agent is blocked (non-zero exit so a script's `if` can branch on it).
    assert main(["lease", "acquire", "exclusive", "--agent", "B"] + base) == 1
    # Release frees it.
    assert main(["lease", "release", "--agent", "A", "--project", "demo"]) == 0
    assert main(["lease", "acquire", "exclusive", "--agent", "B"] + base) == 0


def test_cli_lease_explicit_pid_is_reclaimed_on_death(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(co, "_pid_alive", lambda pid: False)  # the anchored process is "dead"
    # Anchored to a (now dead) pid -> next acquire reclaims it despite a long TTL.
    assert main(["lease", "acquire", "exclusive", "--agent", "R", "--pid", "999999",
                 "--ttl", "9999", "--project", "demo", "--wait", "0"]) == 0
    assert main(["lease", "acquire", "exclusive", "--agent", "S",
                 "--project", "demo", "--wait", "0"]) == 0  # reclaimed


# ---- enforcement: an exclusive lease must actually gate other agents' editor ops ----
# Regression: `wait_if_blocked` existed but had NO callers, so `lease acquire exclusive
# --reason pie` recorded a holder that no command ever consulted. Other agents happily ran
# `exec` / level-load / PIE straight through a held lease.

def test_exclusive_lease_blocks_another_agents_editor_op(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_LEASE_WAIT", "0")       # do not really block the test
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    ran = {"n": 0}
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)

    class _FakeExec:
        def __init__(self, **kw):
            pass
        def exec_python(self, code):
            ran["n"] += 1
            return {"success": True, "result": "ok", "output": []}
    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)

    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "HOLDER",
                 "--project", "demo", "--wait", "0"]) == 0

    # Another agent (no token) must NOT reach the editor.
    rc = main(["exec", "print(1)", "--project", "demo"])
    assert ran["n"] == 0, "editor op ran despite another agent holding the exclusive lease"
    assert rc != 0
    assert "HOLDER" in capsys.readouterr().out


def test_lease_holder_is_not_blocked_by_its_own_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_LEASE_WAIT", "0")
    ran = {"n": 0}
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)

    class _FakeExec:
        def __init__(self, **kw):
            pass
        def exec_python(self, code):
            ran["n"] += 1
            return {"success": True, "result": "ok", "output": []}
    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)

    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "MINE",
                 "--project", "demo", "--wait", "0"]) == 0
    # Same agent, identified via --agent -> proceeds.
    assert main(["exec", "print(1)", "--project", "demo", "--agent", "MINE"]) == 0
    # ...and via $UAP_AGENT_ID, since this harness has no persistent shell env per call.
    monkeypatch.setenv("UAP_AGENT_ID", "MINE")
    assert main(["exec", "print(1)", "--project", "demo"]) == 0
    assert ran["n"] == 2


def test_holder_editor_op_refreshes_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_LEASE_WAIT", "0")
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)

    class _FakeExec:
        def __init__(self, **kw):
            pass
        def exec_python(self, code):
            return {"success": True, "result": "ok", "output": []}
    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)

    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "HB",
                 "--project", "demo", "--wait", "0"]) == 0
    before = co._load("demo")["exclusive"]["heartbeat_at"]
    monkeypatch.setattr(co, "_now", lambda: before + 50.0)
    assert main(["exec", "print(1)", "--project", "demo", "--agent", "HB"]) == 0
    after = co._load("demo")["exclusive"]["heartbeat_at"]
    assert after > before, "holder's own editor op must refresh the lease heartbeat"


def test_abandoned_pie_lease_ages_out_and_unblocks_others(tmp_path, monkeypatch):
    """A holder that stops calling uap must not wedge everyone for the full rebuild TTL."""
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_LEASE_WAIT", "0")
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    ran = {"n": 0}

    class _FakeExec:
        def __init__(self, **kw):
            pass
        def exec_python(self, code):
            ran["n"] += 1
            return {"success": True, "result": "ok", "output": []}
    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)

    t0 = co._now()
    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "GHOST",
                 "--project", "demo", "--wait", "0"]) == 0
    assert co._load("demo")["exclusive"]["ttl"] == 600

    # Still held 9 minutes in -> others blocked.
    monkeypatch.setattr(co, "_now", lambda: t0 + 540)
    assert main(["exec", "print(1)", "--project", "demo"]) == 1
    assert ran["n"] == 0

    # Past TTL with no heartbeat from GHOST -> reclaimed, others proceed.
    monkeypatch.setattr(co, "_now", lambda: t0 + 601)
    assert main(["exec", "print(1)", "--project", "demo"]) == 0
    assert ran["n"] == 1
