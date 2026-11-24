"""`uap pie start --clients N --net-mode standalone|listen|client`.

A multi-client hub test (Photon / EOS: every player signs in separately) needs each client to be
its OWN standalone game. The editor layout (PlayNetMode / PlayNumberOfClients / RunUnderOneProcess)
cannot be set from Python or RemoteControl, so the plugin exports ConfigurePIENet / RestorePIENet
and the CLI drives them. These pin: the layout is set BEFORE the start, `pie stop` restores it, a
refused start restores it too, an old plugin is refused (never silently a listen server), and the
DevAuthTool credential names a tester must create are reported.
"""

import json

import pytest

from unreal_agent_player import cli


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    state = {"now": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(cli.time, "sleep", lambda s: state.__setitem__("now", state["now"] + s))


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _stub(monkeypatch, table):
    seen = []

    def fake(func, params, project=None):
        seen.append((func, params))
        if func not in table:
            raise AssertionError(f"unexpected RC call {func}")
        val = table[func]
        return val(params) if callable(val) else val

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


_CFG = json.dumps({"ok": True, "net_mode": "standalone", "clients": 2, "one_process": True})
_STARTED = json.dumps({"ok": True, "mode": "flat"})


def test_standalone_two_clients_configures_then_starts(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"ConfigurePIENet": _CFG, "StartPIEMode": _STARTED, "IsInPIE": True})
    assert cli.main(["pie", "start", "--clients", "2", "--net-mode", "standalone"]) == 0
    body = _out(capsys)
    names = [f for f, _ in seen]
    assert names.index("ConfigurePIENet") < names.index("StartPIEMode")
    assert seen[0][1] == {"NetMode": "standalone", "Clients": 2, "bRunUnderOneProcess": True}
    assert body["playing"] is True
    assert body["devauth_credentials"] == ["Context_1", "Context_2"]


def test_clients_alone_is_a_listen_server(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"ConfigurePIENet": _CFG, "StartPIEMode": _STARTED, "IsInPIE": True})
    assert cli.main(["pie", "start", "--clients", "3"]) == 0
    assert seen[0][1] == {"NetMode": "listen", "Clients": 3, "bRunUnderOneProcess": True}


def test_net_mode_alone_defaults_to_two_clients(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"ConfigurePIENet": _CFG, "StartPIEMode": _STARTED, "IsInPIE": True})
    assert cli.main(["pie", "start", "--net-mode", "client"]) == 0
    assert seen[0][1]["Clients"] == 2 and seen[0][1]["NetMode"] == "client"


def test_separate_processes_clears_one_process(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"ConfigurePIENet": _CFG, "StartPIEMode": _STARTED, "IsInPIE": True})
    assert cli.main(["pie", "start", "--net-mode", "standalone", "--separate-processes"]) == 0
    assert seen[0][1]["bRunUnderOneProcess"] is False


def test_plain_start_never_touches_the_layout(monkeypatch, capsys):
    seen = _stub(monkeypatch, {"StartPIEMode": _STARTED, "IsInPIE": True})
    assert cli.main(["pie", "start"]) == 0
    assert "ConfigurePIENet" not in [f for f, _ in seen]
    assert "devauth_credentials" not in _out(capsys)


def test_old_plugin_is_refused_not_downgraded(monkeypatch, capsys):
    """Silently starting the editor's last layout would hand back a listen server while the caller
    thinks it has two standalone games -- the exact failure this verb exists to remove."""
    def boom(params):
        raise cli.AgentError(cli.ErrorCode.UE_OBJECT_NOT_FOUND, "404 not found", recoverable=False)
    monkeypatch.setattr(cli, "_is_missing_verb", lambda exc: True)
    seen = _stub(monkeypatch, {"ConfigurePIENet": boom})
    assert cli.main(["pie", "start", "--clients", "2", "--net-mode", "standalone"]) == 1
    body = _out(capsys)
    assert body["ok"] is False and "ConfigurePIENet" in body["error"]
    assert "StartPIEMode" not in [f for f, _ in seen]


def test_refused_layout_does_not_start(monkeypatch, capsys):
    bad = json.dumps({"ok": False, "error": "PIE is live or queued"})
    seen = _stub(monkeypatch, {"ConfigurePIENet": bad})
    assert cli.main(["pie", "start", "--clients", "2"]) == 1
    assert "PIE is live" in _out(capsys)["error"]
    assert "StartPIEMode" not in [f for f, _ in seen]


def test_failed_start_restores_the_layout(monkeypatch, capsys):
    seen = _stub(monkeypatch, {
        "ConfigurePIENet": _CFG,
        "StartPIEMode": json.dumps({"ok": False, "mode": "flat", "error": "no GEditor"}),
        "RestorePIENet": json.dumps({"ok": True, "restored": True}),
    })
    assert cli.main(["pie", "start", "--clients", "2"]) == 1
    assert [f for f, _ in seen][-1] == "RestorePIENet"


def test_stop_reports_the_restored_layout(monkeypatch, capsys):
    """The plugin restores inside StopPIEEx (no extra round trip on every stop) and says so."""
    seen = _stub(monkeypatch, {
        "StopPIEEx": json.dumps({"ok": True, "net_restored": {
            "ok": True, "restored": True, "net_mode": "listen", "clients": 1,
            "one_process": True}}),
        "IsPIEInProgress": False,
    })
    assert cli.main(["pie", "stop"]) == 0
    assert _out(capsys)["net_restored"]["net_mode"] == "listen"
    assert [f for f, _ in seen] == ["StopPIEEx", "IsPIEInProgress"]


def test_plain_stop_has_no_net_noise(monkeypatch, capsys):
    _stub(monkeypatch, {"StopPIEEx": json.dumps({"ok": True}), "IsPIEInProgress": False})
    assert cli.main(["pie", "stop"]) == 0
    assert "net_restored" not in _out(capsys)


def test_vr_and_net_flags_conflict(monkeypatch, capsys):
    seen = _stub(monkeypatch, {})
    assert cli.main(["pie", "start", "--mode", "vr", "--clients", "2"]) == 1
    assert seen == []


@pytest.mark.parametrize("n", ["0", "17"])
def test_client_count_bounds(monkeypatch, capsys, n):
    seen = _stub(monkeypatch, {})
    assert cli.main(["pie", "start", "--clients", n]) == 1
    assert seen == []


def test_plugin_header_declares_the_verbs():
    """The contract-skew check reads this header; the verbs the CLI calls must be in it."""
    from unreal_agent_player import contract
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "Plugin/Source/UnrealAgentPlayer/Public/UAPAgentSubsystem.h").read_text()
    for verb in ("ConfigurePIENet", "RestorePIENet"):
        assert f" {verb}(" in text
        assert verb in contract.PURPOSE
