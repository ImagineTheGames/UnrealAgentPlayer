import webbrowser

import pytest

from unreal_agent_player import cli
from unreal_agent_player.reporting import viewer


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Tests must never pop a real browser window, nor touch one that is already open.

    `uap report finish` shows the HTML report and several tests exercise that path, which
    would otherwise spawn a browser on the developer's machine during every test run. Both
    routes are stubbed: the fallback (webbrowser.open) and the app-window route -- including
    the window list, so a test can never post WM_CLOSE to a real window."""
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)
    monkeypatch.setattr(viewer, "_spawn", lambda exe, url: True)
    monkeypatch.setattr(viewer, "list_windows", list)
    monkeypatch.setattr(viewer, "close_window", lambda hwnd: False)
    monkeypatch.setenv("UAP_REPORT_OPEN_TIMEOUT", "0")


@pytest.fixture(autouse=True)
def _no_live_contract(monkeypatch):
    """Default every test to "the editor's contract could not be read".

    There is no editor in a test run, and a real one on the developer's machine must not change
    what the suite asserts. None is also the documented degradation (contract.py): the CLI
    behaves exactly as it did before the contract check existed. Tests that exercise the check
    monkeypatch `cli._live_contract` themselves.
    """
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)
