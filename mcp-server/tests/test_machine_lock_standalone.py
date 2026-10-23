"""Two projects running games at once, still, after PR #17 (2026-10-22 ~23:05 UTC).

Live state when it was found:

  Project Broken Wings   editor in PIE ("ProjectBrokenWings Preview"), project lease `pie` held
                         by `bulletsplash`, machine lock held by `bulletsplash` (`pie:start`),
                         acquired 22:58:xx.
  School's Out           project lease held by `crew-hold` with reason **`2p`** since 22:47:xx --
                         ELEVEN MINUTES BEFORE PBW took the machine -- and two standalone
                         `UnrealEditor.exe SchoolsOut.uproject -game` clients up (the
                         launch_2p_standalone.ps1 windows, "School's Out (64-bit Development
                         PCD3D_SM5)").

So School's Out was mid two-client test, and the machine lock knew nothing about it:

  1. `lease acquire exclusive --reason 2p` took ONLY the project lease. The machine lock was taken
     for a fixed list of reasons (pie / level / input / screenshot / click / nav), and `2p` was not
     on it. Nothing told PBW the workstation was in use, so PBW's `pie start` was granted.
  2. The standalone clients themselves held nothing. They are started by a project script
     (`launch_2p_standalone.ps1`, `Start-Process UnrealEditor.exe ... -game`) or by the MCP
     server's `game_launch`, never through a guarded uap verb, and while they ran the machine lock
     read "free" to the other project.
  3. Other ways to put a game up without the lock: `uap rc StartPIE|StartPIEMode`, `uap exec`
     Python that requests a play session, MCP `pie_start`, and input injection through `rc`.
"""

import asyncio
import json
from typing import ClassVar

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player import cli

_STARTED = json.dumps({"ok": True, "mode": "flat"})
_STOPPED = json.dumps({"ok": True, "was_playing": True, "cancelled_queued_start": False,
                       "in_progress": True})


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _stub_rc(monkeypatch, table):
    seen = []

    def fake(func, params, project=None):
        seen.append(func)
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val(project) if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _pie_start(monkeypatch, project, agent):
    seen = _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    return cli.main(["pie", "start", "--project", project, "--agent", agent]), seen


def _clients(monkeypatch, table: dict | None):
    """Install the game-client probe: {project_key: [pids]}, or None for "cannot tell"."""
    state = {"table": table}
    monkeypatch.setattr(co, "_game_client_probe", lambda: state["table"])
    return state


def _alive(monkeypatch, pids: set):
    real = co._pid_alive
    monkeypatch.setattr(co, "_pid_alive", lambda pid: int(pid) in pids if pid else real(pid))


# --- gap 1: a custom exclusive reason took only the project lease -----------------------------

def test_an_exclusive_lease_for_a_custom_reason_takes_the_machine(monkeypatch, capsys):
    """`crew-hold` held `--reason 2p` for a two-client test and the machine read free."""
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "2p", "--agent", "crew-hold",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    holder = co.machine_status()["holder"]
    assert holder is not None and holder["project"] == "schoolsout"
    assert holder["agent"] == "crew-hold" and holder["reason"] == "2p"


@pytest.mark.parametrize("reason", ["", "2p", "profile", "soak:utah", "Verify"])
def test_every_exclusive_reason_but_rebuild_takes_the_machine(monkeypatch, capsys, reason):
    assert cli.main(["lease", "acquire", "exclusive", "--reason", reason, "--agent", "A",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    assert co.machine_status()["holder"]["project"] == "schoolsout"


@pytest.mark.parametrize("reason", ["rebuild", "rebuild:full", "relaunch"])
def test_rebuild_and_relaunch_stay_exempt(monkeypatch, capsys, reason):
    assert cli.main(["lease", "acquire", "exclusive", "--reason", reason, "--agent", "R",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    assert co.machine_status()["holder"] is None


def test_a_shared_lease_never_takes_the_machine(monkeypatch, capsys):
    assert cli.main(["lease", "acquire", "shared", "--reason", "read", "--agent", "R",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    assert co.machine_status()["holder"] is None


def test_the_incident_a_2p_hold_blocks_the_other_projects_pie_start(monkeypatch, capsys):
    """The order it happened in: School's Out's 2p hold first, PBW's `pie start` eleven minutes
    later. PBW must be refused and must not have asked its editor to start PIE."""
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "2p", "--agent", "crew-hold",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    capsys.readouterr()
    rc, seen = _pie_start(monkeypatch, "ProjectBrokenWings", "bulletsplash")
    assert rc == 1
    body = _out(capsys)
    assert body["busy"] is True and body["blocked_by"] == "crew-hold"
    assert "StartPIEMode" not in seen


def test_a_denied_custom_reason_gives_the_project_lease_back(monkeypatch, capsys):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "2p", "--agent", "crew-hold",
                     "--project", "SchoolsOut", "--wait", "0"]) == 1
    body = _out(capsys)
    assert body["busy"] is True and body["blocked_by"] == "bulletsplash"
    assert co.status("SchoolsOut")["exclusive"] is None


def test_lease_heartbeat_keeps_the_agents_machine_hold(monkeypatch, capsys):
    """A 2p session is driven mostly through `screenshot --window` and `exec --instance`, which
    never touch the machine lock, so `lease heartbeat` must keep the machine hold alive too."""
    t = {"now": 1000.0}
    monkeypatch.setattr(co, "_now", lambda: t["now"])
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "2p", "--agent", "crew-hold",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    t["now"] += co.MACHINE_TTL - 10
    assert cli.main(["lease", "heartbeat", "--agent", "crew-hold", "--project", "SchoolsOut"]) == 0
    t["now"] += co.MACHINE_TTL - 10
    assert co.machine_status()["holder"]["agent"] == "crew-hold"


# --- gap 2: standalone / packaged game clients held nothing -----------------------------------

def test_running_standalone_clients_hold_the_machine_for_their_project(monkeypatch, capsys):
    """The two School's Out `-game` windows (pids 21716, 59372) were up with no machine record.
    The machine lock must see them, and PBW's `pie start` must be refused without starting PIE."""
    _clients(monkeypatch, {"schoolsout": [21716, 59372]})
    _alive(monkeypatch, {21716, 59372})
    rc, seen = _pie_start(monkeypatch, "ProjectBrokenWings", "bulletsplash")
    assert rc == 1
    body = _out(capsys)
    assert body["busy"] is True and body["project"] == "schoolsout"
    assert body["blocked_by"] == co.GAME_CLIENTS_AGENT
    assert "StartPIEMode" not in seen
    assert "21716" in body["hint"] and "59372" in body["hint"]
    holder = co.machine_status()["holder"]
    assert holder["project"] == "schoolsout" and sorted(holder["pids"]) == [21716, 59372]


def test_the_client_hold_frees_itself_when_the_clients_exit(monkeypatch, capsys):
    """No stale lock after a crash or a close: once every client is gone the hold is gone."""
    probe = _clients(monkeypatch, {"schoolsout": [21716, 59372]})
    alive = {21716, 59372}
    _alive(monkeypatch, alive)
    assert co.machine_status()["holder"]["project"] == "schoolsout"
    alive.discard(21716)
    probe["table"] = {"schoolsout": [59372]}
    assert co.machine_status()["holder"]["pids"] == [59372]
    alive.clear()
    probe["table"] = {}
    assert co.machine_status()["holder"] is None
    rc, seen = _pie_start(monkeypatch, "ProjectBrokenWings", "bulletsplash")
    assert rc == 0 and "StartPIEMode" in seen


def test_a_client_hold_frees_on_pid_death_even_when_the_scan_cannot_answer(monkeypatch, capsys):
    probe = _clients(monkeypatch, {"schoolsout": [21716]})
    alive = {21716}
    _alive(monkeypatch, alive)
    assert co.machine_status()["holder"]["project"] == "schoolsout"
    probe["table"] = None                 # process list unreadable this time
    alive.clear()
    assert co.machine_status()["holder"] is None


def test_the_clients_own_project_passes_through(monkeypatch, capsys):
    """Same-project calls pass through, as for every other machine hold: the SOVR agent running
    the two-client test must still be able to drive its own editor."""
    _clients(monkeypatch, {"schoolsout": [21716]})
    _alive(monkeypatch, {21716})
    monkeypatch.setattr(cli, "_screenshot", lambda args: 0)
    assert cli.main(["screenshot", "a.png", "--project", "SchoolsOut", "--agent", "crew-hold"]) == 0
    assert co.machine_status()["holder"]["agent"] == co.GAME_CLIENTS_AGENT


def test_a_sticky_hold_in_the_clients_project_takes_ownership(monkeypatch, capsys):
    """`lease acquire --reason 2p` (or `pie start`) after the clients are already up: the agent
    now owns the hold, so it does not vanish with the clients while its own session runs."""
    probe = _clients(monkeypatch, {"schoolsout": [21716]})
    alive = {21716}
    _alive(monkeypatch, alive)
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "2p", "--agent", "crew-hold",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    holder = co.machine_status()["holder"]
    assert holder["agent"] == "crew-hold" and holder["clients"] == [21716]
    alive.clear()
    probe["table"] = {}
    assert co.machine_status()["holder"]["agent"] == "crew-hold"


def test_live_clients_keep_an_agents_hold_past_its_ttl(monkeypatch, capsys):
    t = {"now": 1000.0}
    monkeypatch.setattr(co, "_now", lambda: t["now"])
    probe = _clients(monkeypatch, {})
    _alive(monkeypatch, {21716})
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "2p", "--agent", "crew-hold",
                     "--project", "SchoolsOut", "--wait", "0"]) == 0
    probe["table"] = {"schoolsout": [21716]}
    for _ in range(3):
        t["now"] += co.MACHINE_TTL - 1
        assert co.machine_status()["holder"]["agent"] == "crew-hold"
    probe["table"] = {}
    t["now"] += co.MACHINE_TTL + 1
    assert co.machine_status()["holder"] is None


def test_clients_started_under_the_other_projects_hold_are_reported(monkeypatch, capsys):
    """Launched outside uap while PBW held the machine: uap cannot stop the launch, but it must
    not hide it, and the moment PBW lets go the clients' project holds the machine."""
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    _clients(monkeypatch, {"schoolsout": [21716, 59372]})
    _alive(monkeypatch, {21716, 59372})
    st = co.machine_status()
    assert st["holder"]["project"] == "projectbrokenwings"
    assert st["conflicts"] == {"schoolsout": [21716, 59372]}
    co.release_machine(agent="bulletsplash", project="ProjectBrokenWings")
    assert co.machine_status()["holder"]["project"] == "schoolsout"


def test_an_unreadable_process_list_changes_nothing(monkeypatch, capsys):
    _clients(monkeypatch, None)
    rc, _seen = _pie_start(monkeypatch, "ProjectBrokenWings", "bulletsplash")
    assert rc == 0


def test_the_probe_finds_editor_game_clients_and_packaged_builds(monkeypatch, tmp_path):
    """What counts as a game client: `UnrealEditor*.exe <proj>.uproject -game` (the
    launch_2p_standalone.ps1 windows) and a packaged `<Project>[-Win64-<Config>].exe` under a
    Binaries folder. Not the editor itself, and not a windowless `-server` / `-nullrhi` process."""
    co.acquire("SchoolsOut", "shared", reason="read", agent="x")   # makes `schoolsout` known
    procs = {
        62052: ("UnrealEditor.exe", r"E:\Eng\UnrealEditor.exe",
                r'"E:\Eng\UnrealEditor.exe" "E:\S\SchoolsOut.uproject"'),
        21716: ("UnrealEditor.exe", r"E:\Eng\UnrealEditor.exe",
                r'"E:\Eng\UnrealEditor.exe" "E:\S\SchoolsOut.uproject" -game -windowed'),
        59372: ("UnrealEditor.exe", r"E:\Eng\UnrealEditor.exe",
                r'"E:\Eng\UnrealEditor.exe" "E:\S\SchoolsOut.uproject" -game -nohmd'),
        700: ("UnrealEditor.exe", r"E:\Eng\UnrealEditor.exe",
              r'"E:\Eng\UnrealEditor.exe" "E:\S\SchoolsOut.uproject" -game -server'),
        701: ("UnrealEditor-Cmd.exe", r"E:\Eng\UnrealEditor-Cmd.exe",
              r'"E:\Eng\UnrealEditor-Cmd.exe" "E:\P\ProjectBrokenWings.uproject" -game -nullrhi'),
        800: ("SchoolsOut-Win64-Shipping.exe",
              r"D:\Pkg\SchoolsOut\Binaries\Win64\SchoolsOut-Win64-Shipping.exe",
              r'"D:\Pkg\SchoolsOut\Binaries\Win64\SchoolsOut-Win64-Shipping.exe"'),
        801: ("SchoolsOut.exe", r"D:\Pkg\SchoolsOut.exe", r'"D:\Pkg\SchoolsOut.exe"'),
        900: ("notepad.exe", r"C:\Windows\notepad.exe", "notepad"),
    }
    monkeypatch.setattr(co, "_enumerate_processes",
                        lambda: [(pid, v[0]) for pid, v in procs.items()])
    from unreal_agent_player import window_capture as wc
    monkeypatch.setattr(wc, "_process_info", lambda pid: (procs[pid][1], procs[pid][2]))
    monkeypatch.delenv("UAP_GAME_CLIENT_PROBE", raising=False)
    co._GAME_CLIENT_CACHE.clear()
    assert co.game_clients() == {"schoolsout": [800, 21716, 59372]}


# --- gap 3: other ways to put a game up without the lock --------------------------------------

def test_rc_startpie_takes_the_machine_like_pie_start(monkeypatch, capsys):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    for func in ("StartPIE", "StartPIEMode"):
        seen = _stub_rc(monkeypatch, {func: True})
        assert cli.main(["rc", func, "--project", "SchoolsOut", "--agent", "sprayhub"]) == 1
        assert _out(capsys)["blocked_by"] == "bulletsplash"
        assert func not in seen


def test_rc_startpie_holds_the_machine_stickily(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsPIEInProgress": True})
    assert cli.main(["rc", "StartPIEMode", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    holder = co.machine_status()["holder"]
    assert holder["project"] == "schoolsout" and holder["pid"] == 0


def test_rc_stoppie_is_never_blocked_and_frees_a_confirmed_stop(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsPIEInProgress": True})
    assert cli.main(["rc", "StartPIEMode", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    _stub_rc(monkeypatch, {"StopPIEEx": _STOPPED, "IsPIEInProgress": False})
    assert cli.main(["rc", "StopPIEEx", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    assert co.machine_status()["holder"] is None


def test_rc_stoppie_that_did_not_stop_keeps_the_hold(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_RAW_STOP_CONFIRM_SECONDS", 0.0)
    _stub_rc(monkeypatch, {"StartPIEMode": _STARTED, "IsPIEInProgress": True})
    assert cli.main(["rc", "StartPIEMode", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    _stub_rc(monkeypatch, {"StopPIEEx": _STOPPED, "IsPIEInProgress": True})
    assert cli.main(["rc", "StopPIEEx", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    assert co.machine_status()["holder"]["project"] == "schoolsout"


@pytest.mark.parametrize("func", ["InjectKey", "HoldKey", "HoldAxis", "ClickMouse",
                                  "InjectGamepad", "InjectMouseMove", "SetMousePosition",
                                  "InjectXRButton", "NavigateUI", "SelectTab"])
def test_rc_input_injection_waits_for_the_machine(monkeypatch, capsys, func):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    seen = _stub_rc(monkeypatch, {func: True})
    assert cli.main(["rc", func, "--project", "SchoolsOut", "--agent", "sprayhub"]) == 1
    assert func not in seen


def test_rc_read_only_functions_are_not_gated(monkeypatch, capsys):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    _stub_rc(monkeypatch, {"GetPIEPhase": "NotPlaying"})
    assert cli.main(["rc", "GetPIEPhase", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0


class _FakeExec:
    calls: ClassVar[list] = []

    def __init__(self, *a, **k):
        self.last_target = None

    def exec_python(self, code):
        _FakeExec.calls.append(code)
        return {"result": "None", "output": [], "success": True}


@pytest.mark.parametrize("code", [
    ("import unreal\nunreal.get_editor_subsystem(unreal.LevelEditorSubsystem)"
     ".editor_request_begin_play()"),
    "unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).editor_play_simulate()",
    "unreal.get_editor_subsystem(unreal.UAPAgentSubsystem).start_pie_mode('flat')",
])
def test_exec_that_starts_pie_takes_the_machine(monkeypatch, capsys, code):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    _FakeExec.calls = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _FakeExec)
    assert cli.main(["exec", code, "--project", "SchoolsOut", "--agent", "sprayhub"]) == 1
    assert _out(capsys)["blocked_by"] == "bulletsplash"
    assert _FakeExec.calls == []


def test_exec_file_that_starts_pie_takes_the_machine(monkeypatch, capsys, tmp_path):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    f = tmp_path / "go.py"
    f.write_text("import unreal\nunreal.get_editor_subsystem(unreal.LevelEditorSubsystem)"
                 ".editor_request_begin_play()\n", encoding="utf-8")
    _FakeExec.calls = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _FakeExec)
    assert cli.main(["exec-file", str(f), "--project", "SchoolsOut", "--agent", "sprayhub"]) == 1
    assert _FakeExec.calls == []


def test_a_raw_start_that_left_no_session_gives_the_hold_back(monkeypatch, capsys):
    """An exec that only MENTIONS a start API (or a start that failed) must not block the other
    project until the TTL: no session in progress, so the sticky hold it took goes straight back."""
    _FakeExec.calls = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _FakeExec)
    _stub_rc(monkeypatch, {"IsPIEInProgress": False})
    code = "print(hasattr(unreal.LevelEditorSubsystem, 'editor_request_begin_play'))"
    assert cli.main(["exec", code, "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    assert co.machine_status()["holder"] is None


def test_a_raw_start_that_is_still_coming_up_keeps_the_hold(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"StartPIE": True, "IsPIEInProgress": True})
    assert cli.main(["rc", "StartPIE", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    assert co.machine_status()["holder"]["agent"] == "sprayhub"


def test_ordinary_exec_is_still_ungated(monkeypatch, capsys):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    _FakeExec.calls = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _FakeExec)
    assert cli.main(["exec", "print(1)", "--project", "SchoolsOut", "--agent", "sprayhub"]) == 0
    assert _FakeExec.calls == ["print(1)"]


def test_batch_steps_inherit_the_new_gates(monkeypatch, capsys):
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    seen = _stub_rc(monkeypatch, {"StartPIE": True})
    cli.main(["batch", "rc StartPIE", "--project", "SchoolsOut", "--agent", "sprayhub"])
    assert "StartPIE" not in seen


# --- MCP server: game_launch and pie_start -----------------------------------------------------

class _FakeProc:
    def __init__(self, pid=4321):
        self.pid = pid
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False


def test_mcp_game_launch_is_refused_while_the_other_project_holds_the_machine(monkeypatch):
    from unreal_agent_player.instances import InstanceRegistry
    from unreal_agent_player.tools import game as game_mod

    monkeypatch.setenv("UAP_PROJECT", "SchoolsOut")
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)
    spawned = []
    monkeypatch.setattr(game_mod, "_spawn",
                        lambda *a, **k: spawned.append(1) or _FakeProc())
    r = asyncio.run(game_mod.game_launch(registry=InstanceRegistry(), map=None))
    assert r["ok"] is False and "bulletsplash" in r["error"]["message"]
    assert spawned == []


def test_mcp_game_launch_holds_the_machine_while_its_client_lives(monkeypatch):
    from unreal_agent_player.instances import InstanceRegistry
    from unreal_agent_player.tools import game as game_mod

    monkeypatch.setenv("UAP_PROJECT", "SchoolsOut")
    proc = _FakeProc(4321)
    alive = {4321}
    _alive(monkeypatch, alive)
    monkeypatch.setattr(game_mod, "_spawn", lambda *a, **k: proc)

    async def _ready(port):
        return True
    monkeypatch.setattr(game_mod, "_wait_rc_ready", _ready)
    r = asyncio.run(game_mod.game_launch(registry=InstanceRegistry(), map=None))
    assert r["ok"] is True
    holder = co.machine_status()["holder"]
    assert holder["project"] == "schoolsout" and holder["pids"] == [4321]
    alive.clear()
    assert co.machine_status()["holder"] is None


def test_mcp_game_launch_that_fails_gives_the_machine_back(monkeypatch):
    from unreal_agent_player.instances import InstanceRegistry
    from unreal_agent_player.tools import game as game_mod

    monkeypatch.setenv("UAP_PROJECT", "SchoolsOut")
    monkeypatch.setattr(game_mod, "_spawn", lambda *a, **k: _FakeProc())

    async def _never(port):
        return False
    monkeypatch.setattr(game_mod, "_wait_rc_ready", _never)
    r = asyncio.run(game_mod.game_launch(registry=InstanceRegistry(), map=None))
    assert r["ok"] is False
    assert co.machine_status()["holder"] is None


def test_mcp_pie_start_is_refused_while_the_other_project_holds_the_machine(monkeypatch):
    from unreal_agent_player.tools import pie as pie_mod

    monkeypatch.setenv("UAP_PROJECT", "SchoolsOut")
    co.acquire_machine("ProjectBrokenWings", reason="pie:start", agent="bulletsplash", pid=0,
                       wait=0)

    class Py:
        calls: ClassVar[list] = []

        def exec_python(self, code, unattended=True):
            Py.calls.append(code)

    r = asyncio.run(pie_mod.pie_start(rc=None, py_exec=Py(), mode="PlayInNewWindow"))
    assert r["ok"] is False and "bulletsplash" in r["error"]["message"]
    assert Py.calls == []
