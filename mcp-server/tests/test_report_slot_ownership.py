"""Two agents verifying at once must collide LOUDLY, not quietly [part of 17tm466fz3v].

The report slot is one per machine: a single `~/.uap-reports/.active` pointer, and every CLI
call is a fresh process, so that file is the only thing that says which run is being written to.
`report start` used to overwrite it unconditionally and record nothing about who claimed it.

So when a second agent started a report over the same window, the first agent's later
`report note` / `assert` / `screenshot` resolved the pointer to the SECOND agent's run -- and
once that one finished and cleared the pointer, to nothing at all. The observed symptom was a
call answering a bare `no active report` partway through a session, with nothing saying why, and
the report had to be restarted. The displaced run was never even marked: it sat `status:
running` for good with its pointer gone.

Three things are asserted here:
  * a start against a slot held by a DIFFERENT agent token is refused, and the holder's report
    is left exactly as it was;
  * when a take does go through (no token to compare, or --takeover), the displaced run is
    CLOSED as `incomplete`, stamped `superseded_by` and rendered, so its evidence survives;
  * a stranded call SAYS SO -- both the "someone else holds it" case and the "yours was taken"
    case -- instead of the bare `no active report` that reads as "you forgot to start one".
"""

import json
from pathlib import Path

import pytest

from unreal_agent_player import cli
from unreal_agent_player.reporting import session as sess


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_REPORT_NO_OPEN", "1")
    monkeypatch.setattr(sess, "_active", None, raising=False)


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _start(capsys, task, *extra):
    assert cli.main(["report", "start", task, *extra]) == 0
    return _out(capsys)


def test_a_start_by_another_agent_is_refused_and_changes_nothing(capsys):
    """The headline. Agent-a is mid-verification; agent-b's start is refused, and agent-a's
    report and its claim on the slot are untouched."""
    first = _start(capsys, "does the janitor chase", "--agent", "agent-a")
    assert cli.main(["report", "start", "does the bike mount", "--agent", "agent-b"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["busy"] is True
    assert body["held_by"] == "agent-a"
    assert "ONE slot per machine" in body["error"] and "--takeover" in body["error"]
    # Nothing moved: the slot is still agent-a's and its run is still running.
    assert sess.get_active_owner()["agent"] == "agent-a"
    assert str(sess.get_active_run()) == first["run_dir"]
    data = json.loads((Path(first["run_dir"]) / "data.json").read_text(encoding="utf-8"))
    assert data["status"] == "running" and data["superseded_by"] is None


def test_the_positive_control_the_same_agent_can_still_start_a_new_report(capsys):
    """Before trusting the refusal, show the gate opens. One agent starting its next report is
    the ordinary case and must not need --takeover; its own previous run is closed properly."""
    first = _start(capsys, "first question", "--agent", "agent-a")
    second = _start(capsys, "second question", "--agent", "agent-a")
    assert second["run_dir"] != first["run_dir"]
    assert sess.get_active_owner()["agent"] == "agent-a"
    old = json.loads((Path(first["run_dir"]) / "data.json").read_text(encoding="utf-8"))
    assert old["status"] == "incomplete"
    assert old["superseded_by"]["agent"] == "agent-a"


def test_takeover_closes_and_RENDERS_the_displaced_run_instead_of_abandoning_it(capsys):
    """A take that does go through must not lose the other run's evidence. It used to be left
    `status: running` with its pointer overwritten -- no note, no HTML, no record at all."""
    first = _start(capsys, "held by someone", "--agent", "agent-a")
    assert cli.main(["report", "note", "the janitor lunged and missed",
                     "--agent", "agent-a"]) == 0
    capsys.readouterr()
    taken = _start(capsys, "taking it", "--agent", "agent-b", "--takeover")
    assert taken["displaced"]["run_dir"] == first["run_dir"]
    assert taken["displaced"]["agent"] == "agent-a"
    assert "TOOK the machine-wide report slot" in taken["warning"]
    data = json.loads((Path(first["run_dir"]) / "data.json").read_text(encoding="utf-8"))
    assert data["status"] == "incomplete"
    assert data["superseded_by"]["agent"] == "agent-b"
    # The evidence it had collected is still there, and it is readable as a report.
    assert any("janitor lunged" in n["text"] for n in data["notes"])
    assert any("SUPERSEDED" in n["text"] for n in data["notes"])
    assert (Path(first["run_dir"]) / "index.html").exists()


def test_a_write_against_someone_elses_slot_is_refused_not_silently_misfiled(capsys):
    """The other half of the quiet collision: agent-a's note used to land in agent-b's report.
    Putting one agent's evidence in another's run is worse than failing."""
    _start(capsys, "b is verifying", "--agent", "agent-b")
    assert cli.main(["report", "assert", "chase started", "pass", "vel 420",
                     "--agent", "agent-a"]) == 2
    body = _out(capsys)
    assert body["ok"] is False and body["held_by"] == "agent-b"
    assert "not to you" in body["error"]


def test_a_stranded_call_names_the_run_that_lost_the_slot(capsys):
    """The observed symptom, answered. After the taker finishes, the pointer is cleared, and
    agent-a's next call used to get `no active report` -- which reads as "you forgot to start
    one" rather than "yours was taken at 14:02 by agent-b"."""
    first = _start(capsys, "a is verifying", "--agent", "agent-a")
    _start(capsys, "b takes it", "--agent", "agent-b", "--takeover")
    assert cli.main(["report", "finish", "fail", "done", "--agent", "agent-b",
                     "--no-open", "--keep-pie"]) == 0
    capsys.readouterr()
    assert sess.get_active_run() is None
    assert cli.main(["report", "note", "still working", "--agent", "agent-a"]) == 2
    body = _out(capsys)
    assert body["superseded"]["run_dir"] == first["run_dir"]
    assert "LOST the machine-wide report slot" in body["error"]
    assert "agent-b" in body["error"]


def test_with_no_token_the_take_proceeds_but_is_declared(capsys):
    """There is no reliable auto-identity in this harness, so two untokened starts cannot be
    told apart and refusing would break the single-agent case. The take happens -- and says so,
    with the remedy, instead of being invisible."""
    first = _start(capsys, "untokened first")
    assert sess.get_active_owner()["agent"] is None
    second = _start(capsys, "untokened second")
    assert second["displaced"]["run_dir"] == first["run_dir"]
    assert "Pass --agent" in second["warning"]
    assert "--agent" in second["hint"]


def test_a_legacy_bare_path_pointer_is_still_understood(capsys, tmp_path):
    """The pointer used to be a bare path. A checkout mid-upgrade must not read an existing one
    as garbage and silently start writing somewhere else."""
    first = _start(capsys, "written by the old cli", "--agent", "agent-a")
    (Path(sess._reports_root()) / ".active").write_text(first["run_dir"], encoding="utf-8")
    assert str(sess.get_active_run()) == first["run_dir"]
    rec = sess.get_active_owner()
    assert rec["run_dir"] == first["run_dir"] and rec["agent"] is None
    # No recorded owner means no identity to compare, so a tokened agent can still take it --
    # what it must not do is mistake it for an empty slot and leave the old run dangling.
    taken = _start(capsys, "after the upgrade", "--agent", "agent-a")
    assert taken["displaced"]["run_dir"] == first["run_dir"]
