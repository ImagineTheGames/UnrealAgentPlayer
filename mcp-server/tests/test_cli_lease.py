import json

import pytest

from unreal_agent_player.cli import main
from unreal_agent_player import cli as cli_mod
from unreal_agent_player.errors import AgentError, ErrorCode
import unreal_agent_player.coordination as co


@pytest.fixture(autouse=True)
def _no_live_editor(monkeypatch):
    """`lease release` now asks the editor whether PIE is still live before it frees the lease.
    Pin an unreachable RC port so the test suite can never touch a real editor on this machine
    (and so the check fails fast into its documented fail-open path)."""
    monkeypatch.setenv("UAP_RC_PORT", "1")


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


# ---- release must not hand a LIVE PIE session to the next agent ----
# A stop that lies is recoverable if the caller checks. A `lease release` that succeeds on that
# lie is not: by then the next agent already holds the lease and is driving an editor still in
# PIE, and nothing in the system will ever tell either of them.

def _rc_table(monkeypatch, table):
    seen = []

    def fake(func, params, project=None):
        seen.append(func)
        if func not in table:
            raise AgentError(ErrorCode.UE_UNREACHABLE,
                             'Remote Control preset call returned 404: "Unable to resolve the '
                             'preset field."', recoverable=False)
        val = table[func]
        return val() if callable(val) else val

    monkeypatch.setattr(cli_mod, "_rc_call", fake)
    return seen


def test_release_refuses_while_pie_is_still_in_progress(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    _rc_table(monkeypatch, {"IsPIEInProgress": True})
    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "A",
                 "--project", "demo", "--wait", "0"]) == 0
    assert main(["lease", "release", "--agent", "A", "--project", "demo"]) == 1
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["ok"] is False and body["pie_live"] is True
    assert "uap pie stop" in body["error"]
    # ...and the lease is genuinely still held, so nobody else can take it.
    assert co._load("demo")["exclusive"]["agent"] == "A"


def test_release_proceeds_once_pie_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    _rc_table(monkeypatch, {"IsPIEInProgress": False})
    assert main(["lease", "acquire", "exclusive", "--agent", "A",
                 "--project", "demo", "--wait", "0"]) == 0
    assert main(["lease", "release", "--agent", "A", "--project", "demo"]) == 0
    assert co._load("demo")["exclusive"] is None


def test_release_falls_back_to_the_older_verb_on_a_behind_plugin(tmp_path, monkeypatch, capsys):
    """A plugin copy without IsPIEInProgress still answers IsInPIE -- weaker, but a `true` there
    is more than enough to refuse."""
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    seen = _rc_table(monkeypatch, {"IsInPIE": True})     # IsPIEInProgress 404s
    assert main(["lease", "acquire", "exclusive", "--agent", "A",
                 "--project", "demo", "--wait", "0"]) == 0
    assert main(["lease", "release", "--agent", "A", "--project", "demo"]) == 1
    assert seen == ["IsPIEInProgress", "IsInPIE"]
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["checked_with"] == "IsInPIE"


def test_release_force_hands_over_a_live_session_deliberately(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    seen = _rc_table(monkeypatch, {"IsPIEInProgress": True})
    assert main(["lease", "acquire", "exclusive", "--agent", "A",
                 "--project", "demo", "--wait", "0"]) == 0
    assert main(["lease", "release", "--agent", "A", "--project", "demo", "--force"]) == 0
    assert co._load("demo")["exclusive"] is None
    assert seen == [], "--force must not even ask the editor"


def test_release_is_fail_open_when_the_editor_cannot_be_reached(tmp_path, monkeypatch):
    """A dead editor has no PIE session. An unreachable RC must never wedge the lease."""
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)

    def dead(func, params, project=None):
        raise AgentError(ErrorCode.UE_UNREACHABLE, "Could not reach Remote Control at :30010")

    monkeypatch.setattr(cli_mod, "_rc_call", dead)
    assert main(["lease", "acquire", "exclusive", "--agent", "A",
                 "--project", "demo", "--wait", "0"]) == 0
    assert main(["lease", "release", "--agent", "A", "--project", "demo"]) == 0
    assert co._load("demo")["exclusive"] is None
