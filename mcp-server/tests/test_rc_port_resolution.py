from unreal_agent_player import cli
from unreal_agent_player.cli import (
    _pie,
    _rc,
    _rc_port_for,
    _read_port_cache,
    _status,
    _write_port_cache,
    build_parser,
)


def test_port_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    assert _read_port_cache("MyProj") == 0          # nothing cached yet
    _write_port_cache("MyProj", 30013)
    assert _read_port_cache("MyProj") == 30013


def test_env_override_wins_over_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_RC_PORT", "39999")
    _write_port_cache("MyProj", 30013)
    assert _rc_port_for("MyProj") == 39999          # env pins the port


def test_cache_used_when_no_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_RC_PORT", raising=False)
    _write_port_cache("MyProj", 30021)
    assert _rc_port_for("MyProj") == 30021


def test_default_port_when_unresolvable(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_RC_PORT", raising=False)
    # No cache, and force exec resolution to fail -> fall back to 30010.
    monkeypatch.setattr(cli, "_exec_rc_port", lambda project: 0)
    assert _rc_port_for("Nope") == 30010


def test_rc_verbs_accept_project():
    a = build_parser().parse_args(["status", "--project", "PBW"])
    assert a.func is _status and a.project == "PBW"
    b = build_parser().parse_args(["rc", "GetPluginVersion", "--project", "PBW"])
    assert b.func is _rc and b.project == "PBW"
    c = build_parser().parse_args(["pie", "start", "--project", "PBW"])
    assert c.func is _pie and c.project == "PBW"


def test_rc_verbs_default_project():
    a = build_parser().parse_args(["status"])
    assert a.project == "SchoolsOut"
