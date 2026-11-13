"""A PIE screenshot that rendered all black must not come back as proof [17tm466zztw].

A late-joined / flat-pawn client viewport rendered black while `uap screenshot` answered
`ok:true, exists:true`: the blank-frame refusal only existed on the `--window` and level-viewport
paths. The PIE path now judges the PNG it just got.
"""

import os
import random

from unreal_agent_player import cli
from unreal_agent_player import window_capture as wc
from unreal_agent_player.png_stats import png_blank_stats


def _write(tmp_path, name, w, h, pixel):
    """A w x h PNG; pixel(x, y) -> (r, g, b)."""
    buf = bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b = pixel(x, y)
            buf += bytes((b, g, r, 255))
    path = tmp_path / name
    path.write_bytes(wc.bgra_to_png(w, h, bytes(buf)))
    return str(path)


def test_black_png_is_blank(tmp_path):
    p = _write(tmp_path, "black.png", 640, 480, lambda x, y: (0, 0, 0))
    st = png_blank_stats(p)
    assert st["blank"] is True and "black" in st["reason"]


def test_flat_colour_png_is_blank(tmp_path):
    p = _write(tmp_path, "flat.png", 640, 480, lambda x, y: (200, 190, 180))
    st = png_blank_stats(p)
    assert st["blank"] is True and "single flat colour" in st["reason"]


def test_a_real_looking_frame_is_not_blank(tmp_path):
    rnd = random.Random(7)
    p = _write(tmp_path, "scene.png", 320, 240,
               lambda x, y: (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    st = png_blank_stats(p)
    assert st["blank"] is False


def test_two_colour_frame_with_some_black_is_not_blank(tmp_path):
    """The real frames have a black ceiling band over a lit wall: that is NOT a blank frame."""
    p = _write(tmp_path, "band.png", 640, 480,
               lambda x, y: (0, 0, 0) if y < 100 else (250, 250, 240))
    st = png_blank_stats(p)
    assert st["blank"] is False


def test_unreadable_files_are_unknown_not_fine_and_not_blank(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    assert png_blank_stats(str(bad))["blank"] is None
    assert png_blank_stats(str(tmp_path / "missing.png"))["blank"] is None


def test_blank_pie_frame_is_refused_and_moved_aside(tmp_path):
    p = _write(tmp_path, "shot.png", 640, 480, lambda x, y: (0, 0, 0))
    body = cli._refuse_blank_frame({"ok": True, "exists": True, "path": p}, p, False)
    assert body["ok"] is False and body["exists"] is False and body["blank"] is True
    assert "BLANK frame refused" in body["error"]
    assert not os.path.exists(p), "the requested path must not hold an untrustworthy picture"
    assert os.path.exists(body["blank_file"]) and body["blank_file"].endswith(".BLANK.png")


def test_allow_blank_keeps_the_file_but_flags_it(tmp_path):
    p = _write(tmp_path, "shot.png", 640, 480, lambda x, y: (0, 0, 0))
    body = cli._refuse_blank_frame({"ok": True, "exists": True, "path": p}, p, True)
    assert body["ok"] is True and body["blank"] is True and "BLANK FRAME kept" in body["warning"]
    assert os.path.exists(p)


def test_a_good_frame_passes_through_untouched(tmp_path):
    rnd = random.Random(3)
    p = _write(tmp_path, "good.png", 320, 240,
               lambda x, y: (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    body = cli._refuse_blank_frame({"ok": True, "exists": True, "path": p}, p, False)
    assert body["ok"] is True and body["exists"] is True and body["blank"] is False
    assert os.path.exists(p)


def test_unjudgeable_frame_is_not_refused_but_says_so(tmp_path):
    bad = tmp_path / "x.png"
    bad.write_bytes(b"garbage")
    body = cli._refuse_blank_frame({"ok": True, "exists": True, "path": str(bad)}, str(bad), False)
    assert body["ok"] is True and body["blank"] is None
    assert "reason" in body["blank_check"]


def test_screenshot_flag_exists():
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        return
    args = parser.parse_args(["screenshot", "x.png", "--allow-blank"])
    assert args.allow_blank is True


# --- the uap overlay / toasts must not keep a black frame alive [17tm46700ke] -------------------

def _overlay(x, y, w=640, h=480):
    """Black scene + what uap draws into the frame: a text block top-right (sparse bright pixels
    in a ~25% x 20% region) and, optionally via _toast, a white bar near the bottom."""
    in_block = x > w * 0.5 and y < h * 0.20
    if in_block and (x % 7 < 3) and (y % 5 < 2):
        return (240, 240, 230)
    return None


def _black_with_overlay(x, y):
    return _overlay(x, y) or (0, 0, 0)


def _black_with_overlay_and_toast(x, y):
    if 640 * 0.07 < x < 640 * 0.93 and 480 * 0.86 < y < 480 * 0.92:
        return (255, 255, 255)
    return _overlay(x, y) or (0, 0, 0)


def test_black_frame_with_overlay_text_is_blank_via_cli(tmp_path):
    """The measured failure: 0.9947 black pixels, under the old 0.995 cut-off."""
    p = _write(tmp_path, "ov.png", 640, 480, _black_with_overlay)
    st = png_blank_stats(p)
    assert st["blank"] is True and st["dark_fraction"] < 0.995, st
    body = cli._refuse_blank_frame({"ok": True, "exists": True, "path": p}, p, False)
    assert body["ok"] is False and body["blank"] is True


def test_black_frame_with_overlay_and_toast_is_blank(tmp_path):
    p = _write(tmp_path, "ovt.png", 640, 480, _black_with_overlay_and_toast)
    st = png_blank_stats(p)
    assert st["blank"] is True and st["dark_fraction"] < 0.95, st


def test_black_frame_with_overlay_is_blank_in_window_capture_too():
    buf = bytearray()
    for y in range(480):
        for x in range(640):
            r, g, b = _black_with_overlay_and_toast(x, y)
            buf += bytes((b, g, r, 255))
    st = wc.image_stats(640, 480, bytes(buf))
    assert st["blank"] is True, st


def test_dark_but_real_night_scene_is_not_blank(tmp_path):
    """Dim ambient gradient + the same overlay: content everywhere, so ok."""

    def night(x, y):
        ov = _overlay(x, y)
        if ov:
            return ov
        v = 12 + int(30 * y / 480) + (x // 80)
        return (v // 2, v // 2, v)
    p = _write(tmp_path, "night.png", 640, 480, night)
    st = png_blank_stats(p)
    assert st["blank"] is False and st["live_cell_fraction"] > 0.9, st
    body = cli._refuse_blank_frame({"ok": True, "exists": True, "path": p}, p, False)
    assert body["ok"] is True and body["exists"] is True and body["blank"] is False


def test_dark_scene_with_a_black_half_is_not_blank(tmp_path):
    """A dim room: left half genuinely unlit, right half dim furniture. Still a real frame."""
    rnd = random.Random(5)
    p = _write(tmp_path, "half.png", 640, 480,
               lambda x, y: (0, 0, 0) if x < 320 else (20 + rnd.randrange(30),) * 3)
    assert png_blank_stats(p)["blank"] is False
