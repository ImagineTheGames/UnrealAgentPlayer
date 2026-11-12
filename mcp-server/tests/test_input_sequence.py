"""`uap input sequence`: a whole timed input script in ONE call [17tm466zqax].

`input hold` starts counting when the call ARRIVES, and the CLI-to-engine hop adds 0.2-0.6 s of
jitter to that, so an agent could not hit a timing window under ~1.5 s (vanishing platforms,
obstacle gaps, a jump 0.85 s into a run); Cloud Race testers faked jumps with in-engine
ACharacter::Jump calls. The fix is that the script is parsed here, shipped as JSON in one call,
and PLAYED IN-ENGINE against world time (RunInputSequence), so the round trip no longer matters.

These tests pin the CLI half: the grammar, what goes on the wire, and the refusals. The
engine half is measured in a live PIE and reported in the ticket.
"""

import json

import pytest

from unreal_agent_player import cli, input_script


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --- the grammar -------------------------------------------------------------------------

def test_the_ticket_example_parses():
    """`hold W 0-2.0s; press Jump at 0.85s` is the example the ticket gave."""
    steps = input_script.parse_script("hold W 0-2.0s; press SpaceBar at 0.85s")
    assert steps == [
        {"kind": "hold", "key": "W", "start": 0.0, "end": 2.0},
        {"kind": "hold", "key": "SpaceBar", "start": 0.85, "end": 0.95},
    ]


@pytest.mark.parametrize("text, start, end", [
    ("hold W 0-2", 0.0, 2.0),
    ("hold W 0.5s-1.5s", 0.5, 1.5),
    ("hold W 500ms-1500ms", 0.5, 1.5),
    ("hold W at 0.25 for 1", 0.25, 1.25),
    ("hold W at 250ms for 750ms", 0.25, 1.0),
    ("hold W from 1 to 3", 1.0, 3.0),
    ("HOLD W AT 1 FOR 2", 1.0, 3.0),
    ("press SpaceBar at 1", 1.0, 1.1),
    ("press SpaceBar 1", 1.0, 1.1),
    ("press SpaceBar at 1 for 0.3s", 1.0, 1.3),
])
def test_every_spelling_gives_the_same_step(text, start, end):
    (step,) = input_script.parse_script(text)
    assert step["kind"] == "hold" and step["key"] == (text.split()[1])
    assert step["start"] == pytest.approx(start) and step["end"] == pytest.approx(end)


def test_axis_carries_its_value_and_may_be_negative():
    steps = input_script.parse_script("axis Gamepad_LeftY -1 0-2s\naxis Gamepad_LeftX 0.5 at 1 for 1")
    assert steps[0] == {"kind": "axis", "key": "Gamepad_LeftY", "value": -1.0, "start": 0.0, "end": 2.0}
    assert steps[1]["value"] == 0.5 and steps[1]["start"] == 1.0 and steps[1]["end"] == 2.0


def test_newlines_semicolons_and_comments():
    steps = input_script.parse_script("# run\nhold W 0-3;\n\nhold A 1-2  # strafe")
    assert [s["key"] for s in steps] == ["W", "A"]


def test_json_script_is_accepted_as_a_list_or_an_object():
    one = {"kind": "hold", "key": "W", "start": 0, "end": 1}
    assert input_script.parse_script(json.dumps([one]))[0]["key"] == "W"
    assert input_script.parse_script(json.dumps({"steps": [one]}))[0]["end"] == 1.0


@pytest.mark.parametrize("text, fragment", [
    ("", "empty script"),
    ("jump W 0-1", "unknown action"),
    ("hold", "no key"),
    ("hold W", "no timing"),
    ("hold W 2-1", "start < end"),
    ("hold W at 1", "needs `for"),
    ("hold W 1", "not a range"),
    ("hold W at x for 1", "not a time"),
    ("axis Gamepad_LeftY 0-1", "needs a value"),
    ("hold W at 1 for", "unexpected"),
])
def test_a_script_that_cannot_be_read_says_which_statement(text, fragment):
    with pytest.raises(ValueError) as exc:
        input_script.parse_script(text)
    assert fragment in str(exc.value)


# --- the verb ----------------------------------------------------------------------------

def _wire(monkeypatch, *, run=None, log=None):
    calls: list[tuple[str, dict]] = []

    def fake(func, params, project=None):
        calls.append((func, params))
        if func == "RunInputSequence":
            return json.dumps(run or {"ok": True, "sequence": 7, "steps": 2, "total_seconds": 2.0,
                                      "starts_in_seconds": 0.0, "clock": "world"})
        if func == "GetInputSequenceLog":
            return json.dumps(log or {"ok": True, "sequences": []})
        raise AssertionError(f"unexpected RC call {func}")

    monkeypatch.setattr(cli, "_rc_call", fake)
    return calls


def test_the_whole_script_goes_out_in_one_call(capsys, monkeypatch):
    """The point of the verb: ONE round trip carries every step, so none of them waits on the CLI."""
    calls = _wire(monkeypatch)
    assert cli.main(["input", "sequence", "hold W 0-2.0s; press SpaceBar at 0.85s"]) == 0
    assert [c[0] for c in calls] == ["RunInputSequence"]
    sent = json.loads(calls[0][1]["ScriptJson"])
    assert [(s["key"], s["start"], s["end"]) for s in sent["steps"]] == \
        [("W", 0.0, 2.0), ("SpaceBar", 0.85, 0.95)]
    assert calls[0][1]["StartAt"] == ""
    body = _out(capsys)
    assert body["ok"] is True and body["sequence"] == 7 and "NOT blocking" in body["note"]


def test_at_is_passed_through_untouched(capsys, monkeypatch):
    calls = _wire(monkeypatch)
    assert cli.main(["input", "sequence", "hold W 0-1", "--at", "+1.5"]) == 0
    assert calls[0][1]["StartAt"] == "+1.5"


def test_a_bad_script_never_reaches_the_engine(capsys, monkeypatch):
    calls = _wire(monkeypatch)
    assert cli.main(["input", "sequence", "hold W 2-1"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and body["pressed"] is False and "start < end" in body["error"]
    assert calls == []


def test_dry_run_parses_and_sends_nothing(capsys, monkeypatch):
    calls = _wire(monkeypatch)
    assert cli.main(["input", "sequence", "hold W 0-2; press SpaceBar at 1", "--dry-run"]) == 0
    body = _out(capsys)
    assert body["dry_run"] is True and body["total_seconds"] == pytest.approx(2.0)
    assert len(body["steps"]) == 2 and calls == []


def test_script_from_file(capsys, monkeypatch, tmp_path):
    calls = _wire(monkeypatch)
    f = tmp_path / "run.txt"
    f.write_text("hold W 0-1\npress SpaceBar at 0.5", encoding="utf-8")
    assert cli.main(["input", "sequence", "--file", str(f)]) == 0
    assert len(json.loads(calls[0][1]["ScriptJson"])["steps"]) == 2


def test_the_engines_refusal_is_relayed_verbatim(capsys, monkeypatch):
    _wire(monkeypatch, run={"ok": False, "pressed": False, "error": "W is already held (1.00 s left)"})
    assert cli.main(["input", "sequence", "hold W 0-1"]) == 1
    assert "already held" in _out(capsys)["error"]


def test_wait_reports_how_far_each_step_fired_from_the_script(capsys, monkeypatch):
    """--wait blocks for the script, then reads the engine's own record of when each step fired."""
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    log = {"ok": True, "sequences": [{
        "sequence": 7, "state": "done", "max_error_ms": 11.0,
        "steps": [{"step": 0, "key": "W", "start": 0.0, "end": 0.4, "start_error_ms": 0.0,
                   "end_error_ms": 9.0},
                  {"step": 1, "key": "SpaceBar", "start": 0.2, "end": 0.3, "start_error_ms": 11.0,
                   "end_error_ms": 5.0}]}]}
    _wire(monkeypatch, run={"ok": True, "sequence": 7, "steps": 2, "total_seconds": 0.4,
                            "starts_in_seconds": 0.0}, log=log)
    assert cli.main(["input", "sequence", "hold W 0-0.4; press SpaceBar at 0.2", "--wait"]) == 0
    body = _out(capsys)
    assert body["waited"] is True and body["playback"]["state"] == "done"
    assert body["playback"]["max_error_ms"] == 11.0
    assert body["playback"]["errors_ms"] == [0.0, 11.0, 9.0, 5.0]


def test_wait_fails_when_a_step_could_not_be_pressed(capsys, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    log = {"ok": True, "sequences": [{"sequence": 7, "state": "done", "max_error_ms": 0,
                                      "steps": [{"step": 0, "key": "W", "failure": "no live viewport"}]}]}
    _wire(monkeypatch, run={"ok": True, "sequence": 7, "starts_in_seconds": 0.0}, log=log)
    assert cli.main(["input", "sequence", "hold W 0-1", "--wait"]) == 1
    assert "did not play out cleanly" in _out(capsys)["error"]


def test_sequence_status_reads_the_engine_log(capsys, monkeypatch):
    calls = _wire(monkeypatch, log={"ok": True, "sequences": [{"sequence": 3, "state": "done"}]})
    assert cli.main(["input", "sequence-status"]) == 0
    assert calls[0][0] == "GetInputSequenceLog"
    assert _out(capsys)["sequences"][0]["sequence"] == 3


def test_an_older_plugin_without_the_verb_is_named_as_skew(capsys, monkeypatch):
    """No bare 404: a project that has not rebuilt must be told what to do."""
    from unreal_agent_player.errors import AgentError, ErrorCode

    def fake(func, params, project=None):
        raise AgentError(ErrorCode.UE_UNREACHABLE, f"HTTP 404 for {func}")

    monkeypatch.setattr(cli, "_rc_call", fake)
    monkeypatch.setattr(cli, "_is_missing_verb", lambda exc: True)
    assert cli.main(["input", "sequence", "hold W 0-1"]) == 1
    assert "RunInputSequence" in _out(capsys)["error"]


def test_the_verbs_take_a_client(monkeypatch):
    """Both new verbs are per-client: without `Client` on the wire a --client N call would silently
    run on the first client."""
    assert {"RunInputSequence", "GetInputSequenceLog"} <= cli._CLIENT_VERBS
