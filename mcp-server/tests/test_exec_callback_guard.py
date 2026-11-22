# ruff: noqa: S102
"""`uap exec` tick callbacks must not outlive PIE or spam on error [ClickUp 17tm4675e8t].

2026-11-18: a callback registered through `uap exec` kept ticking after PIE ended, its handler hit
a NameError every frame and wrote 2.4 M log lines. The guard is real editor-side Python, so it is
run here against a fake `unreal` with a tick pump.
"""

import contextlib
import io
import sys
import types

from unreal_agent_player import exec_client as xc


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


def _exec(code, monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "unreal", fake)
    text = xc.build_client_code(code, "auto")
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(text, "<test>", "exec"), {})


def _setup(monkeypatch):
    monkeypatch.delitem(sys.modules, "_uap_cb_guard", raising=False)
    _Sub.pie = True
    return _Fake()


# The real incident: handler raises, its own except-handler reads a vanished global.
_BAD = (
    "import unreal\n"
    "REC = []\n"
    "def _tick(dt):\n"
    "    try:\n"
    "        None.get_controlled_pawn()\n"
    "    except Exception:\n"
    "        REC.append(1)\n"
    "unreal.register_slate_post_tick_callback(_tick)\n"
)


def test_raising_callback_unregisters_after_first_exception_and_logs_once(monkeypatch):
    fake = _setup(monkeypatch)
    _exec(_BAD.replace("REC.append(1)", "REC.append(1)\n        raise RuntimeError('boom')"),
          monkeypatch, fake)
    # Second exec: exec namespace rebuilt, so REC is gone, like the incident.
    assert len(fake.cbs) == 1
    fake.tick(1000)
    assert fake.cbs == {}, "callback must unregister after its first exception"
    assert len(fake.warnings) == 1, fake.warnings
    assert "_tick" in fake.warnings[0] and "raised" in fake.warnings[0]


def test_nameerror_from_vanished_exec_global_does_not_repeat(monkeypatch):
    fake = _setup(monkeypatch)
    code = ("import unreal\nREC = 1\n"
            "def _tick(dt):\n    REC.x\n"
            "unreal.register_slate_post_tick_callback(_tick)\n")
    _exec(code, monkeypatch, fake)
    fake.tick(5000)
    assert fake.cbs == {} and len(fake.warnings) == 1
    assert "NameError" in fake.warnings[0] or "AttributeError" in fake.warnings[0]


def test_pie_bound_callback_unregisters_when_pie_ends(monkeypatch):
    fake = _setup(monkeypatch)
    ticks = []
    fake.ticks = ticks
    _exec("import unreal\ndef _tick(dt):\n    unreal.ticks.append(1)\n"
          "unreal.register_slate_post_tick_callback(_tick)\n", monkeypatch, fake)
    fake.tick(3)
    assert len(ticks) == 3 and fake.cbs
    _Sub.pie = False
    fake.tick(10)
    assert fake.cbs == {}, "callback registered during PIE must be dropped when PIE ends"
    assert len(ticks) == 3, "no further handler calls after PIE ended"
    assert len(fake.warnings) == 1 and "PIE ended" in fake.warnings[0]


def test_callback_registered_without_pie_survives_pie_end(monkeypatch):
    fake = _setup(monkeypatch)
    _Sub.pie = False
    ticks = []
    fake.ticks = ticks
    _exec("import unreal\ndef _tick(dt):\n    unreal.ticks.append(1)\n"
          "unreal.register_slate_post_tick_callback(_tick)\n", monkeypatch, fake)
    fake.tick(4)
    assert len(ticks) == 4 and len(fake.cbs) == 1


def test_release_all_drops_every_tracked_callback_and_guard_installs_once(monkeypatch):
    fake = _setup(monkeypatch)
    code = ("import unreal\ndef _a(dt):\n    pass\n"
            "unreal.register_slate_post_tick_callback(_a)\n")
    _exec(code, monkeypatch, fake)
    _exec(code, monkeypatch, fake)
    guard = sys.modules["_uap_cb_guard"]
    assert len(fake.cbs) == 2 and len(guard.live) == 2
    guard.release_all()
    assert fake.cbs == {} and guard.live == {}
    monkeypatch.delitem(sys.modules, "_uap_cb_guard", raising=False)
