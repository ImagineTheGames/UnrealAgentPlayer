"""`uap level open|current`: the SAFE level change [ClickUp 86akezbrp].

load_level from editor Python runs the map load nested inside a live Python frame and has killed
the shared editor. The verb queues the load in the plugin (outside Python); these tests pin the
CLI half: what it sends, that it waits through a busy game thread, that it refuses what the plugin
refuses, that it reads the map back, and that `exec` no longer lets Python swap the level.
"""

import json

import pytest

from unreal_agent_player import cli, contract
from unreal_agent_player.errors import AgentError, ErrorCode

MAP = "/MarbleRace/Maps/Map_MarbleRace"


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


def _stub(monkeypatch, table):
    seen = []

    def fake(func, params, project=None):
        seen.append((func, dict(params)))
        val = table[func]
        if isinstance(val, list):          # a script of answers, consumed in order
            val = val.pop(0)
        if isinstance(val, Exception):
            raise val
        return val(params) if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _timeout():
    return AgentError(ErrorCode.TIMEOUT, "request timed out")


def _done(ok=True, **extra):
    body = {"ok": ok, "state": "done" if ok else "failed", "request": 1, "map": MAP,
            "map_before": "/Game/SchoolsOut/Levels/L_X", "map_after": MAP if ok else "/Game/L_X",
            "load_seconds": 2.5}
    body.update(extra)
    return json.dumps(body)


def _current(map_=MAP):
    return json.dumps({"ok": True, "map": map_, "world": map_.rsplit("/", 1)[-1], "dirty": False,
                       "pie_in_progress": False, "open_state": "done"})


def test_level_open_queues_waits_and_reads_back(monkeypatch, capsys):
    seen = _stub(monkeypatch, {
        "OpenLevel": json.dumps({"ok": True, "queued": True, "request": 1, "map": MAP,
                                 "mounted_plugin": "MarbleRace"}),
        "GetLevelOpenStatus": [json.dumps({"ok": True, "state": "pending", "request": 1}),
                               _done(cleared_globals='{"ok":true,"count":0}')],
        "GetCurrentLevel": _current(),
    })
    assert cli.main(["level", "open", MAP]) == 0
    assert seen[0] == ("OpenLevel", {"Map": MAP, "bDiscard": False})
    body = _out(capsys)
    assert body["ok"] is True and body["map"] == MAP
    assert body["readback"]["map"] == MAP
    assert body["mounted_plugin"] == "MarbleRace"
    assert [s[0] for s in seen] == ["OpenLevel", "GetLevelOpenStatus", "GetLevelOpenStatus",
                                    "GetCurrentLevel"]


def test_discard_flag_is_sent_and_default_is_false(monkeypatch, capsys):
    seen = _stub(monkeypatch, {
        "OpenLevel": json.dumps({"ok": True, "queued": True, "request": 1, "map": MAP}),
        "GetLevelOpenStatus": _done(), "GetCurrentLevel": _current()})
    assert cli.main(["level", "open", MAP, "--discard"]) == 0
    assert seen[0][1]["bDiscard"] is True
    assert _out(capsys)["discard"] is True


def test_a_busy_game_thread_is_still_loading_not_a_failure(monkeypatch, capsys):
    """While the map loads the editor answers nothing: timeouts are polled through."""
    _stub(monkeypatch, {
        "OpenLevel": json.dumps({"ok": True, "queued": True, "request": 1, "map": MAP}),
        "GetLevelOpenStatus": [_timeout(), _timeout(), _timeout(), _done()],
        "GetCurrentLevel": _current(),
    })
    assert cli.main(["level", "open", MAP]) == 0
    assert _out(capsys)["ok"] is True


def test_plugin_refusal_is_relayed_and_nothing_is_polled(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"OpenLevel": json.dumps(
        {"ok": False, "error": "PIE is running or queued: refusing to swap the editor level"})})
    assert cli.main(["level", "open", MAP]) == 1
    assert "PIE is running" in _out(capsys)["error"]
    assert [s[0] for s in seen] == ["OpenLevel"]


def test_dirty_map_refusal_is_relayed(monkeypatch, capsys):
    _stub(monkeypatch, {"OpenLevel": json.dumps(
        {"ok": False, "error": "the open map '/Game/X' has unsaved changes. Pass --discard"})})
    assert cli.main(["level", "open", MAP]) == 1
    assert "--discard" in _out(capsys)["error"]


def test_a_failed_load_is_a_failure(monkeypatch, capsys):
    _stub(monkeypatch, {
        "OpenLevel": json.dumps({"ok": True, "queued": True, "request": 1, "map": MAP}),
        "GetLevelOpenStatus": _done(ok=False, error="FEditorFileUtils::LoadMap returned false"),
        "GetCurrentLevel": _current("/Game/L_X"),
    })
    assert cli.main(["level", "open", MAP]) == 1
    assert "LoadMap returned false" in _out(capsys)["error"]


def test_success_is_not_trusted_when_the_readback_disagrees(monkeypatch, capsys):
    _stub(monkeypatch, {
        "OpenLevel": json.dumps({"ok": True, "queued": True, "request": 1, "map": MAP}),
        "GetLevelOpenStatus": _done(),
        "GetCurrentLevel": _current("/Game/SomethingElse"),
    })
    assert cli.main(["level", "open", MAP]) == 1
    body = _out(capsys)
    assert body["ok"] is False and "reads back" in body["error"]


def test_a_dead_editor_is_reported_not_polled_forever(monkeypatch, capsys):
    dead = AgentError(ErrorCode.UE_UNREACHABLE, "connection refused")
    _stub(monkeypatch, {
        "OpenLevel": json.dumps({"ok": True, "queued": True, "request": 1, "map": MAP}),
        "GetLevelOpenStatus": [dead] * 20,
    })
    assert cli.main(["level", "open", MAP]) == 1
    assert "may have crashed" in _out(capsys)["error"]


def test_level_current_is_a_pure_read(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"GetCurrentLevel": _current()})
    assert cli.main(["level", "current"]) == 0
    assert [s[0] for s in seen] == ["GetCurrentLevel"]
    assert _out(capsys)["map"] == MAP


def test_older_plugin_is_named_as_skew(monkeypatch, capsys):
    miss = AgentError(ErrorCode.UE_OBJECT_NOT_FOUND, "Remote Control preset call returned 404: x")
    _stub(monkeypatch, {"OpenLevel": miss})
    assert cli.main(["level", "open", MAP]) == 1
    assert "plugin has no OpenLevel" in _out(capsys)["error"]


def test_level_verbs_are_in_the_contract_and_the_extra_header_is_parsed():
    for verb in ("OpenLevel", "GetCurrentLevel", "GetLevelOpenStatus"):
        assert verb in contract.PURPOSE
    exp = contract.expected_contract()
    assert exp["OpenLevel"] == {"Map": "FString", "bDiscard": "bool"}
    assert exp["GetCurrentLevel"] == {}
    assert exp["GetLevelOpenStatus"] == {}


def test_level_waits_out_another_agents_lease_like_every_editor_verb():
    assert "level" in cli._LEASE_GUARDED


# --- exec refuses a level swap from Python --------------------------------------------------

@pytest.mark.parametrize("code", [
    "unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level('/Game/Maps/X')",
    "unreal.EditorLoadingAndSavingUtils.load_map('/Game/Maps/X')",
    "les.new_level('/Game/Maps/New')",
    "unreal.EditorLevelLibrary.load_level('/Game/Maps/X')",
])
def test_exec_refuses_python_that_swaps_the_level(monkeypatch, capsys, code):
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("the script must not reach the editor")

    monkeypatch.setattr(cli, "PythonRemoteExecClient", Boom)
    assert cli.main(["exec", code]) == 1
    err = _out(capsys)["error"]
    assert "uap level open" in err and "NOT run" in err


def test_exec_override_is_explicit(monkeypatch, capsys):
    ran = []

    class Fake:
        last_target = None

        def __init__(self, *a, **k):
            pass

        def exec_python(self, code):
            ran.append(code)
            return {"success": True, "result": "None", "output": []}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", Fake)
    assert cli.main(["exec", "les.load_level('/Game/X')", "--unsafe-level-load"]) == 0
    assert ran


def test_ordinary_exec_is_untouched(monkeypatch, capsys):
    ran = []

    class Fake:
        last_target = None

        def __init__(self, *a, **k):
            pass

        def exec_python(self, code):
            ran.append(code)
            return {"success": True, "result": "1", "output": []}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", Fake)
    assert cli.main(["exec", "print(unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem))"]) == 0
    assert ran


def test_help_documents_the_level_verb(capsys):
    assert cli.main(["help", "level"]) == 0
    out = capsys.readouterr().out
    assert "uap level open" in out and "load_level" in out


def test_exec_file_only_warns_because_builder_scripts_open_their_own_map(monkeypatch, capsys, tmp_path):
    ran = []

    class Fake:
        last_target = None

        def __init__(self, *a, **k):
            pass

        def exec_python(self, code):
            ran.append(code)
            return {"success": True, "result": "None", "output": []}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", Fake)
    script = tmp_path / "build_map.py"
    script.write_text("import unreal\nles.load_level('/Game/Maps/X')\n", encoding="utf-8")
    assert cli.main(["exec-file", str(script)]) == 0
    assert ran
    body = _out(capsys)
    assert "uap level open" in body["warning"] and "ran anyway" in body["warning"]
