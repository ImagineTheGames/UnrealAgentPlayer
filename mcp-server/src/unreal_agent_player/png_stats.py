"""Is this PNG blank? Stdlib-only, so it works on any machine that can run the CLI [17tm466zztw].

`uap screenshot` in PIE used to hand back whatever the engine rendered. A frame that rendered as
solid black came back `ok:true, exists:true` and read as proof. The plugin's own PNG is the only
thing every plugin copy produces, so the CLI judges that file.

Cost control: a blank frame compresses to almost nothing (a black 1530x1354 PNG is ~40 KB, a real
frame 0.2-2 MB). So the decode only runs when the file is implausibly small for its pixel count;
anything bigger cannot be a flat image and is answered at once without touching a pixel.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

# Same thresholds as window_capture.image_stats so every capture path judges "blank" identically.
BLACK_MAX = 8
BLACK_FRACTION = 0.995
# A real rendered frame costs at least ~0.1 byte per pixel even when dark and smooth; a flat one
# costs ~0.02. Anything above this is not worth decoding.
MAYBE_BLANK_BYTES_PER_PIXEL = 0.08

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


def _chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        yield tag, data[pos + 8:pos + 8 + length]
        pos += 12 + length


def _paeth_row(row: bytearray, prev: bytearray | None, bpp: int) -> None:
    n = len(row)
    for i in range(n):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i] if prev is not None else 0
        c = prev[i - bpp] if (prev is not None and i >= bpp) else 0
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
        row[i] = (row[i] + pred) & 0xFF


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> list[bytearray]:
    stride = width * bpp
    rows: list[bytearray] = []
    prev: bytearray | None = None
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        row = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            if prev is not None:
                for i in range(stride):
                    row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i] if prev is not None else 0
                row[i] = (row[i] + ((a + b) >> 1)) & 0xFF
        elif ftype == 4:
            _paeth_row(row, prev, bpp)
        elif ftype != 0:
            raise ValueError(f"unknown PNG filter type {ftype}")
        rows.append(row)
        prev = row
    return rows


def png_blank_stats(path: str) -> dict[str, Any]:
    """{blank, reason?, dark_fraction?, distinct_colors?, decoded} for the PNG at `path`.

    Never raises: a file that cannot be judged answers `blank: None` + `reason` (unknown is NOT
    reported as fine OR as blank; the caller decides)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if not data.startswith(_PNG_SIG):
            return {"blank": None, "reason": "not a PNG", "decoded": False}
        ihdr = None
        idat = bytearray()
        for tag, body in _chunks(data):
            if tag == b"IHDR":
                ihdr = body
            elif tag == b"IDAT":
                idat += body
        if ihdr is None:
            return {"blank": None, "reason": "PNG has no IHDR", "decoded": False}
        width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", ihdr)
        if width <= 0 or height <= 0:
            return {"blank": True, "reason": f"empty image ({width}x{height})", "decoded": False}
        if depth != 8 or ctype not in _CHANNELS or interlace != 0:
            return {"blank": None, "decoded": False,
                    "reason": f"unsupported PNG layout (depth {depth}, colour type {ctype}, "
                              f"interlace {interlace}); not judged"}
        if len(data) > width * height * MAYBE_BLANK_BYTES_PER_PIXEL:
            return {"blank": False, "decoded": False,
                    "note": "file too large to be a flat frame; not decoded"}
        bpp = _CHANNELS[ctype]
        raw = zlib.decompress(bytes(idat))
        rows = _unfilter(raw, width, height, bpp)
    except (OSError, ValueError, zlib.error, struct.error, IndexError) as exc:
        return {"blank": None, "decoded": False, "reason": f"could not decode: {exc}"}

    step_y = max(1, height // 200)
    step_x = max(1, width // 200)
    total = dark = 0
    colors: set[tuple[int, ...]] = set()
    for y in range(0, height, step_y):
        row = rows[y]
        for x in range(0, width, step_x):
            o = x * bpp
            px = tuple(row[o:o + (bpp if bpp in (1, 3) else bpp - 1)])
            total += 1
            if all(v <= BLACK_MAX for v in px):
                dark += 1
            if len(colors) < 64:
                colors.add(px)
    frac = dark / total
    out: dict[str, Any] = {"blank": False, "decoded": True, "dark_fraction": round(frac, 4),
                           "distinct_colors": len(colors), "sampled": total}
    if frac >= BLACK_FRACTION:
        out["blank"] = True
        out["reason"] = (f"image is black ({frac:.1%} of sampled pixels <= {BLACK_MAX} in every "
                         "channel)")
    elif len(colors) == 1:
        out["blank"] = True
        out["reason"] = f"image is a single flat colour {next(iter(colors))}"
    return out
