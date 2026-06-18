import webbrowser

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Tests must never pop a real browser window. `uap report finish` calls
    webbrowser.open() to show the HTML report; several tests exercise that path, which
    otherwise spawns browser tabs on the developer's machine during every test run."""
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: False)
