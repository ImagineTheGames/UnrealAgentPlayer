"""`uap batch`: many commands, one process (ClickUp 17tm466ft35).

The per-call cost of `uap` is mostly fixed and mostly not the editor -- a PowerShell host, the
launcher's engine resolve, the Python interpreter and this package's imports, about 0.6s before a
packet leaves the machine. A 20-step sequence paid it 20 times: measured end to end through
`uap.ps1`, 20 separate invocations took 11.0s and the same 20 as one batch took 0.75s.

What these pin is that the saving is in the SETUP and nowhere else: every step runs through the
same `_run_parsed` guard path as a standalone invocation, so the lease, the machine lock and each
verb's own semantics are untouched.
"""

import json

import pytest

from unreal_agent_player import cli


def _lines(capsys):
    return [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines() if ln.strip()]


@pytest.fixture
def steps_run(monkeypatch):
    """Record what each step was dispatched as, without running any real verb."""
    seen = []

    def fake(args):
        seen.append((args.cmd, getattr(args, "project", None), getattr(args, "agent", None)))
        cli._emit({"ok": True, "cmd": args.cmd})
        return 0

    monkeypatch.setattr(cli, "_batch_run_step", fake)
    return seen


def test_every_step_runs_and_each_emits_its_own_line(steps_run, capsys):
    rc = cli.main(["batch", "lease status", "lease status", "lease status"])
    out = _lines(capsys)
    assert rc == 0
    assert len(steps_run) == 3
    assert [o["step"] for o in out[:3]] == [1, 2, 3]      # streamed, not held to the end
    assert out[-1]["batch"] is True and out[-1]["ran"] == 3 and out[-1]["failed"] == 0


def test_a_failing_step_stops_the_rest(monkeypatch, capsys):
    def fake(args):
        cli._emit({"ok": False})
        return 1

    monkeypatch.setattr(cli, "_batch_run_step", fake)
    rc = cli.main(["batch", "lease status", "lease status", "lease status"])
    summary = _lines(capsys)[-1]
    assert rc == 1
    assert summary["ran"] == 1 and summary["not_run"] == 2 and summary["stopped_early"] is True


def test_keep_going_runs_them_all(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_batch_run_step", lambda a: 1)
    cli.main(["batch", "--keep-going", "lease status", "lease status"])
    summary = _lines(capsys)[-1]
    assert summary["ran"] == 2 and summary["failed"] == 2
    assert "stopped_early" not in summary


def test_project_and_agent_are_inherited_but_a_step_may_override(steps_run, capsys):
    cli.main(["batch", "--project", "SchoolsOut", "--agent", "tok",
              "lease status", "lease status --project PBW"])
    capsys.readouterr()
    assert steps_run[0] == ("lease", "SchoolsOut", "tok")
    assert steps_run[1][1] == "PBW"                       # the step's own wins
    assert steps_run[1][2] == "tok"                       # ...and it still keeps the token


def test_json_array_form_avoids_every_quoting_question(steps_run, tmp_path, capsys):
    f = tmp_path / "steps.json"
    f.write_text(json.dumps([["exec", "print('a b')"], ["rc", "GetPIEPhase"]]), encoding="utf-8")
    cli.main(["batch", "--file", str(f)])
    capsys.readouterr()
    assert [s[0] for s in steps_run] == ["exec", "rc"]


def test_line_form_skips_blanks_and_comments(steps_run, tmp_path, capsys):
    f = tmp_path / "steps.txt"
    f.write_text("# a comment\nlease status\n\nlease status\n", encoding="utf-8")
    cli.main(["batch", "--file", str(f)])
    capsys.readouterr()
    assert len(steps_run) == 2


def test_an_empty_batch_is_refused_rather_than_reported_as_success(tmp_path, capsys):
    f = tmp_path / "empty.txt"
    f.write_text("\n\n", encoding="utf-8")
    assert cli.main(["batch", "--file", str(f)]) == 2
    assert _lines(capsys)[-1]["ok"] is False


def test_a_step_with_bad_arguments_is_recorded_not_fatal(monkeypatch, capsys):
    rc = cli.main(["batch", "--keep-going", "no-such-verb", "also-not-a-verb"])
    summary = _lines(capsys)[-1]
    assert rc == 1
    assert summary["ran"] == 2 and summary["failed"] == 2
    assert summary["results"][0]["exit_code"] == 2
