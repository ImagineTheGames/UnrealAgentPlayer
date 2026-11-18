# ruff: noqa: S102
"""Cleanup half of the exec callback guard [ClickUp 17tm4675e8t].

Covers: owner-keyed
release_all (`lease release` / `pie stop`), and the NameError warning that names the missing
global. The guard is real editor-side Python, run here against a fake `unreal`.
"""

import contextlib
import io
import json
import sys
import types

from unreal_agent_player import cli
from unreal_agent_player import exec_callbacks as ecb
from unreal_agent_player import exec_client as xc

_REAL_RELEASE = cli._release_exec_callbacks


class _World:
    def get_name(self):
        return "Map"

    def get_outer(self):
        return types.SimpleNamespace(get_name=lambda: "/Game/Maps/UEDPIE_0_Map")


class _Sub:
    pie = True

    def get_game_world(self):
        return _World() if _Sub.pie else None


class _Fake(types.ModuleType):
    def __init__(self):
        super().__init__("unreal")
        self.World = _World
        self.UnrealEditorSubsystem = _Sub
        self.get_editor_subsystem = lambda cls: cls()
        self.ObjectIterator = lambda cls: iter([_World()] if _Sub.pie else [])
        self.cbs = {}
        self.warnings = []
        self.hits = []
        self._n = 0
        self.log_warning = self.warnings.append

    def register_slate_post_tick_callback(self, fn):
        self._n += 1
        self.cbs[self._n] = fn
        return self._n

    def unregister_slate_post_tick_callback(self, handle):
        self.cbs.pop(handle, None)

    def tick(self, n=1):
        for _ in range(n):
            for fn in list(self.cbs.values()):
                fn(0.016)


def _setup(monkeypatch, pie=True):
    monkeypatch.delitem(sys.modules, "_uap_cb_guard", raising=False)
    _Sub.pie = pie
    return _Fake()


def _exec(code, monkeypatch, fake, owner=""):
    monkeypatch.setitem(sys.modules, "unreal", fake)
    text = xc.build_client_code(code, "auto", owner)
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(text, "<test>", "exec"), {})


# ---- 1. PIE-bound tick callbacks ---------------------------------------------------------

def test_pie_tick_callback_unregisters_when_pie_ended_and_next_exec_sweeps(monkeypatch):
    fake = _setup(monkeypatch)
    _exec("import unreal\ndef _t(dt):\n    unreal.hits.append(1)\n"
          "unreal.register_slate_post_tick_callback(_t)\n", monkeypatch, fake)
    fake.tick()
    assert fake.hits == [1]
    _Sub.pie = False
    _exec("pass\n", monkeypatch, fake)
    assert fake.cbs == {} and any("PIE ended" in w for w in fake.warnings)


# ---- 2. owner-keyed release -------------------------------------------------------------

_TICK = ("import unreal\ndef _t(dt):\n    pass\n"
         "unreal.register_slate_post_tick_callback(_t)\n")


def test_release_all_by_owner_only_drops_that_agents_callbacks(monkeypatch):
    fake = _setup(monkeypatch, pie=False)
    _exec(_TICK, monkeypatch, fake, owner="agentA")
    _exec(_TICK, monkeypatch, fake, owner="agentB")
    _exec(_TICK, monkeypatch, fake, owner="")
    guard = sys.modules["_uap_cb_guard"]
    assert len(fake.cbs) == 3
    assert guard.release_all("agentA") == 1
    assert len(fake.cbs) == 2
    assert {r["owner"] for r in guard.live.values()} == {"agentB", ""}
    guard.release_all("")
    assert {r["owner"] for r in guard.live.values()} == {"agentB"}
    guard.release_all()
    assert fake.cbs == {} and guard.live == {}


def test_release_code_runs_editor_side_and_reports_count(monkeypatch):
    fake = _setup(monkeypatch, pie=False)
    _exec(_TICK, monkeypatch, fake, owner="agentA")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(ecb.release_code("agentA"), "<rel>", "exec"), {})
    assert "@@UAP_CB_RELEASED@@ 1" in out.getvalue() and fake.cbs == {}


def test_release_code_without_guard_is_a_noop(monkeypatch):
    monkeypatch.delitem(sys.modules, "_uap_cb_guard", raising=False)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(ecb.release_code("x"), "<rel>", "exec"), {})
    assert "@@UAP_CB_RELEASED@@ 0" in out.getvalue()


def test_cli_release_helper_sends_agent_token_and_parses_count(monkeypatch):
    sent = []

    class _Client:
        def __init__(self, **kw):
            pass

        def exec_python(self, code):
            sent.append(code)
            return {"output": [{"output": "@@UAP_CB_RELEASED@@ 3\n"}]}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Client)
    assert _REAL_RELEASE("demo", "agentA") == 3
    assert "release_all('agentA')" in sent[0]
    monkeypatch.setenv("UAP_AGENT_ID", "envTok")
    _REAL_RELEASE("demo", None)
    assert "release_all('envTok')" in sent[1]


def test_cli_release_helper_swallows_an_unreachable_editor(monkeypatch):
    class _Boom:
        def __init__(self, **kw):
            raise OSError("no editor")

    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Boom)
    assert _REAL_RELEASE("demo", "a") is None


def test_pie_stop_and_lease_release_call_release_with_the_agent(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_release_exec_callbacks",
                        lambda project, agent: calls.append((project, agent)) or 2)
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    def fake(func, params, project=None):
        return {"StopPIEEx": json.dumps({"ok": True, "was_playing": True}),
                "IsPIEInProgress": False}[func]

    monkeypatch.setattr(cli, "_rc_call", fake)
    assert cli.main(["pie", "stop", "--agent", "agentA", "--project", "demo"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])[
        "exec_callbacks_released"] == 2
    assert calls == [("demo", "agentA")]
    assert cli.main(["lease", "acquire", "exclusive", "--agent", "agentA",
                     "--project", "demo", "--wait", "0"]) == 0
    capsys.readouterr()
    assert cli.main(["lease", "release", "--agent", "agentA", "--project", "demo"]) == 0
    assert calls[-1] == ("demo", "agentA") and len(calls) == 2


def test_exec_passes_agent_token_as_owner(monkeypatch):
    seen = []
    real = xc.build_client_code
    monkeypatch.setattr(xc, "build_client_code",
                        lambda code, sel, owner="": seen.append(owner) or real(code, sel, owner))

    class _Client:
        def __init__(self, **kw):
            pass

        def exec_python(self, code):
            return {"success": True, "output": []}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Client)
    monkeypatch.setattr(cli, "_standalone_instance_selected", lambda *a, **k: False)
    cli.main(["exec", "x = 1", "--agent", "agentA", "--project", "demo"])
    assert seen == ["agentA"]
    assert "'agentA'" in xc.build_client_code("x", "auto", "agentA")


# ---- 3. missing exec-scope global -------------------------------------------------------

def test_missing_global_warning_names_it_and_says_capture_via_defaults(monkeypatch):
    fake = _setup(monkeypatch)
    _exec("import unreal\nREC_LIST = []\n"
          "def _tick(dt):\n    REC_LIST.append(dt)\n"
          "unreal.register_slate_post_tick_callback(_tick)\n", monkeypatch, fake)
    # The editor rebuilds the exec namespace per call: simulate the global vanishing.
    rec = next(iter(sys.modules["_uap_cb_guard"].live.values()))
    rec["fn"].__globals__.pop("REC_LIST", None)
    fake.tick(1000)
    assert fake.cbs == {} and len(fake.warnings) == 1
    w = fake.warnings[0]
    assert "REC_LIST" in w and "default args or closures" in w and "NameError" in w
