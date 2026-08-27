"""CLI cover for the verbs added to close the 2026-08-27 verification-session gaps:
sustained input, frame-rate sampling, log reading, helper listing, VR-preview PIE, and
--agent being accepted everywhere.
"""

import json

import pytest

from unreal_agent_player import cli
from unreal_agent_player.errors import AgentError, ErrorCode


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    # No exec-based port resolution in tests.
    monkeypatch.setenv("UAP_RC_PORT", "30010")


def _stub_rc(monkeypatch, table):
    """Route _rc_call through a {func: value-or-callable} table and record the calls."""
    seen = []

    def fake(func, params, project=None):
        seen.append((func, params, project))
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val(params) if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --- P3: ListTestHelpers must come back with names -------------------------------------

_HELPERS_JSON = json.dumps({"helpers": [
    {"name": "USchoolsOutTestHelpers::GetJanitorState", "category": "Janitor",
     "tooltip": "", "phase": "Playing",
     "arg_schema": {"type": "object", "properties": {}},
     "return_schema": {"type": "string"}, "supported": True, "unsupported_reason": ""},
    {"name": "USchoolsOutTestHelpers::GetPlayerSpeed", "category": "Player",
     "tooltip": "", "phase": "Playing", "arg_schema": None,
     "return_schema": None, "supported": True, "unsupported_reason": ""},
]})


def test_helpers_verb_returns_names_not_empty_objects(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"ListTestHelpersJson": _HELPERS_JSON})
    assert cli.main(["helpers", "--names"]) == 0
    body = _out(capsys)
    assert body["count"] == 2
    assert body["helpers"] == ["USchoolsOutTestHelpers::GetJanitorState",
                               "USchoolsOutTestHelpers::GetPlayerSpeed"]


def test_helpers_grep_filters_by_name_or_category(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"ListTestHelpersJson": _HELPERS_JSON})
    assert cli.main(["helpers", "--grep", "janitor", "--names"]) == 0
    assert _out(capsys)["helpers"] == ["USchoolsOutTestHelpers::GetJanitorState"]


def test_rc_ListTestHelpers_is_routed_to_the_json_twin(monkeypatch, capsys):
    """`uap rc ListTestHelpers` is the documented incantation and must keep its data.

    RemoteControl's preset-call route filters serialized properties down to the function's
    own out/return params, so the TArray<FAgentHelperDescriptor> return arrives as
    [{},{},...] -- 13 helpers, zero usable information. The CLI transparently calls the
    plugin's JSON-string twin instead.
    """
    seen = _stub_rc(monkeypatch, {"ListTestHelpersJson": _HELPERS_JSON})
    assert cli.main(["rc", "ListTestHelpers"]) == 0
    body = _out(capsys)
    assert [c[0] for c in seen] == ["ListTestHelpersJson"]
    assert body["via"] == "ListTestHelpersJson"
    assert body["result"]["helpers"][0]["name"].endswith("GetJanitorState")


def test_rc_passthrough_is_unaffected(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"GetPluginVersion": "0.0.1"})
    assert cli.main(["rc", "GetPluginVersion"]) == 0
    assert seen[0][0] == "GetPluginVersion"
    assert _out(capsys)["result"] == "0.0.1"


# --- P1: sustained input ----------------------------------------------------------------

def test_input_hold_sends_holdkey_and_does_not_block(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"HoldKey": json.dumps(
        {"ok": True, "key": "W", "seconds": 3.0, "pressed": True})})
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("hold must not block by default"))
    assert cli.main(["input", "hold", "W", "--seconds", "3"]) == 0
    assert seen[0][0] == "HoldKey"
    assert seen[0][1] == {"KeyName": "W", "Seconds": 3.0}
    body = _out(capsys)
    assert body["ok"] and body["key"] == "W" and body["pressed"] is True


def test_input_axis_drives_a_vr_thumbstick(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"HoldAxis": json.dumps(
        {"ok": True, "key": "OculusTouch_Left_Thumbstick_Y", "value": 1.0,
         "seconds": 2.5, "pressed": True})})
    assert cli.main(["input", "axis", "OculusTouch_Left_Thumbstick_Y", "1.0",
                     "--seconds", "2.5"]) == 0
    assert seen[0][1] == {"AxisKeyName": "OculusTouch_Left_Thumbstick_Y",
                          "Value": 1.0, "Seconds": 2.5}
    assert _out(capsys)["ok"] is True


# --- Regression: a refused hold must leave NO key pressed -------------------------------
# `uap input hold C` refused with a guessed "unknown key name" while having ALREADY pressed C:
# the pawn was left permanently crouched, `input status` showed nothing held, and `release`
# could not clear it. Root cause was reading UGameViewportClient::InputKey's "handled" bit as
# a success signal (it is false for every key under Enhanced Input). The plugin now validates
# before pressing and reports `pressed:false` on refusal, which is the contract asserted here.

def test_a_refused_hold_reports_that_it_pressed_nothing(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"HoldKey": json.dumps(
        {"ok": False, "key": "NotAKey", "pressed": False,
         "error": "no key named 'NotAKey' in this engine's FKey registry. Use the exact "
                  "FKey name (e.g. W, C, LeftControl, SpaceBar)."})})
    assert cli.main(["input", "hold", "NotAKey"]) == 1
    body = _out(capsys)
    assert body["ok"] is False
    assert body["pressed"] is False, "a refused hold must have zero side effects"


def test_cli_relays_the_plugins_reason_and_never_invents_one(monkeypatch, capsys):
    """The CLI used to print 'unknown key name' for ANY false, which was wrong for a valid
    key and sent a live investigation after a validation table that does not exist."""
    _stub_rc(monkeypatch, {"HoldKey": json.dumps(
        {"ok": False, "key": "W", "pressed": False,
         "error": "no live game viewport is accepting input -- start PIE first"})})
    assert cli.main(["input", "hold", "W"]) == 1
    body = _out(capsys)
    assert body["error"] == "no live game viewport is accepting input -- start PIE first"
    assert "unknown key name" not in body["error"]


@pytest.mark.parametrize("key", ["C", "LeftControl", "SpaceBar", "Gamepad_LeftY"])
def test_real_fkeys_are_passed_through_untouched(key, monkeypatch, capsys):
    """C and LeftControl are real FKeys. The CLI must not filter key names against any list
    of its own -- validation belongs to the engine's FKey registry."""
    seen = _stub_rc(monkeypatch, {"HoldKey": json.dumps(
        {"ok": True, "key": key, "seconds": 1.0, "pressed": True})})
    assert cli.main(["input", "hold", key]) == 0
    assert seen[0][1]["KeyName"] == key


# --- Regression: release must be able to recover a key the registry lost ----------------

def test_release_all_is_the_recovery_path(monkeypatch, capsys):
    """Bare `input release` must clear keys the registry never knew about, so one stuck key
    cannot silently corrupt every later test in the same PIE session."""
    seen = _stub_rc(monkeypatch, {"ReleaseHeldInput": json.dumps(
        {"ok": True, "released": 1, "controllers_flushed": 1, "flushed": True})})
    assert cli.main(["input", "release"]) == 0
    assert seen[0][1] == {"KeyName": ""}
    body = _out(capsys)
    assert body["released"] == 1
    assert body["flushed"] is True


def test_release_by_name_force_releases_an_unregistered_key(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"ReleaseHeldInput": json.dumps(
        {"ok": True, "key": "C", "released": 0, "was_held": False,
         "forced": True, "down_before": True})})
    assert cli.main(["input", "release", "C"]) == 0
    assert seen[0][1] == {"KeyName": "C"}
    body = _out(capsys)
    assert body["forced"] is True
    assert body["down_before"] is True, "reports that it really was stuck before the release"


def test_input_status_reports_engine_ground_truth(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"GetHeldInput": json.dumps({"ok": True, "held": [
        {"key": "W", "analog": False, "value": 1.0, "remaining_seconds": 1.2, "down": True}]})})
    assert cli.main(["input", "status"]) == 0
    held = _out(capsys)["held"]
    assert held[0]["key"] == "W"
    assert held[0]["down"] is True


# --- P7: frame-rate sampling ------------------------------------------------------------

def test_sample_start_waits_then_reads_and_derives_stats(monkeypatch, capsys):
    series = {"ok": True, "active": False, "object": "PlayerCameraManager",
              "property": "WorldLocation", "count": 3, "samples": [
                  {"t": 0.000, "v": {"x": 0.0, "y": 0.0, "z": 0.0}},
                  {"t": 0.011, "v": {"x": 1.0, "y": 0.0, "z": 0.0}},
                  {"t": 0.022, "v": {"x": 5.0, "y": 0.0, "z": 0.0}}]}
    _stub_rc(monkeypatch, {
        "StartPropertySample": json.dumps({"ok": True, "object": "PlayerCameraManager"}),
        "ReadPropertySample": json.dumps(series),
    })
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["sample", "start", "PlayerCameraManager", "WorldLocation",
                     "--seconds", "1"]) == 0
    body = _out(capsys)
    assert body["count"] == 3
    # deltas are 1.0 then 4.0 -> the max IS the judder spike a 1Hz sample cannot see
    assert body["stats"]["delta_max"] == 4.0
    assert body["stats"]["delta_mean"] == 2.5


def test_sample_start_reports_a_bad_object_path(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"StartPropertySample": json.dumps(
        {"ok": False, "error": "no actor named/containing 'Nope' in the live world"})})
    assert cli.main(["sample", "start", "Nope", "WorldLocation"]) == 1
    assert "no actor" in _out(capsys)["error"]


def test_sample_summary_drops_the_raw_series(monkeypatch, capsys):
    _stub_rc(monkeypatch, {
        "StartPropertySample": json.dumps({"ok": True}),
        "ReadPropertySample": json.dumps(
            {"ok": True, "count": 2, "samples": [{"t": 0.0, "v": 1.0}, {"t": 0.01, "v": 2.0}]}),
    })
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["sample", "start", "PlayerPawn", "X", "--summary"]) == 0
    body = _out(capsys)
    assert "samples" not in body
    assert body["stats"]["delta_max"] == 1.0


def test_sample_stats_ignores_a_single_sample():
    assert cli._sample_stats([{"t": 0.0, "v": 1.0}]) == {}


# --- P5: log verb -----------------------------------------------------------------------

_LOG_JSON = json.dumps({"cursor": 42, "lines": [
    {"cursor": 40, "category": "LogJanitor", "verbosity": "Log", "message": "Catch windup"},
    {"cursor": 41, "category": "LogTemp", "verbosity": "Log", "message": "unrelated"},
]})


def test_log_tail_grabs_a_cursor_then_reads(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"GetLogCursor": 100, "GetLogsSince": _LOG_JSON})
    assert cli.main(["log", "tail", "--lines", "10"]) == 0
    assert [c[0] for c in seen] == ["GetLogCursor", "GetLogsSince"]
    assert seen[1][1]["AfterCursor"] == 90
    body = _out(capsys)
    assert body["count"] == 2 and body["cursor"] == 42


def test_log_grep_filters_messages(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"GetLogCursor": 100, "GetLogsSince": _LOG_JSON})
    assert cli.main(["log", "tail", "--grep", "catch"]) == 0
    body = _out(capsys)
    assert body["count"] == 1
    assert body["lines"][0]["category"] == "LogJanitor"


def test_log_since_uses_the_supplied_cursor(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"GetLogsSince": _LOG_JSON})
    assert cli.main(["log", "since", "--since", "37"]) == 0
    assert seen[0][1]["AfterCursor"] == 37


def test_log_since_accepts_a_positional_cursor(monkeypatch, capsys):
    """`uap help` documents `log since <cursor>` positionally; argparse used to reject it, so
    the documented incantation was an error."""
    seen = _stub_rc(monkeypatch, {"GetLogsSince": _LOG_JSON})
    assert cli.main(["log", "since", "37"]) == 0
    assert seen[0][1]["AfterCursor"] == 37


def test_help_text_matches_the_log_since_syntax():
    """The catalog and the parser must not disagree about how a verb is invoked: `uap help`
    showed the positional form while argparse only accepted --since."""
    lines = [ln for ln in cli._HELP_CATALOG.splitlines() if "uap log since" in ln]
    assert lines, "help catalog should document `uap log since`"
    for line in lines:
        tokens = line.split()
        cursor_arg = tokens[tokens.index("since") + 1]
        # Every form the help shows passes a cursor positionally; it must parse.
        assert not cursor_arg.startswith("-"), f"help shows a flag, not a positional: {line}"
        assert cli.build_parser().parse_args(["log", "since", "42"]).cursor == 42


def test_log_bad_regex_is_a_usage_error(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"GetLogCursor": 5, "GetLogsSince": _LOG_JSON})
    assert cli.main(["log", "tail", "--grep", "("]) == 2


def test_log_cursor_verb(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"GetLogCursor": 77})
    assert cli.main(["log", "cursor"]) == 0
    assert _out(capsys)["cursor"] == 77


# --- P6: VR preview ---------------------------------------------------------------------

def test_pie_start_defaults_to_flat_mode(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"StartPIEMode": json.dumps({"ok": True, "mode": "flat"})})
    assert cli.main(["pie", "start"]) == 0
    assert seen[0][1] == {"Mode": "flat"}


def test_pie_start_vr_requests_vr_preview(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"StartPIEMode": json.dumps({"ok": True, "mode": "vr"})})
    assert cli.main(["pie", "start", "--mode", "vr"]) == 0
    assert seen[0][1] == {"Mode": "vr"}
    assert _out(capsys)["mode"] == "vr"


def test_pie_start_vr_without_a_headset_fails_loudly(monkeypatch, capsys):
    """It must NOT quietly fall back to flat PIE -- that makes an HMD-only bug look absent."""
    _stub_rc(monkeypatch, {"StartPIEMode": json.dumps(
        {"ok": False, "mode": "vr", "error": "no HMD connected; connect the headset"})})
    assert cli.main(["pie", "start", "--mode", "vr"]) == 1
    assert "no HMD connected" in _out(capsys)["error"]


# --- P4: --agent accepted everywhere ----------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["report", "note", "hello"],
    ["report", "assert", "label", "pass", "evidence"],
    ["report", "finish", "pass", "summary"],
    ["report", "start", "question"],
    ["help"],
])
def test_agent_flag_is_accepted_on_non_editor_verbs(argv, tmp_path, monkeypatch):
    """AGENTS.md tells agents to pass the SAME --agent token on every related call, so a verb
    that hard-errors on it ('unrecognized arguments: --agent') breaks the whole run."""
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    parsed = cli.build_parser().parse_args(argv + ["--agent", "tok-123", "--project", "P"])
    assert parsed.agent == "tok-123"
    # --project is the same class of trap and is accepted (ignored) everywhere too.
    assert parsed.project == "P"


def test_agent_flag_still_reaches_editor_verbs():
    parsed = cli.build_parser().parse_args(["rc", "GetPluginVersion", "--agent", "tok-123"])
    assert parsed.agent == "tok-123"
    assert parsed.project is not None


def test_every_verb_accepts_agent_and_project():
    """One flag set across the whole CLI. A verb that rejects either breaks a run mid-flight."""
    p = cli.build_parser()
    for argv in (["status"], ["rc", "F"], ["exec", "x"], ["exec-file", "f"],
                 ["pie", "start"], ["pie", "wait", "5"], ["pie", "stop"],
                 ["read-ui"], ["click", "x"], ["tab", "t"], ["nav", "up"],
                 ["screenshot", "f.png"], ["helpers"],
                 ["input", "hold", "W"], ["input", "axis", "K", "1"],
                 ["input", "release"], ["input", "status"],
                 ["sample", "start", "PlayerPawn", "X"], ["sample", "read"],
                 ["log", "tail"], ["log", "since"], ["log", "cursor"],
                 ["report", "start", "q"], ["report", "assert", "l", "pass", "e"],
                 ["report", "note", "n"], ["report", "diag"],
                 ["report", "screenshot", "f"], ["report", "finish", "pass", "s"],
                 ["lease", "status"], ["lease", "acquire", "exclusive"],
                 ["lease", "release"], ["lease", "heartbeat"], ["help"], ["tools"]):
        parsed = p.parse_args(argv + ["--agent", "tok", "--project", "P"])
        assert parsed.agent == "tok", argv
        assert parsed.project == "P", argv
        assert callable(getattr(parsed, "func", None)), argv


# --- error shape ------------------------------------------------------------------------

def test_transport_errors_surface_a_machine_readable_code(monkeypatch, capsys):
    def boom(func, params, project=None):
        raise AgentError(ErrorCode.UE_CONNECTION_RESET, "reset", retry_hint="retry")

    monkeypatch.setattr(cli, "_rc_call", boom)
    assert cli.main(["log", "cursor"]) == 1
    body = _out(capsys)
    assert body["code"] == "UE_CONNECTION_RESET"
    assert body["retryable"] is True
    assert body["retry_hint"] == "retry"
