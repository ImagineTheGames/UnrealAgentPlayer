"""Show a finished report in ONE reusable window instead of one new tab per run.

`uap report finish` used to hand the freshly rendered `index.html` to `webbrowser.open`,
which on Windows is `os.startfile`: the shell passes the URL to the default browser and
Chrome ALWAYS appends a tab. A testing session with a dozen verifications left a dozen
dead report tabs behind, each holding a renderer process.

What this module does instead: open the report in a dedicated browser **app window**
(`--app=<file url>`, Chrome or Edge) and close the window the PREVIOUS report opened. An
app window is its own top-level OS window whose caption is the page title, and that is the
one property a plain tab does not have -- a tab cannot be found in the window list, so it
can be neither recognised nor replaced. The report HTML itself is untouched: it still
loads at its own per-run URL, so screenshots, the in-page tab strip and the lightbox
behave exactly as before, and the path printed by `report finish` still points at it.

Only windows this module opened and recorded (by handle, in `.viewer.json` under the
reports root) are ever closed, and only while their caption still starts with
`TITLE_PREFIX`. A normal browser window that merely happens to be showing a report is
never touched, because closing it would take the user's other tabs with it.

Every failure degrades to the old behaviour rather than erroring:
  * no Chrome/Edge found, or not Windows -> `webbrowser.open`, i.e. a plain tab
  * new window not identified in time    -> nothing recorded, nothing closed next run
  * previous window already gone         -> nothing to close

Opt out of opening anything at all with `--no-open` / `UAP_REPORT_NO_OPEN=1` (the older
`UAP_NO_BROWSER=1` still works). Useful for agents that never look at the page.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

from unreal_agent_player.reporting.session import _reports_root as reports_root

# Every rendered report carries this caption prefix (render.py builds the <title> from it),
# which is how a report window is told apart from any other browser window.
TITLE_PREFIX = "UAP report: "

# Chrome, Edge and every other Chromium use this window class, app windows included.
_BROWSER_WINDOW_CLASS = "Chrome_WidgetWin_1"

_STATE_NAME = ".viewer.json"
_FALSEY = {"", "0", "false", "no", "off"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in _FALSEY


def env_no_open() -> bool:
    """True when the environment says not to open a browser at all."""
    return _env_flag("UAP_REPORT_NO_OPEN") or _env_flag("UAP_NO_BROWSER")


def _open_timeout() -> float:
    try:
        return max(0.0, float(os.environ.get("UAP_REPORT_OPEN_TIMEOUT", "6")))
    except ValueError:
        return 6.0


# --- previous-window bookkeeping -------------------------------------------------------

def _state_file() -> Path:
    return reports_root() / _STATE_NAME


def _read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict[str, Any]) -> None:
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # a lost pointer costs one stale window, never the report


# --- Win32 window list (no-ops off Windows) --------------------------------------------

def list_windows() -> list[tuple[int, str, str]]:
    """(hwnd, caption, window class) for every visible top-level window.

    Empty off Windows or if the enumeration fails, which makes the caller fall back to a
    plain tab rather than guess at what to replace.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        found: list[tuple[int, str, str]] = []
        callback = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _visit(hwnd, _lparam):
            if not u32.IsWindowVisible(hwnd):
                return True
            length = u32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, title, length + 1)
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            found.append((int(hwnd), title.value, cls.value))
            return True

        u32.EnumWindows(callback(_visit), 0)
        return found
    except Exception:
        return []  # no window list -> we simply never recognise or close anything


def close_window(hwnd: int) -> bool:
    """Ask one window to close (WM_CLOSE, the same thing the X button posts)."""
    if os.name != "nt" or not hwnd:
        return False
    try:
        import ctypes

        WM_CLOSE = 0x0010
        return bool(ctypes.windll.user32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0))
    except Exception:
        return False


# --- browser discovery ------------------------------------------------------------------

# Vendor subpath under %ProgramFiles% / %LocalAppData%, per executable. Nothing machine-local
# is baked in: this repo is shared across workstations, so every path is derived from an
# environment variable or the registry.
_APP_BROWSERS = (
    ("chrome.exe", r"Google\Chrome\Application\chrome.exe", ("google-chrome", "chromium")),
    ("msedge.exe", r"Microsoft\Edge\Application\msedge.exe", ("microsoft-edge",)),
)


def _app_paths_lookup(exe: str) -> str | None:
    """Resolve an executable through the Windows App Paths registry key."""
    try:
        import winreg

        sub = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, sub) as key:
                    value = winreg.QueryValueEx(key, "")[0]
            except OSError:
                continue
            if value and Path(value).exists():
                return str(value)
    except Exception:
        pass
    return None


def find_app_browser() -> str | None:
    """A browser that supports `--app=<url>`, or None to fall back to a plain tab."""
    override = os.environ.get("UAP_BROWSER_EXE", "").strip()
    if override:
        return override if Path(override).exists() else None
    for exe, subpath, posix_names in _APP_BROWSERS:
        if os.name != "nt":
            for name in posix_names:
                found = shutil.which(name)
                if found:
                    return found
            continue
        found = _app_paths_lookup(exe) or shutil.which(exe)
        if found and Path(found).exists():
            return found
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if not base:
                continue
            candidate = Path(base) / subpath
            if candidate.exists():
                return str(candidate)
    return None


def _spawn(exe: str, url: str) -> bool:
    """Launch the app window. Detached: the report outlives this CLI invocation."""
    try:
        subprocess.Popen([exe, f"--app={url}"], close_fds=True)
        return True
    except Exception:
        return False


def _await_window(before: set[int], deadline: float) -> int:
    """Handle of the app window we just opened, or 0 if it never identified itself.

    A window is ours only if it is new (absent from `before`), is a Chromium window, and
    carries the report caption -- which it does not until the page loads, hence the poll.
    """
    while True:
        for hwnd, title, cls in list_windows():
            if hwnd not in before and cls == _BROWSER_WINDOW_CLASS \
                    and title.startswith(TITLE_PREFIX):
                return hwnd
        if time.monotonic() >= deadline:
            return 0
        time.sleep(0.2)


def _close_previous(hwnd: Any, keep: int) -> bool:
    """Close the window the previous report opened, if it is still that window.

    The caption is re-checked because window handles are recycled: a stale handle could
    otherwise name something else entirely by the time we get here.
    """
    try:
        hwnd = int(hwnd or 0)
    except (TypeError, ValueError):
        return False
    if not hwnd or hwnd == keep:
        return False
    for h, title, cls in list_windows():
        if h == hwnd and cls == _BROWSER_WINDOW_CLASS and title.startswith(TITLE_PREFIX):
            return close_window(hwnd)
    return False


def open_report(html_path: Any, *, no_open: bool | None = None) -> dict[str, Any]:
    """Show `html_path`, replacing the window the previous report opened.

    Returns `{"opened": bool, "mode": "app"|"tab"|"none", ...}`. Never raises: a report
    that rendered is a report that succeeded, whatever the browser does with it.
    """
    if no_open is None:
        no_open = env_no_open()
    if no_open:
        return {"opened": False, "mode": "none", "reason": "no-open"}

    url = Path(html_path).as_uri()
    exe = find_app_browser()
    if not exe:
        # No Chromium to give us a window we can recognise later: old behaviour, a tab.
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            pass
        return {"opened": opened, "mode": "tab", "reason": "no-app-browser"}

    previous = _read_state().get("hwnd")
    before = {hwnd for hwnd, _title, _cls in list_windows()}
    if not _spawn(exe, url):
        opened = False
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            pass
        return {"opened": opened, "mode": "tab", "reason": "app-launch-failed"}

    hwnd = _await_window(before, time.monotonic() + _open_timeout())
    closed = _close_previous(previous, hwnd)
    _write_state({"hwnd": hwnd, "url": url, "opened_at": time.time()})
    return {"opened": True, "mode": "app", "window": hwnd, "replaced_previous": closed}
