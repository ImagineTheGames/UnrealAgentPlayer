"""`uap exec` refuses ANY NavigationSystemV1 static while PIE is live (CDO ensure), and the
no-world spelling always."""

import json
from typing import ClassVar

import pytest

from unreal_agent_player import cli


class _Fake:
    ran: ClassVar[list] = []

    def __init__(self, *a, **k):
        pass

    def exec_python(self, code):
        _Fake.ran.append(code)
        return {"success": True, "result": "None", "output": []}


_PIE = {"live": True}


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _Fake.ran = []
    _PIE["live"] = True
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Fake)
    monkeypatch.setattr(cli, "_exec_client_is_noop", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_rc_call", lambda func, params, project=None, **k: _PIE["live"])


NOWORLD = [
    "unreal.NavigationSystemV1.find_path_to_location_synchronously()",
    "unreal.NavigationSystemV1.find_path_to_location_synchronously(None, a, b)",
    "unreal.NavigationSystemV1.project_point_to_navigation(, p)",
    "NavigationSystemV1.get_navigation_system( )",
]
WITH_WORLD = [
    "unreal.NavigationSystemV1.find_path_to_location_synchronously(w, a, b)",
    "unreal.NavigationSystemV1.project_point_to_navigation(pie_world, p)",
    "unreal.NavigationSystemV1.get_navigation_system(world)",
    "unreal.NavigationSystemV1.is_navigation_being_built(w)",
]
UNRELATED = [
    "unreal.SONavDevLibrary.find_nav_path(w, a, b)",
    "nav.find_path_to_location_synchronously(w, a, b)",
    "print('NavigationSystemV1 is a class')",
]


@pytest.mark.parametrize("code", NOWORLD)
def test_noworld_refused_even_without_pie(code):
    _PIE["live"] = False
    assert cli._exec_nav_cdo_refusal(code)


@pytest.mark.parametrize("code", WITH_WORLD)
def test_world_arg_still_refused_while_pie_live(code):
    msg = cli._exec_nav_cdo_refusal(code)
    assert msg and "SONavDevLibrary" in msg


@pytest.mark.parametrize("code", WITH_WORLD)
def test_world_arg_allowed_when_no_pie(code):
    _PIE["live"] = False
    assert cli._exec_nav_cdo_refusal(code) is None


@pytest.mark.parametrize("code", UNRELATED)
def test_unrelated_passes(code):
    assert cli._exec_nav_cdo_refusal(code) is None


def test_editor_unreachable_allows(monkeypatch):
    def boom(*a, **k):
        raise cli.AgentError(next(iter(cli.ErrorCode)), "down")
    monkeypatch.setattr(cli, "_rc_call", boom)
    assert cli._exec_nav_cdo_refusal(WITH_WORLD[0]) is None


def test_exec_refuses_and_does_not_run(capsys):
    assert cli.main(["exec", WITH_WORLD[0]]) == 1
    assert _Fake.ran == []
    assert "SONavDevLibrary" in json.loads(capsys.readouterr().out)["error"]
