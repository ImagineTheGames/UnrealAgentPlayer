"""`uap pie start` (flat) gives every PIE client a standing head.

Without it a VR pawn run with no headset has its camera at the tracking origin: the floor.
"""

import json

import pytest

from unreal_agent_player import cli

_REAL = cli._pie_default_head


@pytest.fixture(autouse=True)
def _real_default_head(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.delenv("UAP_HEAD_HEIGHT", raising=False)
    monkeypatch.setattr(cli, "_pie_default_head", _REAL)


def _stub(monkeypatch, head_supported=True):
    seen = []

    def fake(func, params, project=None):
        seen.append((func, params))
        if func == "StartPIEMode":
            return json.dumps({"ok": True, "mode": "flat"})
        if func == "IsInPIE":
            return True
        if func == "GetXRControllerState":
            return {"clients": [{"head": {}}]} if head_supported else {"clients": [{}]}
        if func == "InjectXRControllerPose":
            return True
        raise AssertionError(f"unexpected RC call {func}")

    monkeypatch.setattr(cli, "_rc_call", fake)
    monkeypatch.setattr(cli, "_rc_json", lambda f, p, proj=None, needs=None: fake(f, p, proj))
    return seen


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _head_calls(seen):
    return [p for f, p in seen if f == "InjectXRControllerPose"]


def test_flat_start_places_standing_head_for_all_clients(monkeypatch, capsys):
    seen = _stub(monkeypatch)
    assert cli.main(["pie", "start"]) == 0
    calls = _head_calls(seen)
    assert len(calls) == 1
    assert calls[0]["Hand"] == "Head" and calls[0]["Client"] == "all"
    assert calls[0]["Position"]["Z"] == 170.0
    body = _out(capsys)
    assert body["default_head"]["applied"] is True


def test_head_height_flag_and_env(monkeypatch, capsys):
    seen = _stub(monkeypatch)
    assert cli.main(["pie", "start", "--head-height", "160"]) == 0
    assert _head_calls(seen)[0]["Position"]["Z"] == 160.0
    seen.clear()
    monkeypatch.setenv("UAP_HEAD_HEIGHT", "165")
    assert cli.main(["pie", "start"]) == 0
    assert _head_calls(seen)[0]["Position"]["Z"] == 165.0


def test_no_head_flag_skips(monkeypatch, capsys):
    seen = _stub(monkeypatch)
    assert cli.main(["pie", "start", "--no-head"]) == 0
    assert _head_calls(seen) == []
    assert "default_head" not in _out(capsys)


def test_vr_mode_never_gets_a_fake_head(monkeypatch, capsys):
    seen = _stub(monkeypatch)
    monkeypatch.setattr(cli, "_rc_call", lambda f, p, proj=None: (
        json.dumps({"ok": True, "mode": "vr"}) if f == "StartPIEMode" else True))
    assert cli.main(["pie", "start", "--mode", "vr"]) == 0
    assert "default_head" not in _out(capsys)


def test_old_plugin_without_head_support_does_not_fail_start(monkeypatch, capsys):
    seen = _stub(monkeypatch, head_supported=False)
    assert cli.main(["pie", "start"]) == 0
    body = _out(capsys)
    assert body["default_head"]["applied"] is False
    assert "default head not placed" in body["default_head"]["note"]
    assert _head_calls(seen) == []
