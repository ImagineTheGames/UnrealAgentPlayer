import webbrowser

import pytest

from unreal_agent_player import cli


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Tests must never pop a real browser window. `uap report finish` calls
    webbrowser.open() to show the HTML report; several tests exercise that path, which
    otherwise spawns browser tabs on the developer's machine during every test run."""
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _no_live_contract(monkeypatch):
    """Default every test to "the editor's contract could not be read".

    There is no editor in a test run, and a real one on the developer's machine must not change
    what the suite asserts. None is also the documented degradation (contract.py): the CLI
    behaves exactly as it did before the contract check existed. Tests that exercise the check
    monkeypatch `cli._live_contract` themselves.
    """
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
