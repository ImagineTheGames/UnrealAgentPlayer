import webbrowser

import pytest

from unreal_agent_player import cli
from unreal_agent_player import coordination as _coordination
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
def _isolated_coordination_state(tmp_path_factory, monkeypatch):
    """No test may read or write the developer's REAL lease state.

    `$UAP_REPORTS_DIR` roots the lease files (`coordination._reports_base`), so a test that drove
    a lease verb without setting it wrote into `~/.uap-reports/.leases/` on the real machine.
    That was already untidy; it became load-bearing once the machine-wide foreground lock landed,
    because a test running `pie start` left a REAL machine hold behind and blocked every live
    agent on the box for the full TTL.

    Also pins the wait cap to 0: a test must never sit in a coordination poll loop. Tests that
    exercise waiting set their own values, and this fixture runs before they do.
    """
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path_factory.mktemp("uap-reports")))
    monkeypatch.setenv("UAP_LEASE_WAIT", "0")


@pytest.fixture(autouse=True)
def _no_live_contract(monkeypatch):
    """Default every test to "the editor's contract could not be read".

    There is no editor in a test run, and a real one on the developer's machine must not change
    what the suite asserts. None is also the documented degradation (contract.py): the CLI
    behaves exactly as it did before the contract check existed. Tests that exercise the check
    monkeypatch `cli._live_contract` themselves.
    """
    monkeypatch.setattr(cli, "_live_contract", lambda project=None: None)


@pytest.fixture(autouse=True)
def _no_live_editor_probe(monkeypatch):
    """Default the dead-editor probe to "cannot tell" (None), which never evicts.

    The probe looks at the REAL process list, so without this a developer with no editor open would
    see every test that takes a `pie`/`level` lease have it voided on the next poll, and one with
    an editor open would see different results again. Tests of the eviction itself set their own
    `coordination._editor_probe` (or fake `_enumerate_editors`).
    """
    monkeypatch.setattr(_coordination, "_editor_probe", lambda project: None)
    # Same for the machine lock's live-PIE probe, which the CLI wires to a real RC call: with no
    # editor in a test it must not run (tests of it install `cli._machine_pie_probe` themselves).
    monkeypatch.setattr(_coordination, "_machine_pie_probe", None)
    # ...and for child processes, which inherit the environment but not the monkeypatch above
    # (the lease-contention tests spawn real workers that take `pie` leases).
    monkeypatch.setenv("UAP_EDITOR_LIVENESS", "0")
    _coordination._EDITOR_CACHE.clear()
