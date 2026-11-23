"""`uap exec` refuses NavigationSystemV1 statics with no world context (CDO ensure)."""

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


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _Fake.ran = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Fake)
    monkeypatch.setattr(cli, "_exec_client_is_noop", lambda *a, **k: True)


BAD = [
    "unreal.NavigationSystemV1.find_path_to_location_synchronously()",
    "unreal.NavigationSystemV1.find_path_to_location_synchronously(None, a, b)",
    "unreal.NavigationSystemV1.project_point_to_navigation(, p)",
    "NavigationSystemV1.get_navigation_system( )",
]
GOOD = [
    "unreal.NavigationSystemV1.find_path_to_location_synchronously(w, a, b)",
    "unreal.NavigationSystemV1.project_point_to_navigation(pie_world, p)",
    "unreal.NavigationSystemV1.get_navigation_system(world)",
]


@pytest.mark.parametrize("code", BAD)
def test_refused(code):
    assert cli._exec_nav_cdo_refusal(code)


@pytest.mark.parametrize("code", GOOD)
def test_world_passed_ok(code):
    assert cli._exec_nav_cdo_refusal(code) is None


def test_exec_refuses_and_does_not_run(capsys):
    assert cli.main(["exec", BAD[0]]) == 1
    assert _Fake.ran == []
    assert "class default object" in json.loads(capsys.readouterr().out)["error"]
