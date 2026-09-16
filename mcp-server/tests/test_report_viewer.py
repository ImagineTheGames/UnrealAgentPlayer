"""One reusable report window per session, instead of one browser tab per run."""
import json

from unreal_agent_player.cli import main
from unreal_agent_player.reporting import viewer


class FakeDesktop:
    """A stand-in window list: `_spawn` adds a window, WM_CLOSE removes it."""

    def __init__(self, monkeypatch, *, others=()):
        self.windows = [(h, t, c) for h, t, c in others]
        self.closed = []
        self._next = 1000
        monkeypatch.setattr(viewer, "find_app_browser", lambda: r"C:\fake\chrome.exe")
        monkeypatch.setattr(viewer, "list_windows", lambda: list(self.windows))
        monkeypatch.setattr(viewer, "_spawn", self._spawn)
        monkeypatch.setattr(viewer, "close_window", self._close)

    def _spawn(self, exe, url):
        self._next += 1
        self.windows.append((self._next, viewer.TITLE_PREFIX + "a test", "Chrome_WidgetWin_1"))
        return True

    def _close(self, hwnd):
        self.closed.append(hwnd)
        self.windows = [w for w in self.windows if w[0] != hwnd]
        return True

    @property
    def report_windows(self):
        return [w for w in self.windows if w[1].startswith(viewer.TITLE_PREFIX)]


def _run_report(tmp_path, monkeypatch, *extra):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    assert main(["report", "start", "a test"]) == 0
    assert main(["report", "finish", "pass", "done", "--keep-pie", *extra]) == 0


def test_second_report_replaces_the_first_window(tmp_path, monkeypatch, capsys):
    """The whole point: two verifications leave ONE window, not two tabs."""
    desktop = FakeDesktop(monkeypatch)
    _run_report(tmp_path, monkeypatch)
    first = desktop.report_windows[0][0]
    _run_report(tmp_path, monkeypatch)
    assert desktop.closed == [first]
    assert len(desktop.report_windows) == 1
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["opened"] is True
    # The per-run path agents quote is unchanged: still this run's own index.html.
    assert body["html"].endswith("index.html") and str(tmp_path) in body["html"]


def test_never_closes_a_window_it_did_not_open(tmp_path, monkeypatch):
    """Handles get recycled, so a recorded handle is only closed while it still carries the
    report caption. Closing a stranger's window would take their tabs with it."""
    other = (1000, "Some other window", "Chrome_WidgetWin_1")
    desktop = FakeDesktop(monkeypatch, others=[other])
    _run_report(tmp_path, monkeypatch)
    ours = desktop.report_windows[0][0]
    # The window we recorded is gone and its handle now names something unrelated.
    desktop.windows = [(ours, "Somebody else's window", "Chrome_WidgetWin_1"), other]
    _run_report(tmp_path, monkeypatch)
    assert desktop.closed == []
    assert other in desktop.windows


def test_no_open_flag_renders_but_shows_nothing(tmp_path, monkeypatch, capsys):
    desktop = FakeDesktop(monkeypatch)
    _run_report(tmp_path, monkeypatch, "--no-open")
    assert desktop.report_windows == []
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["ok"] is True and body["opened"] is False
    assert "door" not in body["html"]  # sanity: the path is still emitted
    runs = [p for p in tmp_path.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert (runs[0] / "index.html").exists()


def test_no_open_env_var(tmp_path, monkeypatch):
    desktop = FakeDesktop(monkeypatch)
    monkeypatch.setenv("UAP_REPORT_NO_OPEN", "1")
    _run_report(tmp_path, monkeypatch)
    assert desktop.report_windows == []


def test_legacy_no_browser_env_still_honoured(tmp_path, monkeypatch):
    desktop = FakeDesktop(monkeypatch)
    monkeypatch.setenv("UAP_NO_BROWSER", "1")
    _run_report(tmp_path, monkeypatch)
    assert desktop.report_windows == []


def test_falsey_no_open_value_does_not_suppress(tmp_path, monkeypatch):
    """`UAP_REPORT_NO_OPEN=0` means "open", not "any value is truthy"."""
    desktop = FakeDesktop(monkeypatch)
    monkeypatch.setenv("UAP_REPORT_NO_OPEN", "0")
    _run_report(tmp_path, monkeypatch)
    assert len(desktop.report_windows) == 1


def test_falls_back_to_a_plain_tab_without_a_chromium(tmp_path, monkeypatch):
    """No Chrome/Edge (or a non-Windows box) -> the pre-existing behaviour, a tab."""
    import webbrowser

    opened = []
    monkeypatch.setattr(viewer, "find_app_browser", lambda: None)
    monkeypatch.setattr(viewer, "_spawn", lambda exe, url: pytest_fail_spawn())
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    _run_report(tmp_path, monkeypatch)
    assert len(opened) == 1 and opened[0].startswith("file:")


def pytest_fail_spawn():
    raise AssertionError("must not launch an app window when no chromium was found")


def test_report_title_carries_the_window_prefix(tmp_path, monkeypatch):
    """Window matching keys off this caption -- if the prefix goes, reuse silently stops."""
    FakeDesktop(monkeypatch)
    _run_report(tmp_path, monkeypatch)
    runs = [p for p in tmp_path.iterdir() if p.is_dir() and not p.name.startswith(".")]
    html = (runs[0] / "index.html").read_text(encoding="utf-8")
    assert f"<title>{viewer.TITLE_PREFIX}a test</title>" in html
