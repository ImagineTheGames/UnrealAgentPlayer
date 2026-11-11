"""Per-client input targeting [17tm466z4a6] and the owner's no-focus rule (2026-11-11).

Two things the owner asked for, pinned together because they come from one rule: agent testing
must never take his mouse, keyboard or foreground window.

1. `uap input ... --client N|all`: with several PIE clients in one process every verb used to act
   on the FIRST game world, so a net client could never be driven. The CLI adds a trailing
   `Client` argument to the client-capable verbs. An older plugin copy would DROP that argument
   silently (RemoteControl ignores unknown parameters) and drive client 0 while the caller
   believes it drove client 1 -- the silent wrong answer this exists to end -- so it is REFUSED.
2. uap never raises, focuses or sends OS input to a window. `FocusEditorWindow` is gone, no source
   file calls the Win32 window-activation / OS-input APIs, and `report finish` no longer opens a
   browser window (a new window takes the foreground) unless asked.
"""

import json
import os
import re
from pathlib import Path

import pytest

from unreal_agent_player import cli
from unreal_agent_player.reporting import viewer
from unreal_agent_player.tools import game as game_mod

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.setattr(cli, "_CLIENT", [None])


# --- the Client argument ----------------------------------------------------------------

def test_no_client_flag_sends_no_client_argument(monkeypatch):
    """Omitted must mean omitted: an older plugin and every existing caller are unchanged."""
    assert cli._with_client("HoldKey", {"KeyName": "W", "Seconds": 1.0}, None) == \
        {"KeyName": "W", "Seconds": 1.0}


def test_client_flag_adds_client_to_every_client_capable_verb(monkeypatch):
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)   # unreadable -> send
    cli._CLIENT[0] = "1"
    for verb in cli._CLIENT_VERBS:
        assert cli._with_client(verb, {"X": 1}, None)["Client"] == "1", verb


def test_client_flag_never_touches_a_verb_that_is_not_per_client(monkeypatch):
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    cli._CLIENT[0] = "1"
    assert cli._with_client("GetPIEPhase", {}, None) == {}
    assert cli._with_client("StartPIEMode", {"Mode": "flat"}, None) == {"Mode": "flat"}


def test_client_all_is_passed_through(monkeypatch):
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    cli._CLIENT[0] = "all"
    assert cli._with_client("InjectKey", {"KeyName": "W"}, None)["Client"] == "all"


def test_a_plugin_without_the_parameter_is_refused_not_silently_driven_on_client_0(monkeypatch):
    """RemoteControl takes the call and drops the unknown argument. Without this check the input
    would go to the FIRST client while the caller believes it went to client 1."""
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: {
        "HoldKey": {"KeyName": "FString", "Seconds": "float"}})      # no Client
    cli._CLIENT[0] = "1"
    with pytest.raises(cli.AgentError) as exc:
        cli._with_client("HoldKey", {"KeyName": "W", "Seconds": 1.0}, "SchoolsOut")
    msg = str(exc.value)
    assert "HoldKey" in msg and "FIRST client" in msg and "rebuild" in msg


def test_a_plugin_with_the_parameter_is_sent_it(monkeypatch):
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: {
        "HoldKey": {"KeyName": "FString", "Seconds": "float", "Client": "FString"}})
    cli._CLIENT[0] = "1"
    assert cli._with_client("HoldKey", {"KeyName": "W"}, "SchoolsOut")["Client"] == "1"


def test_the_client_rides_every_rc_call_made_by_the_verbs(monkeypatch, capsys):
    """End to end through _rc_call, which is where it is added (the verbs never thread it)."""
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    sent = []

    class FakeRC:
        def __init__(self, *a, **k):
            pass

        async def call_preset(self, func, params, *a, **k):
            sent.append((func, dict(params)))
            if func == "GetHeldInput":
                return json.dumps({"ok": True, "held": []})
            return json.dumps({"ok": True, "key": params.get("KeyName"), "seconds": 1.0,
                               "pressed": True, "route": "viewport", "client": 1})

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "RemoteControlClient", FakeRC)
    assert cli.main(["input", "hold", "W", "--seconds", "1", "--client", "1"]) == 0
    holds = [p for f, p in sent if f == "HoldKey"]
    assert holds and holds[0]["Client"] == "1"
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True and out["client"] == 1


def test_client_env_var_is_the_default(monkeypatch):
    monkeypatch.setenv("UAP_CLIENT", "1")
    args = cli.build_parser().parse_args(["input", "status"])
    assert args.client == "1"


def test_click_refuses_client_all(monkeypatch, capsys):
    cli._CLIENT[0] = "all"
    assert cli._click(type("A", (), {"label": "OK", "project": None})()) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["clicked"] is False and "one client" in out["error"]


def test_input_clients_lists_the_targets(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    seen = []

    def fake(func, params, project=None):
        seen.append(func)
        return json.dumps({"ok": True, "count": 2, "clients": [
            {"client": 0, "net_mode": "ListenServer", "default": True},
            {"client": 1, "net_mode": "Client", "default": False}]})

    monkeypatch.setattr(cli, "_rc_call", fake)
    assert cli.main(["input", "clients"]) == 0
    assert seen == ["ListClients"]
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["count"] == 2


# --- holds are per client ---------------------------------------------------------------

def test_the_same_key_on_two_clients_is_not_an_overlap(monkeypatch, capsys):
    """W on client 0 and W on client 1 are two different holds, and a different key on a
    DIFFERENT client is not an overlap either. Only a different key on the SAME client is."""
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    cli._CLIENT[0] = "0"
    cli._hold_ledger_note("p", "W", 30, "a")
    cli._CLIENT[0] = "1"
    assert cli._hold_conflict("A", "p", allow=False) is None      # client 1 has nothing held


def test_a_different_key_on_the_same_client_still_overlaps(monkeypatch):
    held = [{"key": "W", "remaining_seconds": 20.0, "route": "viewport", "client": 1}]
    monkeypatch.setattr(cli, "_rc_json",
                        lambda func, params, project=None, needs=None:
                        {"ok": True, "held": held} if func == "GetHeldInput" else {})
    cli._CLIENT[0] = "1"
    cli._hold_ledger_note("p", "W", 30, "a")
    clash = cli._hold_conflict("A", "p", allow=False)
    assert clash is not None and clash["overlap_refused"] is True


# --- the plugin source carries the feature ---------------------------------------------

def _plugin(rel):
    return (REPO / "Plugin/Source" / rel).read_text(encoding="utf-8")


def test_every_client_verb_has_a_trailing_client_param_in_both_subsystems():
    for header in ("UnrealAgentPlayer/Public/UAPAgentSubsystem.h",
                   "UnrealAgentPlayerRuntime/Public/UAPAgentRuntimeSubsystem.h"):
        text = _plugin(header)
        for verb in cli._CLIENT_VERBS:
            m = re.search(r"\b(?:bool|FString) " + verb + r"\(([^)]*)\);", text)
            assert m, f"{verb} not found in {header}"
            assert m.group(1).rstrip().endswith("FString Client"), \
                f"{verb} in {header} must end with `FString Client`: ({m.group(1)})"
        assert "FString ListClients();" in text


def test_a_hold_remembers_its_world_and_reenters_it_every_tick():
    cpp = _plugin("UnrealAgentPlayerRuntime/Private/AgentInput.cpp")
    assert "TWeakObjectPtr<UWorld> World;" in cpp
    # the ticker makes the hold's own world current before injecting, and so does the release
    assert cpp.count("FAgentWorld::FScope Scope(") >= 3


# --- no window activation, no OS input --------------------------------------------------

# A CALL (not a mention in a comment or a help string) of any window-activation or OS-input API.
_FORBIDDEN_CPP = re.compile(
    r"::(SetForegroundWindow|BringWindowToTop|AttachThreadInput|AllowSetForegroundWindow|"
    r"SwitchToThisWindow|SetActiveWindow|SetFocus|ShowWindow|SendInput|keybd_event|mouse_event|"
    r"SetCursorPos|LockSetForegroundWindow)\s*\(|"
    r"\bSetWindowPos\s*\(\s*\w+\s*,\s*HWND_(TOPMOST|NOTOPMOST)|"
    # Slate / engine wrappers that end in the OS cursor: they drag the human's real mouse pointer.
    r"\bApp\.SetCursorPos\s*\(|\bSlateApp\w*(\.|->)SetCursorPos\s*\(|\bSetMouseLocation\s*\(|"
    r"\bSPI_SETFOREGROUNDLOCKTIMEOUT\s*,\s*0\s*,\s*\(PVOID\)\s*0\b")
_FORBIDDEN_PY = re.compile(
    r"\.(SetForegroundWindow|BringWindowToTop|AttachThreadInput|AllowSetForegroundWindow|"
    r"SwitchToThisWindow|SetActiveWindow|SetFocus|ShowWindow|SendInput|keybd_event|mouse_event|"
    r"SetCursorPos|LockSetForegroundWindow)\s*\(|\bpyautogui\b|\bSendKeys\b|\bAppActivate\b")


def _strip_cpp_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def test_the_plugin_never_activates_a_window_or_sends_os_input():
    offenders = []
    for path in (REPO / "Plugin/Source").rglob("*"):
        if path.suffix not in (".cpp", ".h"):
            continue
        for m in _FORBIDDEN_CPP.finditer(_strip_cpp_comments(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(REPO)}: {m.group(0)}")
    assert not offenders, "window activation / OS input is banned in uap:\n" + "\n".join(offenders)


def test_the_cli_never_activates_a_window_or_sends_os_input():
    offenders = []
    for path in (REPO / "mcp-server/src").rglob("*.py"):
        code = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                         if not l.lstrip().startswith("#"))
        for m in _FORBIDDEN_PY.finditer(code):
            offenders.append(f"{path.relative_to(REPO)}: {m.group(0)}")
    assert not offenders, "window activation / OS input is banned in uap:\n" + "\n".join(offenders)


def test_focus_editor_window_is_gone_from_the_plugin_and_the_cli():
    assert "FocusEditorWindow" not in _plugin("UnrealAgentPlayer/Public/UAPAgentSubsystem.h")
    assert "FocusEditorWindow" not in _plugin("UnrealAgentPlayer/Private/UAPAgentSubsystem.cpp")
    assert "FocusEditorWindow" not in set(re.findall(r"\w+", Path(cli.__file__).read_text(
        encoding="utf-8").split("_RC_FOREGROUND = {")[1].split("}")[0]))


def test_the_plugin_turns_the_background_throttle_off_for_the_pie_session_and_restores_it():
    cpp = _plugin("UnrealAgentPlayer/Private/UAPAgentSubsystem.cpp")
    assert "bThrottleCPUWhenNotForeground = false" in cpp
    assert "Settings->bThrottleCPUWhenNotForeground = bPriorThrottleSetting" in cpp
    # restored on every way a session can end
    for hook in ("OnEndPIE", "OnCancelPIE"):
        line = [l for l in cpp.splitlines() if f"::{hook}(" in l and "SuppressBackgroundThrottle" in l]
        assert line, f"{hook} must restore the throttle setting"
    assert "SuppressBackgroundThrottle(false);\n    FAgentRemoteControlBootstrap::Unexpose" in \
        cpp.replace("\r\n", "\n")
    # never persisted: an ini write would outlive the session
    assert "SaveConfig" not in cpp.split("SuppressBackgroundThrottle(bool bSuppress)")[1][:1200]


def test_the_module_never_zeroes_the_foreground_lock_timeout_and_undoes_an_old_zero():
    """The old module wrote ForegroundLockTimeout=0 persistently so FocusEditorWindow could steal
    focus. That also lets ANY background process (this editor opening a PIE window) jump in front
    of the human, so a leftover zero is put back to the Windows default (200000 ms), never set."""
    cpp = _strip_cpp_comments(_plugin("UnrealAgentPlayer/Private/UnrealAgentPlayerModule.cpp"))
    assert not _FORBIDDEN_CPP.search(cpp)
    assert "SPI_GETFOREGROUNDLOCKTIMEOUT" in cpp and "200000" in cpp
    assert "Timeout == 0" in cpp


def test_report_finish_does_not_open_a_window_by_default(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(viewer, "_spawn", lambda exe, url: spawned.append(url) or True)
    monkeypatch.setattr(viewer, "find_app_browser", lambda: "chrome.exe")
    monkeypatch.delenv("UAP_REPORT_OPEN", raising=False)
    monkeypatch.delenv("UAP_REPORT_NO_OPEN", raising=False)
    monkeypatch.delenv("UAP_NO_BROWSER", raising=False)
    res = viewer.open_report(tmp_path / "index.html")
    assert res["opened"] is False and res["mode"] == "none" and spawned == []


def test_report_finish_opens_only_when_asked(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(viewer, "_spawn", lambda exe, url: spawned.append(url) or True)
    monkeypatch.setattr(viewer, "find_app_browser", lambda: "chrome.exe")
    monkeypatch.setattr(viewer, "_await_window", lambda before, deadline: 0)
    monkeypatch.setattr(viewer, "list_windows", list)
    monkeypatch.setattr(viewer, "_read_state", dict)
    monkeypatch.setattr(viewer, "_write_state", lambda s: None)
    monkeypatch.delenv("UAP_REPORT_NO_OPEN", raising=False)
    monkeypatch.delenv("UAP_NO_BROWSER", raising=False)
    assert viewer.open_report(tmp_path / "index.html", open_=True)["opened"] is True
    assert len(spawned) == 1
    spawned.clear()
    monkeypatch.setenv("UAP_REPORT_OPEN", "1")
    assert viewer.open_report(tmp_path / "index.html")["opened"] is True
    # ...and --no-open still wins over everything
    spawned.clear()
    assert viewer.open_report(tmp_path / "index.html", no_open=True, open_=True)["opened"] is False
    assert spawned == []


@pytest.mark.skipif(os.name != "nt", reason="STARTUPINFO is Windows only")
def test_a_launched_game_window_is_not_activated():
    kw = game_mod._no_activate_kwargs()
    assert kw["startupinfo"].wShowWindow == 7          # SW_SHOWMINNOACTIVE
    assert kw["startupinfo"].dwFlags & 0x1             # STARTF_USESHOWWINDOW


# --- a standalone -game client (a second process) is reached in-engine, never through the OS ---

_EDITOR = {"pid": 10, "role": "editor", "project": "E:/p/SchoolsOut.uproject",
           "cmdline": "UnrealEditor.exe SchoolsOut.uproject"}
_CLIENT2 = {"pid": 22, "role": "game", "project": "E:/p/SchoolsOut.uproject",
            "cmdline": "UnrealEditor.exe SchoolsOut.uproject -game -RCWebControlEnable "
                       "-UAPRCPort=30102 -DevAuthToolName=Context_2"}
_CLIENT_NO_PORT = {"pid": 33, "role": "game", "project": "E:/p/SchoolsOut.uproject",
                   "cmdline": "UnrealEditor.exe SchoolsOut.uproject -game -DevAuthToolName=Context_3"}


def _nodes(monkeypatch, *nodes):
    from unreal_agent_player.transport import PythonRemoteExecClient as Real

    class FakeProbe(Real):          # real selector logic, canned discovery
        def list_nodes(self):
            return list(nodes)

    monkeypatch.setattr(cli, "PythonRemoteExecClient", FakeProbe)


def test_instance_selector_resolves_to_the_clients_runtime_preset_and_port(monkeypatch):
    _nodes(monkeypatch, _EDITOR, _CLIENT2)
    t = cli._resolve_instance_target("Context_2", "SchoolsOut")
    assert t == {"port": 30102, "preset": "UAP_RuntimePreset", "pid": 22}


def test_no_instance_or_editor_means_the_editor(monkeypatch):
    assert cli._resolve_instance_target(None, "SchoolsOut") is None
    assert cli._resolve_instance_target("editor", "SchoolsOut") is None


def test_a_client_started_without_the_rc_port_is_refused_and_never_driven_through_the_os(monkeypatch):
    _nodes(monkeypatch, _EDITOR, _CLIENT_NO_PORT)
    with pytest.raises(cli.AgentError) as exc:
        cli._resolve_instance_target("Context_3", "SchoolsOut")
    msg = str(exc.value)
    assert "-UAPRCPort" in msg and "banned" in msg and "launch_2p_standalone" in msg


def test_an_unknown_or_ambiguous_instance_is_refused(monkeypatch):
    _nodes(monkeypatch, _EDITOR, _CLIENT2)
    with pytest.raises(cli.AgentError):
        cli._resolve_instance_target("Context_9", "SchoolsOut")
    _nodes(monkeypatch, _EDITOR, _CLIENT2, dict(_CLIENT2, pid=23))
    with pytest.raises(cli.AgentError):
        cli._resolve_instance_target("Context_2", "SchoolsOut")


def test_rc_calls_go_to_the_standalone_port_and_runtime_preset(monkeypatch):
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
    seen = []

    class FakeRC:
        def __init__(self, port=None, **k):
            seen.append(("port", port))

        async def call_preset(self, func, params, *a, preset=None, **k):
            seen.append((func, preset, dict(params)))
            return json.dumps({"ok": True})

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "RemoteControlClient", FakeRC)
    cli._CLIENT[0] = None
    cli._RC_TARGET[0] = {"port": 30102, "preset": "UAP_RuntimePreset", "pid": 22}
    try:
        cli._rc_call("HoldKey", {"KeyName": "W", "Seconds": 1.0}, "SchoolsOut")
    finally:
        cli._RC_TARGET[0] = None
    assert ("port", 30102) in seen
    assert ("HoldKey", "UAP_RuntimePreset", {"KeyName": "W", "Seconds": 1.0}) in seen


def test_the_launcher_starts_each_client_with_its_uap_rc_port():
    ps1 = (REPO.parent / "SchoolsOutVR" / "launch_2p_standalone.ps1")
    if not ps1.exists():
        pytest.skip("the SchoolsOut launcher is not beside this checkout")
    text = ps1.read_text(encoding="utf-8")
    assert "-UAPRCPort=$RcPortA" in text and "-UAPRCPort=$RcPortB" in text
