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
