"""Reports are one per --agent token, so concurrent agents never share or lose evidence.

History: the report slot used to be ONE per machine (a single `~/.uap-reports/.active`
pointer). A second agent's `report start` took the slot (or had to wait behind the first
agent's open report, 45 minutes on 2026-11-13), and every tokenless / foreign auto-captured
call landed in whichever report held the pointer -- a Marble Race agent's first two calls
appeared in a chalkboard agent's timeline.

Now each token has its own claim record under `~/.uap-reports/.active.d/`. Asserted here:
  * two tokened agents hold open reports at once, interleave steps and screenshots, finish
    both, and each report holds ONLY its own;
  * a call with no token while several reports are open is refused (never guessed), and writes
    nothing anywhere;
  * a tokened call never falls through to someone else's report, and auto-captured verbs are
    recorded only into the calling agent's report;
  * single-agent behaviour (one report, tokenless) is unchanged.
Reports only RECORD: editor / PIE ownership is the lease's job and is not touched here.
"""

import json
import shutil
from pathlib import Path

import pytest

from unreal_agent_player import cli
from unreal_agent_player.reporting import session as sess

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f0005000201a6cd1b8d0000000049454e44ae426082")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_REPORT_NO_OPEN", "1")
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    # `report finish` best-effort stops PIE over RC; pin an unreachable port so a live editor on
    # this machine is never touched by the suite.
    monkeypatch.setenv("UAP_RC_PORT", "1")
    monkeypatch.setattr(sess, "_active", None, raising=False)
    cli._OUT["agent"] = ""


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _start(capsys, task, *extra):
    assert cli.main(["report", "start", task, *extra]) == 0
    return _out(capsys)


def _data(run_dir):
    return json.loads((Path(run_dir) / "data.json").read_text(encoding="utf-8"))


def _shot(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(PNG)
    return str(p)


def _finish(capsys, agent, verdict="fail", summary="done"):
    assert cli.main(["report", "finish", verdict, summary, "--agent", agent,
                     "--no-open", "--keep-pie"]) == 0
    return _out(capsys)


def test_two_agents_hold_open_reports_at_once_and_each_report_holds_only_its_own(
        capsys, tmp_path):
    """The headline. Marble agent starts while chalkboard agent's report is open: no wait, no
    refusal; steps and screenshots interleave; both finish; each index.html is clean."""
    chalk = _start(capsys, "does the chalkboard draw", "--agent", "chalk")
    marble = _start(capsys, "does the marble race run", "--agent", "marble")
    assert chalk["run_dir"] != marble["run_dir"]
    assert len(sess.list_active_slots()) == 2

    for agent, tag in (("chalk", "CHALKA"), ("marble", "MARBLEA"), ("chalk", "CHALKB"),
                       ("marble", "MARBLEB")):
        assert cli.main(["report", "note", f"note-{tag}", "--agent", agent]) == 0
        assert cli.main(["report", "assert", f"assert-{tag}", "pass", f"ev-{tag}",
                         "--agent", agent]) == 0
        assert cli.main(["report", "screenshot", _shot(tmp_path, f"{tag}.png"),
                         "--caption", f"cap-{tag}", "--agent", agent]) == 0
        capsys.readouterr()

    f1 = _finish(capsys, "chalk")
    # marble is still open and writable after chalk finished
    assert cli.main(["report", "note", "after-chalk-finished", "--agent", "marble"]) == 0
    capsys.readouterr()
    f2 = _finish(capsys, "marble")

    for fin, mine, other in ((f1, "CHALK", "MARBLE"), (f2, "MARBLE", "CHALK")):
        html = Path(fin["html"]).read_text(encoding="utf-8")
        for suffix in ("A", "B"):
            assert f"note-{mine}{suffix}" in html and f"assert-{mine}{suffix}" in html
            assert f"cap-{mine}{suffix}" in html
        assert f"{other}A" not in html and f"{other}B" not in html
        data = _data(Path(fin["html"]).parent)
        assert data["agent"] == mine.lower()
        assert len(data["screenshots"]) == 2 and len(data["assertions"]) == 2
    assert "after-chalk-finished" in Path(f2["html"]).read_text(encoding="utf-8")
    assert sess.list_active_slots() == []
    assert not (Path(sess._reports_root()) / ".active").exists()


def test_a_call_with_no_token_is_refused_when_several_reports_are_open(capsys, tmp_path):
    a = _start(capsys, "run a", "--agent", "agent-a")
    b = _start(capsys, "run b", "--agent", "agent-b")
    for argv in (["report", "note", "who am i"],
                 ["report", "assert", "x", "pass", "y"],
                 ["report", "screenshot", _shot(tmp_path, "s.png")],
                 ["report", "finish", "fail", "nope", "--no-open", "--keep-pie"]):
        assert cli.main(argv) == 2
        body = _out(capsys)
        assert body["ok"] is False and body["ambiguous"] is True
        assert "--agent" in body["error"] and "Refusing to guess" in body["error"]
        assert {r["agent"] for r in body["open_reports"]} == {"agent-a", "agent-b"}
    # Nothing was written to either report, and both are still open.
    for r in (a, b):
        d = _data(r["run_dir"])
        assert d["notes"] == [] and d["assertions"] == [] and d["screenshots"] == []
        assert d["status"] == "running"


def test_single_agent_without_a_token_is_unchanged(capsys):
    """Backward compatibility: one report, no tokens anywhere, the old workflow."""
    run = _start(capsys, "solo")
    assert run["agent"] is None and "hint" in run
    assert cli.main(["report", "note", "hello"]) == 0
    assert cli.main(["report", "assert", "ok", "pass", "ev"]) == 0
    capsys.readouterr()
    assert cli.main(["report", "finish", "fail", "s", "--no-open", "--keep-pie"]) == 0
    assert sess.list_active_slots() == []


def test_a_single_tokened_report_still_answers_a_tokenless_call(capsys):
    """One open report -> nothing to be ambiguous about; the sole report is used."""
    run = _start(capsys, "solo tokened", "--agent", "agent-a")
    assert cli.main(["report", "note", "tokenless note"]) == 0
    assert [n["text"] for n in _data(run["run_dir"])["notes"]] == ["tokenless note"]


def test_a_token_with_no_report_never_falls_through_to_someone_elses(capsys):
    """B never started. Its calls must say so, not land in A's open report."""
    a = _start(capsys, "a is verifying", "--agent", "agent-a")
    assert cli.main(["report", "note", "b writes", "--agent", "agent-b"]) == 2
    body = _out(capsys)
    assert body["ok"] is False and "agent-b" in body["error"]
    assert _data(a["run_dir"])["notes"] == []


def test_auto_capture_records_only_into_the_calling_agents_report(capsys):
    a = _start(capsys, "a", "--agent", "agent-a")
    b = _start(capsys, "b", "--agent", "agent-b")
    cli._OUT["agent"] = "agent-b"
    cli._capture("status", {"x": 1}, {"ok": True}, 5)
    cli._OUT["agent"] = "agent-a"
    cli._capture("pie:start", {}, {"ok": True}, 5)
    # tokenless auto-capture with two open reports is ambiguous: it records nowhere.
    cli._OUT["agent"] = ""
    cli._capture("stray", {}, {"ok": True}, 5)
    assert [t["tool"] for t in _data(a["run_dir"])["timeline"]] == ["pie:start"]
    assert [t["tool"] for t in _data(b["run_dir"])["timeline"]] == ["status"]


def test_a_call_before_the_report_is_started_is_not_recorded_in_anothers(capsys):
    """The Marble Race symptom: its first calls (before its own `report start`) appeared in the
    chalkboard agent's timeline."""
    a = _start(capsys, "chalkboard", "--agent", "chalk")
    cli._OUT["agent"] = "marble"
    cli._capture("status", {}, {"ok": True}, 5)
    cli._capture("pie:start", {}, {"ok": True}, 5)
    assert _data(a["run_dir"])["timeline"] == []


def test_the_same_agent_starting_again_closes_its_own_previous_run(capsys):
    first = _start(capsys, "first question", "--agent", "agent-a")
    other = _start(capsys, "someone else", "--agent", "agent-b")
    second = _start(capsys, "second question", "--agent", "agent-a")
    assert second["run_dir"] != first["run_dir"]
    assert second["displaced"]["run_dir"] == first["run_dir"]
    old = _data(first["run_dir"])
    assert old["status"] == "incomplete" and old["superseded_by"]["agent"] == "agent-a"
    assert (Path(first["run_dir"]) / "index.html").exists()
    assert _data(other["run_dir"])["status"] == "running"      # the neighbour is untouched
    assert len(sess.list_active_slots()) == 2


def test_a_stranded_call_names_the_run_that_was_closed(capsys):
    first = _start(capsys, "a is verifying", "--agent", "agent-a")
    _start(capsys, "a again", "--agent", "agent-a")
    _finish(capsys, "agent-a")
    assert cli.main(["report", "note", "still working", "--agent", "agent-a"]) == 2
    body = _out(capsys)
    assert body["superseded"]["run_dir"] == first["run_dir"]
    assert "CLOSED as superseded" in body["error"]


def test_a_tokenless_start_beside_a_tokened_report_is_refused_and_changes_nothing(capsys):
    a = _start(capsys, "held by a", "--agent", "agent-a")
    assert cli.main(["report", "start", "no token"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["busy"] is True and body["held_by"] == "agent-a"
    assert "--agent <your-token>" in body["error"] and "--takeover" in body["error"]
    assert _data(a["run_dir"])["status"] == "running"
    assert sess.get_active_owner("agent-a")["run_dir"] == a["run_dir"]


def test_takeover_closes_and_renders_the_one_other_open_report(capsys):
    a = _start(capsys, "held by a", "--agent", "agent-a")
    assert cli.main(["report", "note", "evidence so far", "--agent", "agent-a"]) == 0
    capsys.readouterr()
    taken = _start(capsys, "taking it", "--takeover")
    assert taken["displaced"]["run_dir"] == a["run_dir"]
    data = _data(a["run_dir"])
    assert data["status"] == "incomplete"
    assert any("evidence so far" in n["text"] for n in data["notes"])
    assert (Path(a["run_dir"]) / "index.html").exists()
    assert len(sess.list_active_slots()) == 1


def test_takeover_refuses_when_several_other_reports_are_open(capsys):
    _start(capsys, "a", "--agent", "agent-a")
    _start(capsys, "b", "--agent", "agent-b")
    assert cli.main(["report", "start", "greedy", "--takeover"]) == 1
    assert len(sess.list_active_slots()) == 2


def test_UAP_AGENT_ID_is_the_identity_when_no_flag_is_passed(capsys, monkeypatch):
    monkeypatch.setenv("UAP_AGENT_ID", "env-agent")
    run = _start(capsys, "via env")
    assert run["agent"] == "env-agent"
    other = _start(capsys, "other", "--agent", "flag-agent")
    assert cli.main(["report", "note", "mine"]) == 0           # env token -> env-agent's report
    assert [n["text"] for n in _data(run["run_dir"])["notes"]] == ["mine"]
    assert _data(other["run_dir"])["notes"] == []


def test_odd_tokens_get_distinct_slots(capsys):
    a = _start(capsys, "a", "--agent", "who/am:i")
    b = _start(capsys, "b", "--agent", "who_am_i")
    assert a["run_dir"] != b["run_dir"] and len(sess.list_active_slots()) == 2


def test_same_task_same_second_gets_distinct_run_dirs(capsys):
    a = _start(capsys, "same question", "--agent", "agent-a")
    b = _start(capsys, "same question", "--agent", "agent-b")
    assert a["run_dir"] != b["run_dir"]


def test_a_finished_or_deleted_run_is_not_an_open_report(capsys):
    a = _start(capsys, "gone", "--agent", "agent-a")
    (Path(a["run_dir"]) / "data.json").unlink()
    b = _start(capsys, "alive", "--agent", "agent-b")
    assert [r["agent"] for r in sess.list_active_slots()] == ["agent-b"]
    assert cli.main(["report", "note", "no token needed now"]) == 0
    assert [n["text"] for n in _data(b["run_dir"])["notes"]] == ["no token needed now"]


def test_a_legacy_bare_path_pointer_is_still_understood(capsys):
    """A report started by the old CLI (bare-path `.active`, no slot record) must not read as
    absent mid-upgrade."""
    first = _start(capsys, "written by the old cli", "--agent", "agent-a")
    shutil.rmtree(sess._slots_dir())
    (Path(sess._reports_root()) / ".active").write_text(first["run_dir"], encoding="utf-8")
    assert str(sess.get_active_run()) == first["run_dir"]
    rec = sess.get_active_owner()
    assert rec["run_dir"] == first["run_dir"] and rec["agent"] is None
    assert cli.main(["report", "note", "still works"]) == 0
