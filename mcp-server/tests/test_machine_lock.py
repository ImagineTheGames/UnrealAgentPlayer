"""The machine-wide foreground lock: two PROJECTS, one workstation.

The per-project lease solved "several agents, one editor". It cannot solve "two editors, one
keyboard", because each project's lease is keyed by project and neither one can see the other.
Project Broken Wings and School's Out VR run the SAME uap install on this machine (PBW pins
`UAP_PROJECT=ProjectBrokenWings`, SOVR pins `UAP_PROJECT=SchoolsOut`), so a PBW agent could start
PIE while a SOVR agent was mid-session: both leases were free, both said yes, and the one keyboard,
one foreground window and one GPU went to whichever grabbed focus last.

So `pie` / `input` / `screenshot` / `click` / `tab` / `nav` / `read-ui` take a second, machine-wide
turn on top of the project lease. What these pin:

  * the other project WAITS and then reports `busy` naming the holder -- it does not run;
  * your OWN project passes straight through, so a one-project workstation never waits (the
    requirement that made a simple global mutex wrong: intra-project turn-taking already has an
    owner, the project lease, and a second gate there would deadlock agents it serializes fine);
  * read-only verbs never take it -- you must be able to diagnose a wedged machine;
  * it ages out and can be broken, so a crashed session cannot own the machine forever.
"""

import json
import time

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player import cli


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _stub_rc(monkeypatch, table):
    """Same shape the pie/lease CLI tests use: an RC table, and anything else is a test bug."""
    seen = []

    def fake(func, params, project=None):
        seen.append(func)
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val() if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


_STARTED = json.dumps({"ok": True, "mode": "flat"})
_STOPPED = json.dumps({"ok": True, "was_playing": True, "cancelled_queued_start": False,
                       "in_progress": True})


def _pie_start(monkeypatch, project, agent):
    _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    return cli.main(["pie", "start", "--project", project, "--agent", agent])


def _pie_stop(monkeypatch, project, agent):
    _stub_rc(monkeypatch, {"StopPIEEx": _STOPPED, "IsPIEInProgress": False})
    return cli.main(["pie", "stop", "--project", project, "--agent", agent])


# --- the lock itself --------------------------------------------------------------------

def test_the_other_project_is_blocked_and_told_who_holds_the_machine(tmp_path, monkeypatch):
    co.acquire_machine("SchoolsOut", reason="pie", agent="SOVR", pid=0, wait=0)
    busy = co.acquire_machine("ProjectBrokenWings", reason="pie", agent="PBW", wait=0)
    assert busy["ok"] is False and busy["busy"] is True
    # The blocked caller must be able to say WHO to go and ask -- agent alone is not enough when
    # the holder is in a different project entirely.
    assert busy["blocked_by"] == "SOVR"
    assert busy["project"] == "schoolsout"


def test_the_same_project_passes_straight_through(tmp_path, monkeypatch):
    """A second agent on the SAME project must not be gated here.

    This is the requirement that rules out a plain global mutex: PBW documents several agents
    sharing one editor, and they are already serialized by the project lease. Gating them again
    machine-wide would deadlock the normal single-project workflow.
    """
    co.acquire_machine("ProjectBrokenWings", reason="pie", agent="AGENT-1", pid=0, wait=0)
    second = co.acquire_machine("ProjectBrokenWings", reason="screenshot", agent="AGENT-2", wait=0)
    assert second["ok"] is True and second["granted"] is True
    # ...and it did NOT take ownership, so finishing its own call cannot free agent 1's hold.
    assert second["acquired"] is False
    assert co.machine_status()["holder"]["agent"] == "AGENT-1"


def test_a_pass_through_call_refreshes_the_holders_heartbeat(tmp_path, monkeypatch):
    """Using the machine IS liveness, exactly as it is for the project lease: an agent that is
    actively working must never have its hold reclaimed out from under it."""
    co.acquire_machine("pbw", reason="pie", agent="HOLDER", pid=0, ttl=600, wait=0)
    before = co.machine_status()["holder"]["heartbeat_at"]
    monkeypatch.setattr(co, "_now", lambda: before + 120)
    co.acquire_machine("pbw", reason="screenshot", agent="OTHER-AGENT-SAME-PROJECT", wait=0)
    assert co.machine_status()["holder"]["heartbeat_at"] > before


def test_an_abandoned_hold_ages_out_instead_of_wedging_the_machine(tmp_path, monkeypatch):
    """A crashed session must not own the workstation forever. Same reclaimer as the project
    lease: a heartbeat older than the TTL is a dead holder."""
    co.acquire_machine("schoolsout", reason="pie", agent="GHOST", pid=0, ttl=600, wait=0)
    assert co.acquire_machine("pbw", reason="pie", agent="LIVE", wait=0)["busy"]
    state = co._load(co.MACHINE_SENTINEL)
    state["exclusive"]["heartbeat_at"] = time.time() - 9999
    co._save(co.MACHINE_SENTINEL, state)
    assert co.acquire_machine("pbw", reason="pie", agent="LIVE", wait=0)["granted"]


def test_a_dead_transient_holder_is_reclaimed_by_pid(tmp_path, monkeypatch):
    """A transient hold anchors to its own process, so a killed `uap` frees the machine at once
    rather than after the TTL."""
    monkeypatch.setattr(co, "_pid_alive", lambda pid: False)
    co.acquire_machine("schoolsout", reason="screenshot", agent="KILLED", wait=0)
    assert co.acquire_machine("pbw", reason="pie", agent="NEXT", wait=0)["granted"]


def test_release_only_frees_your_own_hold_unless_forced(tmp_path, monkeypatch):
    co.acquire_machine("schoolsout", reason="pie", agent="SOVR", pid=0, wait=0)
    assert co.release_machine(agent="PBW", project="pbw")["released"] is False
    assert co.machine_status()["holder"]["agent"] == "SOVR"
    # Break-glass for a session that died without releasing and has not aged out yet.
    assert co.release_machine(agent="PBW", force=True)["released"] is True
    assert co.machine_status()["holder"] is None


def test_release_by_project_frees_a_hold_another_agent_took(tmp_path, monkeypatch):
    """`pie stop` is run by whichever agent is there; the hold may have been taken by a different
    agent on the same project. What ended is the PROJECT's session, so the project frees it."""
    co.acquire_machine("pbw", reason="pie", agent="STARTER", pid=0, wait=0)
    assert co.release_machine(agent="FINISHER", project="pbw")["released"] is True
    assert co.machine_status()["holder"] is None


def test_a_project_named_like_the_machine_scope_cannot_collide_with_it(tmp_path, monkeypatch):
    """The machine lock lives in its own lease file. A project whose name sanitises to that same
    key would otherwise share the file and silently take/free the machine lock as its own lease.
    """
    assert co._safe_project(co.MACHINE_SCOPE) != co.MACHINE_SCOPE
    assert co._lease_path(co.MACHINE_SCOPE) != co._lease_path(co.MACHINE_SENTINEL)
    co.acquire(co.MACHINE_SCOPE, "exclusive", reason="pie", agent="A", wait=0)
    assert co.machine_status()["holder"] is None


def test_the_lock_can_be_disabled_entirely(tmp_path, monkeypatch):
    """An escape hatch, so a bug in coordination can never be the thing standing between an agent
    and the editor."""
    assert co.machine_lock_enabled() is True
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    assert co.machine_lock_enabled() is False


# --- the CLI, end to end ----------------------------------------------------------------

def test_pie_in_one_project_blocks_input_in_the_other(monkeypatch, capsys):
    """The headline case, verbatim from the report: SOVR is playing, PBW must not inject keys
    into the machine it is playing on."""
    assert _pie_start(monkeypatch, "SchoolsOut", "SOVR-AGENT") == 0
    capsys.readouterr()

    injected = {"n": 0}
    monkeypatch.setattr(cli, "_rc_call",
                        lambda *a, **k: injected.__setitem__("n", injected["n"] + 1))
    assert cli.main(["input", "hold", "W", "--seconds", "1", "--project", "ProjectBrokenWings",
                     "--agent", "PBW-AGENT"]) == 1
    assert injected["n"] == 0, "input reached the editor while the other project held the machine"
    body = _out(capsys)
    assert body["busy"] is True and body["blocked_by"] == "SOVR-AGENT"
    assert body["project"] == "schoolsout" and body["scope"] == "machine"


def test_pie_stop_hands_the_machine_back(monkeypatch, capsys):
    assert _pie_start(monkeypatch, "SchoolsOut", "SOVR-AGENT") == 0
    assert _pie_stop(monkeypatch, "SchoolsOut", "SOVR-AGENT") == 0
    capsys.readouterr()
    assert co.machine_status()["holder"] is None
    assert _pie_start(monkeypatch, "ProjectBrokenWings", "PBW-AGENT") == 0


def test_a_failed_pie_stop_does_not_hand_the_machine_back(monkeypatch, capsys):
    """A stop that could not confirm the teardown left PIE running. Freeing the machine on it
    would hand a live session to the other project -- the same failure `lease release` refuses."""
    assert _pie_start(monkeypatch, "SchoolsOut", "SOVR-AGENT") == 0
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _stub_rc(monkeypatch, {"StopPIEEx": _STOPPED, "IsPIEInProgress": True})   # never tears down
    assert cli.main(["pie", "stop", "--project", "SchoolsOut", "--agent", "SOVR-AGENT",
                     "--timeout", "0"]) == 1
    capsys.readouterr()
    assert co.machine_status()["holder"]["project"] == "schoolsout"


def test_a_one_shot_verb_gives_the_machine_back_when_it_returns(monkeypatch, capsys):
    """Only PIE is held across calls. A screenshot needs the foreground for its own duration and
    no longer, so the next project is free the moment it returns."""
    monkeypatch.setattr(cli, "_screenshot", lambda args: 0)
    assert cli.main(["screenshot", "shot.png", "--project", "SchoolsOut", "--agent", "S"]) == 0
    assert co.machine_status()["holder"] is None
    assert cli.main(["screenshot", "shot.png", "--project", "ProjectBrokenWings",
                     "--agent", "P"]) == 0


def test_a_crashing_verb_still_gives_the_machine_back(monkeypatch, capsys):
    """A transient hold is released in `finally`. A verb that raises must not leave the other
    project locked out for the full TTL."""
    def boom(args):
        raise RuntimeError("editor went away mid-call")

    monkeypatch.setattr(cli, "_screenshot", boom)
    with pytest.raises(RuntimeError):
        cli.main(["screenshot", "shot.png", "--project", "SchoolsOut", "--agent", "S"])
    assert co.machine_status()["holder"] is None


def test_read_only_verbs_answer_while_the_other_project_holds_the_machine(monkeypatch, capsys):
    """You have to be able to DIAGNOSE a machine that is locked. `status` and `log` are the verbs
    you reach for while doing it, so they must never be behind the thing you are inspecting."""
    assert _pie_start(monkeypatch, "SchoolsOut", "SOVR-AGENT") == 0
    capsys.readouterr()
    ran = {"n": 0}
    monkeypatch.setattr(cli, "_status", lambda args: ran.__setitem__("n", ran["n"] + 1) or 0)
    monkeypatch.setattr(cli, "_log", lambda args: ran.__setitem__("n", ran["n"] + 1) or 0)
    assert cli.main(["status", "--project", "ProjectBrokenWings"]) == 0
    assert cli.main(["log", "tail", "--project", "ProjectBrokenWings"]) == 0
    assert ran["n"] == 2


def test_working_alone_never_waits(monkeypatch, capsys):
    """The single-project case must be a no-op: PIE up, then several agents driving it, with no
    wait and no busy anywhere."""
    assert _pie_start(monkeypatch, "ProjectBrokenWings", "AGENT-1") == 0
    monkeypatch.setattr(cli, "_screenshot", lambda args: 0)
    monkeypatch.setattr(cli, "_read_ui", lambda args: 0)
    assert cli.main(["screenshot", "a.png", "--project", "ProjectBrokenWings",
                     "--agent", "AGENT-2"]) == 0
    assert cli.main(["read-ui", "--project", "ProjectBrokenWings", "--agent", "AGENT-3"]) == 0
    # ...and agent 1's PIE hold is intact: a pass-through call must not free someone else's hold.
    assert co.machine_status()["holder"]["agent"] == "AGENT-1"


def test_the_lock_is_skipped_when_disabled(monkeypatch, capsys):
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    assert _pie_start(monkeypatch, "SchoolsOut", "SOVR-AGENT") == 0
    capsys.readouterr()
    assert co.machine_status()["holder"] is None
    monkeypatch.setattr(cli, "_screenshot", lambda args: 0)
    assert cli.main(["screenshot", "a.png", "--project", "ProjectBrokenWings", "--agent", "P"]) == 0


# --- the explicit lease verbs -----------------------------------------------------------

def test_lease_acquire_for_a_foreground_reason_takes_the_machine_too(monkeypatch, capsys):
    """`lease acquire exclusive --reason pie` is the documented way to hold PIE across calls. It
    is a claim on the workstation, not just on one editor, so it must take both turns."""
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "SOVR",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    assert co.machine_status()["holder"]["project"] == "schoolsout"
    # A rebuild is CPU-heavy but takes neither the keyboard nor a game window, so it does not.
    co.release_machine(force=True)
    co.release("SchoolsOut", agent="SOVR")
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "rebuild", "--agent", "R",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    assert co.machine_status()["holder"] is None


def test_a_contended_foreground_acquire_gives_the_project_lease_back(monkeypatch, capsys):
    """Half-holding is worse than not holding: a project lease kept while the machine is denied
    blocks your OWN project's other agents for a turn you never got."""
    co.acquire_machine("SchoolsOut", reason="pie", agent="SOVR", pid=0, wait=0)
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "PBW",
                     "--project", "ProjectBrokenWings", "--wait", "0"]) == 1
    body = _out(capsys)
    assert body["busy"] is True and body["blocked_by"] == "SOVR"
    assert co.status("ProjectBrokenWings")["exclusive"] is None


def test_lease_release_hands_back_both_turns(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"IsPIEInProgress": False})
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "A",
                     "--project", "pbw", "--wait", "0"]) == 0
    assert cli.main(["lease", "release", "--agent", "A", "--project", "pbw"]) == 0
    assert co.status("pbw")["exclusive"] is None
    assert co.machine_status()["holder"] is None


def test_lease_status_names_the_machine_holder(monkeypatch, capsys):
    """"My project lease is free" explains nothing when what is blocking you is the OTHER
    project's PIE session, so one call has to show both scopes."""
    co.acquire_machine("SchoolsOut", reason="pie", agent="SOVR", pid=0, wait=0)
    assert cli.main(["lease", "status", "--project", "ProjectBrokenWings"]) == 0
    body = _out(capsys)
    assert body["exclusive"] is None                      # this project's editor is free...
    assert body["machine"]["holder"]["project"] == "schoolsout"   # ...the machine is not


def test_machine_status_and_release_verbs(monkeypatch, capsys):
    co.acquire_machine("SchoolsOut", reason="pie", agent="SOVR", pid=0, wait=0)
    assert cli.main(["lease", "machine-status"]) == 0
    assert _out(capsys)["holder"]["agent"] == "SOVR"
    # Without --force you can only give back what is yours.
    assert cli.main(["lease", "machine-release", "--project", "pbw", "--agent", "PBW"]) == 0
    assert _out(capsys)["released"] is False
    assert cli.main(["lease", "machine-release", "--force"]) == 0
    assert _out(capsys)["released"] is True
    assert co.machine_status()["holder"] is None
