"""Capture a standalone game client's window, and stamp where the image came from.

`uap screenshot` captures the EDITOR viewport over Remote Control. A standalone `-game` client
(what launch_2p_standalone.ps1 starts) is a separate process with its own window, and nothing
in the editor can see it -- so two-client tests had no way to put proof in a report, and one
passed with `--no-require-screenshot` while its frames lived only in a doc.

This module does the capture from the OS side, with no editor plugin involved:

  * pick ONE top-level window by a selector -- a Dev Auth context (`Context_2`, read from the
    client's `-DevAuthToolName=` argument), a pid, or a window-title substring -- and REFUSE an
    ambiguous selector with the candidates listed, rather than guessing;
  * capture it with PrintWindow(PW_RENDERFULLCONTENT), which works while the window is
    unfocused, behind other windows, or the game is paused;
  * check the pixels are not black / a single flat colour, because a capture that silently
    returns an empty frame is exactly the "proof" this exists to replace;
  * stamp the image with its source: process, pid, exe, .uproject / project, client context,
    window title and the capture time. The report's pass gate reads the project from that
    stamp, the same way it reads the editor's project for an editor screenshot.

Everything that does not need Win32 (command-line parsing, selection, PNG encoding, blank
detection, frame difference) is plain Python so it is unit-testable on any machine.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# --- command-line parsing ---------------------------------------------------------------------

_CONTEXT_RE = re.compile(r"""-DevAuthToolName=(?:"([^"]*)"|'([^']*)'|(\S+))""", re.IGNORECASE)
_UPROJECT_RE = re.compile(r"""(?:"([^"]+?\.uproject)"|(\S+?\.uproject))""", re.IGNORECASE)
_LOG_RE = re.compile(r"""-ABSLOG=(?:"([^"]*)"|(\S+))""", re.IGNORECASE)
# A packaged UE build is <Project>.exe or <Project>-Win64-<Config>.exe under Binaries\Win64.
_PACKAGED_SUFFIX_RE = re.compile(r"-(Win64|Win32|Linux|Mac)(-\w+)?$", re.IGNORECASE)


def _first(m: re.Match | None) -> str | None:
    if not m:
        return None
    for g in m.groups():
        if g:
            return g
    return None


def _has_flag(cmdline: str, flag: str) -> bool:
    """Is `-flag` present as its own token? `-game` must not match `-gameusersettingsini=`."""
    return re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|$)", cmdline, re.IGNORECASE) is not None


def parse_cmdline(cmdline: str | None, exe: str | None = None) -> dict[str, Any]:
    """What a process command line says about the Unreal instance behind it.

    Returns uproject / project / context / game_client / log. `project` is the .uproject stem
    -- the same thing the editor stamps on an editor screenshot -- or, for a packaged build with
    no .uproject on its command line, the exe stem with the platform/config suffix removed.
    """
    cmdline = cmdline or ""
    uproject = _first(_UPROJECT_RE.search(cmdline))
    project = None
    if uproject:
        project = os.path.splitext(os.path.basename(uproject.replace("/", "\\").split("\\")[-1]))[0]
    elif exe:
        norm = exe.replace("/", "\\").lower()
        stem = os.path.splitext(os.path.basename(exe.replace("/", "\\").split("\\")[-1]))[0]
        if "\\binaries\\" in norm and not stem.lower().startswith("unrealeditor"):
            project = _PACKAGED_SUFFIX_RE.sub("", stem) or None
    return {
        "uproject": uproject,
        "project": project,
        "context": _first(_CONTEXT_RE.search(cmdline)),
        "game_client": _has_flag(cmdline, "-game"),
        "log": _first(_LOG_RE.search(cmdline)),
    }


# --- candidates + selection -------------------------------------------------------------------

@dataclass
class Candidate:
    """One capturable top-level window, and the process that owns it."""
    hwnd: int
    pid: int
    title: str
    width: int = 0
    height: int = 0
    iconic: bool = False
    exe: str | None = None
    cmdline: str | None = None
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def is_unreal(self) -> bool:
        exe = (self.exe or "").lower()
        return bool(self.info.get("uproject") or self.info.get("project")
                    or os.path.basename(exe.replace("/", "\\").split("\\")[-1]).startswith("unrealeditor"))

    def summary(self) -> dict[str, Any]:
        return {
            "pid": self.pid, "hwnd": self.hwnd, "title": self.title,
            "context": self.info.get("context"), "project": self.info.get("project"),
            "game_client": bool(self.info.get("game_client")),
            "exe": os.path.basename((self.exe or "").replace("/", "\\").split("\\")[-1]) or None,
            "size": [self.width, self.height], "minimized": self.iconic,
        }


class SelectError(Exception):
    """A selector matched no window, or more than one. Carries the candidates to list."""

    def __init__(self, message: str, candidates: list[Candidate]):
        super().__init__(message)
        self.candidates = candidates


def _one_per_pid(cands: list[Candidate]) -> list[Candidate]:
    """A process can own several top-level windows (splash, console, the game window). Keep
    its LARGEST, which is the game viewport; ambiguity is only meaningful between processes."""
    best: dict[int, Candidate] = {}
    for c in cands:
        cur = best.get(c.pid)
        if cur is None or (c.width * c.height) > (cur.width * cur.height):
            best[c.pid] = c
    return list(best.values())


def select(cands: list[Candidate], selector: str) -> tuple[Candidate, str]:
    """Resolve a --window selector to exactly one window. Returns (candidate, matched_by).

    Order, most specific first:
      1. `pid:<n>` or a bare integer        -> that process
      2. a Dev Auth context (`Context_2`)   -> exact, case-insensitive, on -DevAuthToolName=
      3. anything else                      -> case-insensitive window-title substring
    Zero or several matches raise SelectError listing the candidates -- never a guess, because
    a shot of the wrong client is a plausible image of the wrong thing.
    """
    sel = (selector or "").strip()
    if not sel:
        raise SelectError("empty --window selector", _listable(cands))
    per_pid = _one_per_pid(cands)

    m = re.fullmatch(r"(?:pid:)?(\d+)", sel, re.IGNORECASE)
    if m:
        pid = int(m.group(1))
        hits = [c for c in per_pid if c.pid == pid]
        if hits:
            return hits[0], "pid"
        raise SelectError(f"no visible top-level window belongs to pid {pid}", _listable(cands))

    ctx_hits = [c for c in per_pid
                if (c.info.get("context") or "").lower() == sel.lower()]
    if len(ctx_hits) == 1:
        return ctx_hits[0], "context"
    if len(ctx_hits) > 1:
        raise SelectError(
            f"--window {sel!r} matches {len(ctx_hits)} processes launched with "
            f"-DevAuthToolName={sel}; pass --window pid:<n> to pick one", ctx_hits)

    title_hits = [c for c in per_pid if sel.lower() in (c.title or "").lower()]
    if len(title_hits) == 1:
        return title_hits[0], "title"
    if len(title_hits) > 1:
        raise SelectError(
            f"--window {sel!r} is ambiguous: it is a substring of {len(title_hits)} window "
            f"titles. Use a client context (e.g. Context_2) or pid:<n>", title_hits)
    raise SelectError(f"--window {sel!r} matched no client context, pid or window title",
                      _listable(cands))


def _listable(cands: list[Candidate]) -> list[Candidate]:
    """What to show when nothing matched: Unreal windows if there are any, else everything."""
    per_pid = _one_per_pid(cands)
    unreal = [c for c in per_pid if c.is_unreal]
    return unreal or per_pid


# --- stamp ------------------------------------------------------------------------------------

def make_stamp(c: Candidate, *, matched_by: str, selector: str, width: int, height: int,
               frame: int = 1, frames: int = 1, offset_ms: int = 0,
               captured_at: datetime | None = None) -> dict[str, Any]:
    """Where an image came from. Rendered under the image in the report, and its `project`
    becomes the shot's provenance for the pass gate."""
    return {
        "kind": "window",
        "pid": c.pid,
        "hwnd": c.hwnd,
        "exe": c.exe,
        "title": c.title,
        "uproject": c.info.get("uproject"),
        "project": c.info.get("project"),
        "context": c.info.get("context"),
        "game_client": bool(c.info.get("game_client")),
        "log": c.info.get("log"),
        "selector": selector,
        "matched_by": matched_by,
        "size": [int(width), int(height)],
        "frame": int(frame),
        "frames": int(frames),
        "offset_ms": int(offset_ms),
        "captured_at": (captured_at or datetime.now(timezone.utc).astimezone()).isoformat(timespec="milliseconds"),
    }


def describe_source(src: dict[str, Any] | None) -> str:
    """One human line for a report caption: which client / process an image came from."""
    if not src:
        return ""
    if src.get("kind") == "editor":
        return f"editor | {src.get('project') or '?'}"
    who = (f"client {src['context']}" if src.get("context")
           else ("game client" if src.get("game_client") else "window"))
    exe = os.path.basename((src.get("exe") or "").replace("/", "\\").split("\\")[-1])
    parts = [who, f"pid {src.get('pid')}", src.get("project") or "project unknown"]
    if exe:
        parts.append(exe + (" -game" if src.get("game_client") else ""))
    if (src.get("frames") or 1) > 1:
        parts.append(f"frame {src.get('frame')}/{src.get('frames')} (+{src.get('offset_ms')} ms)")
    if src.get("captured_at"):
        parts.append(str(src["captured_at"]).replace("T", " "))
    return " | ".join(parts)


# --- pixels -----------------------------------------------------------------------------------

def bgra_to_png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode a top-down 32-bit BGRA buffer as an RGB PNG. Stdlib only (zlib + struct)."""
    stride = width * 4
    if len(bgra) < stride * height:
        raise ValueError(f"buffer too small: {len(bgra)} < {stride * height}")
    raw = bytearray()
    for y in range(height):
        row = bgra[y * stride:(y + 1) * stride]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        raw.append(0)       # filter: none
        raw += rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def _sample_offsets(width: int, height: int, target: int = 40000) -> list[int]:
    """Byte offsets of an even grid of ~`target` pixels -- enough to judge a frame, cheap."""
    n = max(1, width * height)
    step = max(1, int((n / target) ** 0.5))
    return [(y * width + x) * 4 for y in range(0, height, step) for x in range(0, width, step)]


# A pixel this dark in every channel counts as black. A real UE frame almost never has more
# than a sliver of pure black; an empty PrintWindow (minimised, DWM not composing, wrong
# swapchain) is ~100% of it.
_BLACK_MAX = 8
_BLACK_FRACTION = 0.995


def image_stats(width: int, height: int, bgra: bytes) -> dict[str, Any]:
    """Is this frame blank? `blank` is True for an all-black frame or a single flat colour."""
    if width <= 0 or height <= 0:
        return {"blank": True, "reason": f"empty image ({width}x{height})",
                "dark_fraction": 1.0, "distinct_colors": 0, "mean_luma": 0.0}
    offs = _sample_offsets(width, height)
    dark = 0
    luma_sum = 0.0
    colors: set[tuple[int, int, int]] = set()
    for o in offs:
        b, g, r = bgra[o], bgra[o + 1], bgra[o + 2]
        if r <= _BLACK_MAX and g <= _BLACK_MAX and b <= _BLACK_MAX:
            dark += 1
        luma_sum += 0.2126 * r + 0.7152 * g + 0.0722 * b
        if len(colors) < 64:
            colors.add((r, g, b))
    n = len(offs)
    stats: dict[str, Any] = {
        "dark_fraction": round(dark / n, 4),
        "distinct_colors": len(colors),          # capped at 64 -- only "1" matters
        "mean_luma": round(luma_sum / n, 1),
        "sampled": n,
        "blank": False,
    }
    if stats["dark_fraction"] >= _BLACK_FRACTION:
        stats["blank"] = True
        stats["reason"] = (f"image is black ({stats['dark_fraction']:.1%} of sampled pixels "
                           f"<= {_BLACK_MAX} in every channel)")
    elif len(colors) == 1:
        stats["blank"] = True
        stats["reason"] = f"image is a single flat colour rgb{next(iter(colors))}"
    return stats


def frame_difference(width: int, height: int, a: bytes, b: bytes, *, tol: int = 12) -> float:
    """Fraction of sampled pixels whose colour moved by more than `tol` in any channel.
    0.0 = identical frames. Used to say whether a burst actually shows motion."""
    offs = _sample_offsets(width, height)
    if not offs or len(a) != len(b):
        return 1.0
    changed = 0
    for o in offs:
        if (abs(a[o] - b[o]) > tol or abs(a[o + 1] - b[o + 1]) > tol
                or abs(a[o + 2] - b[o + 2]) > tol):
            changed += 1
    return round(changed / len(offs), 4)


# --- Win32 ------------------------------------------------------------------------------------

class CaptureError(Exception):
    pass


def _require_windows() -> None:
    if sys.platform != "win32":
        raise CaptureError("window capture needs Windows (PrintWindow)")


def _process_info(pid: int) -> tuple[str | None, str | None]:
    """(exe path, command line) for a pid, via the documented-in-practice
    NtQueryInformationProcess(ProcessCommandLineInformation) -- no WMI, no PowerShell, ~0 ms.
    Either half is None when the process cannot be opened (another user, protected)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None, None
    exe = cmdline = None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            exe = buf.value

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                        ("Buffer", ctypes.c_void_p)]

        ProcessCommandLineInformation = 60
        need = ctypes.c_ulong(0)
        ntdll.NtQueryInformationProcess(h, ProcessCommandLineInformation, None, 0,
                                        ctypes.byref(need))
        if need.value:
            raw = ctypes.create_string_buffer(need.value)
            st = ntdll.NtQueryInformationProcess(h, ProcessCommandLineInformation, raw,
                                                 need.value, ctypes.byref(need))
            if st == 0:
                us = UNICODE_STRING.from_buffer(raw)
                if us.Buffer and us.Length:
                    cmdline = ctypes.wstring_at(us.Buffer, us.Length // 2)
    finally:
        k32.CloseHandle(h)
    return exe, cmdline


def _user32():
    import ctypes
    from ctypes import wintypes

    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u32.GetWindowDC.argtypes = [wintypes.HWND]
    u32.GetWindowDC.restype = wintypes.HDC
    u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    u32.IsWindow.argtypes = [wintypes.HWND]
    return u32


def _per_monitor_dpi():
    """Make THIS thread per-monitor DPI aware for the capture, and return the old context.
    Without it, a DPI-unaware Python sees a DPI-aware game window's client rect in scaled
    logical units, and PrintWindow fills a bitmap too small for it -- a cropped frame."""
    import ctypes

    try:
        u32 = ctypes.WinDLL("user32")
        u32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        return u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        return None


def list_candidates() -> list[Candidate]:
    """Every visible, titled, top-level window on the desktop, with its process info."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    _per_monitor_dpi()
    u32 = _user32()
    found: list[Candidate] = []
    proc_cache: dict[int, tuple[str | None, str | None]] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        tb = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, tb, n + 1)
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rc = wintypes.RECT()
        u32.GetClientRect(hwnd, ctypes.byref(rc))
        h = int(hwnd or 0)
        found.append(Candidate(hwnd=h, pid=int(pid.value), title=tb.value,
                               width=rc.right - rc.left, height=rc.bottom - rc.top,
                               iconic=bool(u32.IsIconic(hwnd))))
        return True

    u32.EnumWindows(_cb, 0)
    me = os.getpid()
    out = []
    for c in found:
        if c.pid == me:
            continue
        if c.pid not in proc_cache:
            proc_cache[c.pid] = _process_info(c.pid)
        c.exe, c.cmdline = proc_cache[c.pid]
        c.info = parse_cmdline(c.cmdline, c.exe)
        out.append(c)
    return out


def capture_bgra(hwnd: int) -> tuple[int, int, bytes]:
    """PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT) the client area of `hwnd` into a
    top-down 32-bit BGRA buffer. PW_RENDERFULLCONTENT is what makes a DirectX window (the UE
    viewport) come out with pixels at all, and it does not need the window in front."""
    _require_windows()
    import ctypes
    from ctypes import wintypes

    _per_monitor_dpi()
    u32 = _user32()
    g32 = ctypes.WinDLL("gdi32", use_last_error=True)
    g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g32.CreateCompatibleDC.restype = wintypes.HDC
    g32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g32.SelectObject.restype = wintypes.HGDIOBJ
    g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g32.DeleteDC.argtypes = [wintypes.HDC]
    g32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

    if not u32.IsWindow(hwnd):
        raise CaptureError(f"window {hwnd} no longer exists (client closed?)")
    if u32.IsIconic(hwnd):
        raise CaptureError("window is MINIMISED, so it has no frame to capture. Restore it "
                           "(it does not need focus or to be in front) and capture again.")
    rc = wintypes.RECT()
    u32.GetClientRect(hwnd, ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        raise CaptureError(f"window client area is empty ({w}x{h})")

    wdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(wdc)
    bmp = g32.CreateCompatibleBitmap(wdc, w, h)
    old = g32.SelectObject(mdc, bmp)
    try:
        PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
        if not u32.PrintWindow(hwnd, mdc, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            raise CaptureError(f"PrintWindow failed (win32 error {ctypes.get_last_error()})")

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth, bih.biHeight = w, -h          # negative height = top-down rows
        bih.biPlanes, bih.biBitCount, bih.biCompression = 1, 32, 0   # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        g32.SelectObject(mdc, old)                  # a bitmap must be deselected for GetDIBits
        old = None
        lines = g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bih), 0)
        if lines != h:
            raise CaptureError(f"GetDIBits returned {lines} of {h} rows")
        return w, h, buf.raw
    finally:
        if old is not None:
            g32.SelectObject(mdc, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(hwnd, wdc)
