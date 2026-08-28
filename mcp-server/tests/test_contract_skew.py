"""CLI <-> plugin contract skew, known BEFORE something breaks.

Background (docs/known-issues.md #23, #28): every project vendors its own copy of the plugin
while all of them share ONE CLI, so a pulled CLI runs ahead of a project until that project
syncs and rebuilds. Until now the CLI only discovered this by FAILING -- a raw RemoteControl
404 on whichever verb someone had remembered to guard -- and could not discover it at all when
the gap was a missing PARAMETER rather than a missing verb.

What is pinned here:
  1. both sides of the comparison are DERIVED (this repo's plugin header vs the editor's live
     RemoteControl preset), so neither is a number anybody has to remember to bump;
  2. the parser really does read the real header -- a parser that quietly matched nothing would
     report "unknown" forever and look harmless;
  3. absence is "unknown", never "current";
  4. `uap rc` encodes a key=value against the DECLARED parameter type, so a numeric-looking
     value for an FString parameter is no longer silently swallowed (ClickUp 86ak7kcm7);
  5. a parameter this editor's copy cannot receive is REFUSED, not sent and lost.
"""

import json

import httpx
import pytest

from unreal_agent_player import cli
from unreal_agent_player import contract as C


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _stub_rc(monkeypatch, table):
    seen = []

    def fake(func, params, project=None):
        seen.append((func, params, project))
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val(params) if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


# A plugin copy that is CURRENT for the verbs these tests touch.
_LIVE_CURRENT = {
    "GetPluginVersion": {},
    "HoldAxis": {"AxisKeyName": "FString", "Value": "float", "Seconds": "float",
                 "SlateUser": "FString"},
    "InjectGamepad": {"Button": "EAgentGamepadButton", "bPressed": "bool",
                      "AnalogValue": "float", "SlateUser": "FString"},
    "InjectKey": {"KeyName": "FString", "bPressed": "bool", "bRepeat": "bool"},
}

# The same copy from BEFORE SlateUser was added: the verb is there, the parameter is not.
# This is the shape that produces no 404 and therefore no error of any kind.
_LIVE_BEHIND = {
    "GetPluginVersion": {},
    "HoldAxis": {"AxisKeyName": "FString", "Value": "float", "Seconds": "float"},
    "InjectGamepad": {"Button": "EAgentGamepadButton", "bPressed": "bool",
                      "AnalogValue": "float"},
    "InjectKey": {"KeyName": "FString", "bPressed": "bool", "bRepeat": "bool"},
}


def _pin_contract(monkeypatch, live):
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: live)


# --- the expected side is really parsed from the real header -----------------------------

def test_expected_contract_is_read_from_this_repos_plugin_header():
    """The guard that matters most. If the regex stops matching the header -- a formatting
    change, a move -- `expected_contract()` returns None, every comparison becomes "unknown",
    and the whole check silently stops working while still looking healthy. Assert on real
    verbs with real arguments, not just a non-empty dict.
    """
    exp = C.expected_contract()
    assert exp is not None, "plugin header not found; the skew check would be permanently blind"
    assert len(exp) > 30, f"only parsed {len(exp)} UFUNCTIONs -- the parser is missing some"
    assert exp["HoldAxis"] == {"AxisKeyName": "FString", "Value": "float",
                               "Seconds": "float", "SlateUser": "FString"}
    assert exp["InjectGamepad"]["SlateUser"] == "FString"
    # multi-line declaration, mixed integer widths, an enum argument
    assert exp["GetLogsSince"] == {"AfterCursor": "int64", "MaxLines": "int32",
                                   "CategoryFilter": "FString",
                                   "MinVerbosity": "EAgentLogVerbosity"}
    # templated return type, no arguments, and a `const` member
    assert exp["ListTestHelpers"] == {}
    assert exp["GetPluginVersion"] == {}


def test_runtime_header_is_parsed_too():
    rt = C.expected_contract("UAP_RuntimePreset")
    assert rt is not None and "HoldAxis" in rt
    assert rt["HoldAxis"]["SlateUser"] == "FString"


def test_missing_header_is_unknown_not_current(tmp_path):
    assert C.expected_contract(root=tmp_path) is None
    assert C.compare(None, _LIVE_CURRENT)["state"] == "unknown"


# --- the live side ------------------------------------------------------------------------

_PRESET_BODY = {"Preset": {"Name": "UAP_Preset", "Groups": [{"ExposedFunctions": [
    {"UnderlyingFunction": {"Name": "HoldAxis", "Arguments": [
        {"Name": "AxisKeyName", "Type": "FString"},
        {"Name": "Value", "Type": "float"},
        {"Name": "Seconds", "Type": "float"},
        {"Name": "SlateUser", "Type": "FString"}]}},
    {"UnderlyingFunction": {"Name": "GetPluginVersion", "Arguments": []}},
    {"UnderlyingFunction": {"Name": "ExecuteUbergraph", "Arguments": [
        {"Name": "EntryPoint", "Type": "int32"}]}},
]}]}}


def test_parse_preset_reads_names_and_declared_types():
    live = C.parse_preset(_PRESET_BODY)
    assert live["HoldAxis"]["SlateUser"] == "FString"
    assert live["GetPluginVersion"] == {}
    assert "ExecuteUbergraph" not in live, "UHT plumbing must not read as a plugin verb"


def test_fetch_live_contract_returns_none_on_every_failure(monkeypatch):
    """An unreachable editor, an old RemoteControl, or a garbled body must never break a call
    that would otherwise have worked -- the check is additive or it is nothing."""
    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)
    assert C.fetch_live_contract(30010) is None

    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(404, text="nope"))
    assert C.fetch_live_contract(30010) is None

    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text="not json"))
    assert C.fetch_live_contract(30010) is None

    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: httpx.Response(200, json=_PRESET_BODY))
    assert C.fetch_live_contract(30010)["HoldAxis"]["SlateUser"] == "FString"


# --- the comparison ------------------------------------------------------------------------

def test_current_when_everything_declared_is_present():
    rep = C.compare(_LIVE_CURRENT, _LIVE_CURRENT)
    assert rep["state"] == "current"
    assert C.skew_message(rep, "SchoolsOut") is None


def test_a_missing_parameter_is_behind_even_though_the_verb_exists():
    """The gap a 404 can never see. `HoldAxis` answers; it just cannot receive SlateUser."""
    rep = C.compare(_LIVE_CURRENT, _LIVE_BEHIND)
    assert rep["state"] == "behind"
    assert rep["missing_verbs"] == []
    assert rep["missing_args"] == {"HoldAxis": ["SlateUser"], "InjectGamepad": ["SlateUser"]}


def test_a_missing_verb_is_behind():
    live = dict(_LIVE_CURRENT)
    live.pop("HoldAxis")
    rep = C.compare(_LIVE_CURRENT, live)
    assert rep["state"] == "behind" and rep["missing_verbs"] == ["HoldAxis"]


def test_a_plugin_newer_than_this_checkout_is_reported_but_not_behind():
    """That project rebuilt from a newer commit than the one running this CLI. Nothing breaks
    -- the CLI simply does not call the new verbs -- so it must not be dressed up as skew."""
    live = dict(_LIVE_CURRENT, BrandNewVerb={})
    rep = C.compare(_LIVE_CURRENT, live)
    assert rep["state"] == "current"
    assert rep["plugin_ahead"] == ["BrandNewVerb"]


def test_unreadable_live_side_is_unknown_and_says_why():
    rep = C.compare(_LIVE_CURRENT, None)
    assert rep["state"] == "unknown"
    assert "did not answer" in rep["reason"]


def test_skew_message_has_all_three_parts_of_the_house_rule():
    """agentplayertest.md, "What a good failure message contains": name the gap, say what is
    unavailable because of it, give the exact remedy."""
    msg = C.skew_message(C.compare(_LIVE_CURRENT, _LIVE_BEHIND), "SchoolsOut")
    assert "HoldAxis.SlateUser" in msg                       # 1: the gap, named
    assert "input axis --user N" in msg                      # 2: what it costs you
    assert "sync and rebuild SchoolsOut" in msg              # 3: the one remedy
    assert "Restart-Editor.ps1" in msg
    assert "expected skew, not a broken editor" in msg


# --- uap status surfaces it up front -------------------------------------------------------

def test_status_reports_the_contract_state(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"GetPluginVersion": "0.0.1"})
    _pin_contract(monkeypatch, _LIVE_BEHIND)
    monkeypatch.setattr(C, "expected_contract", lambda *a, **k: _LIVE_CURRENT)
    assert cli.main(["status", "--project", "SchoolsOut"]) == 0
    body = _out(capsys)
    assert body["contract"]["state"] == "behind"
    assert "sync and rebuild SchoolsOut" in body["contract"]["message"]


def test_status_never_claims_current_when_it_could_not_check(monkeypatch, capsys):
    """`plugin_version` is a hardcoded literal that has never been bumped, so it can and does
    say "0.0.1" on a copy missing half the verbs. An unknown contract must stay unknown."""
    _stub_rc(monkeypatch, {"GetPluginVersion": "0.0.1"})
    _pin_contract(monkeypatch, None)
    assert cli.main(["status"]) == 0
    body = _out(capsys)
    assert body["plugin_version"] == "0.0.1"
    assert body["contract"]["state"] == "unknown"


# --- `uap rc`: the silent downgrade (ClickUp 86ak7kcm7) ------------------------------------

def test_numeric_looking_value_for_an_fstring_param_stays_a_string(monkeypatch, capsys):
    """The exact repro. `SlateUser=9` used to be coerced to the JSON number 9; RemoteControl
    cannot bind a number to an FString parameter, so it left the field at its zero-initialised
    default, the plugin read "" as "resolve the user yourself", and the call answered ok having
    quietly done something else. Sending "9" refuses correctly -- so ONLY numeric-looking
    strings were swallowed, which is what made it invisible.
    """
    seen = _stub_rc(monkeypatch, {"InjectGamepad": True})
    _pin_contract(monkeypatch, _LIVE_CURRENT)
    assert cli.main(["rc", "InjectGamepad", "Button=FaceBottom", "bPressed=true",
                     "SlateUser=9"]) == 0
    params = seen[0][1]
    assert params["SlateUser"] == "9", "an FString parameter must go on the wire as a string"
    assert params["bPressed"] is True
    assert params["Button"] == "FaceBottom"
    # Button is an enum -- RemoteControl takes it by name or by index, so there is no declared
    # answer -- but the caller's own text went out unchanged, so nothing was retyped.
    assert "coercion" not in _out(capsys), "nothing was retyped; the types were declared"


def test_declared_numeric_params_are_still_numbers(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"HoldAxis": json.dumps({"ok": True})})
    _pin_contract(monkeypatch, _LIVE_CURRENT)
    assert cli.main(["rc", "HoldAxis", "AxisKeyName=Gamepad_LeftX", "Value=1",
                     "Seconds=2.5", "SlateUser=0"]) == 0
    p = seen[0][1]
    assert p["Value"] == 1.0 and isinstance(p["Value"], float)
    assert p["Seconds"] == 2.5
    assert p["AxisKeyName"] == "Gamepad_LeftX"
    assert p["SlateUser"] == "0", "0 is a real Slate user index, and it is a STRING"


def test_a_parameter_the_function_does_not_declare_is_refused(monkeypatch, capsys):
    """Same class, different spelling: RemoteControl drops an unknown key exactly as silently
    as a mistyped one, so a typo today "succeeds" having sent nothing."""
    _stub_rc(monkeypatch, {})
    _pin_contract(monkeypatch, _LIVE_CURRENT)
    assert cli.main(["rc", "InjectGamepad", "SlateUsr=0"]) == 2
    err = _out(capsys)["error"]
    assert "has no parameter(s) SlateUsr" in err
    assert "Declared parameters" in err and "SlateUser: FString" in err


def test_without_a_contract_the_guess_is_reported_not_hidden(monkeypatch, capsys):
    """No editor contract to read -> the old heuristic runs, and the caller is TOLD, with the
    escape hatch that carries real JSON types."""
    _stub_rc(monkeypatch, {"InjectGamepad": True})
    _pin_contract(monkeypatch, None)
    assert cli.main(["rc", "InjectGamepad", "SlateUser=9"]) == 0
    body = _out(capsys)
    assert body["coercion"]["guessed"] == ["SlateUser"]
    assert "as one JSON object" in body["coercion"]["note"]


def test_json_object_form_is_passed_through_untouched(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"InjectGamepad": True})
    _pin_contract(monkeypatch, None)
    assert cli.main(["rc", "InjectGamepad", '{"SlateUser": "9"}']) == 0
    assert seen[0][1] == {"SlateUser": "9"}
    assert "coercion" not in _out(capsys)


def test_enum_and_unknown_types_fall_back_to_the_heuristic(monkeypatch, capsys):
    """RemoteControl takes an enumerator by name OR by index, so there is no single right
    encoding -- the caller's own text is the best available guess, and it is declared as one."""
    assert cli._coerce_declared("FaceBottom", "EAgentGamepadButton") == ("FaceBottom", False)
    assert cli._coerce_declared("3", "EAgentGamepadButton") == (3, True)
    assert cli._coerce_declared("9", "FString") == ("9", False)
    assert cli._coerce_declared("true", "bool") == (True, False)
    assert cli._coerce_declared("false", "bool") == (False, False)
    assert cli._coerce_declared("7", "int32") == (7, False)


# --- `input axis --user` on a copy that cannot receive it ----------------------------------

def test_user_flag_is_refused_when_the_plugin_has_no_slateuser_param(monkeypatch, capsys):
    """No 404 exists for this: HoldAxis answers, RemoteControl drops the argument, and the hold
    goes out the VIEWPORT route reporting ok -- #26's silent discard, one layer up. Refuse."""
    _stub_rc(monkeypatch, {"HoldAxis": lambda p: pytest.fail(
        "must not send a parameter this plugin copy cannot receive")})
    _pin_contract(monkeypatch, _LIVE_BEHIND)
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0", "--user", "0",
                     "--project", "PBW"]) == 1
    body = _out(capsys)
    assert body["pressed"] is False, "a refused call must have zero side effects"
    assert "has no `SlateUser` parameter on `HoldAxis`" in body["error"]
    assert "report success" in body["error"]          # says WHY sending it would be worse
    assert "sync and rebuild PBW" in body["error"]    # the remedy


def test_user_flag_is_sent_when_the_plugin_has_the_param(monkeypatch, capsys):
    seen = _stub_rc(monkeypatch, {"HoldAxis": json.dumps(
        {"ok": True, "route": "slate", "user_index": 0})})
    _pin_contract(monkeypatch, _LIVE_CURRENT)
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0", "--user", "0"]) == 0
    assert seen[0][1]["SlateUser"] == "0"


def test_an_unreadable_contract_never_blocks_the_call(monkeypatch, capsys):
    """Degrade, do not gate: if we cannot tell, behave exactly as before the check existed."""
    seen = _stub_rc(monkeypatch, {"HoldAxis": json.dumps({"ok": True, "route": "slate"})})
    _pin_contract(monkeypatch, None)
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0", "--user", "1"]) == 0
    assert seen[0][1]["SlateUser"] == "1"


def test_axis_without_user_never_consults_the_contract(monkeypatch, capsys):
    """The default path must not pay an HTTP round-trip it does not need."""
    _stub_rc(monkeypatch, {"HoldAxis": json.dumps({"ok": True, "route": "viewport"})})
    monkeypatch.setattr(cli, "_live_contract",
                        lambda project=None: pytest.fail("no contract lookup without --user"))
    assert cli.main(["input", "axis", "Gamepad_LeftX", "1.0"]) == 0
