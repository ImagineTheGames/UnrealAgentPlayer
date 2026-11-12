"""Parse a timed input script for `uap input sequence` [17tm466zqax].

The script is parsed HERE, on the CLI side, into plain steps; the engine plugin only ever sees
JSON (`RunInputSequence`) and plays it against world time. Keeping the grammar out of C++ means a
new spelling never needs a plugin rebuild.

Grammar (statements separated by `;` or a newline; keywords case-insensitive, key names exact FKeys):

    hold  <Key> <A>-<B>[s]            hold  W 0-2.0s
    hold  <Key> at <A> for <D>        hold  W at 0 for 2
    hold  <Key> from <A> to <B>       hold  W from 0 to 2
    press <Key> at <T> [for <D>]      press SpaceBar at 0.85s          (D defaults to 0.1 s)
    axis  <Key> <V> <A>-<B>           axis  Gamepad_LeftY 1 0-2s
    axis  <Key> <V> at <A> for <D>    axis  OculusTouch_Left_Thumbstick_Y -1 at 0.5 for 1

Times are seconds after the script's 0 (`0.85`, `0.85s`) or milliseconds (`850ms`). A JSON script
({"steps":[{"kind","key","value","start","end"}]} or the bare list) is accepted too.
"""

from __future__ import annotations

import json
import re

DEFAULT_PRESS_SECONDS = 0.1

_TIME = r"(?:\d+(?:\.\d+)?|\.\d+)(?:ms|s)?"
_TIME_RE = re.compile(rf"^{_TIME}$", re.IGNORECASE)
_RANGE_RE = re.compile(rf"^({_TIME})-({_TIME})$", re.IGNORECASE)
_NUM_RE = re.compile(r"^-?(?:\d+(?:\.\d+)?|\.\d+)$")

_GRAMMAR = ("hold <Key> 0-2.0s | hold <Key> at 0 for 2 | press <Key> at 0.85s [for 0.1s] | "
            "axis <Key> <value> 0-2s")


def _seconds(token: str) -> float:
    t = token.lower()
    if t.endswith("ms"):
        return float(t[:-2]) / 1000.0
    t = t.removesuffix("s")
    return float(t)


def _time(token: str, what: str, stmt: str) -> float:
    if not _TIME_RE.match(token):
        raise ValueError(f"{what} {token!r} is not a time (like 0.85, 0.85s or 850ms) in: {stmt!r}")
    return _seconds(token)


def _parse_timing(rest: list[str], stmt: str, *, press: bool) -> tuple[float, float]:
    """The timing tail of one statement -> (start, end)."""
    if not rest:
        raise ValueError(f"no timing in: {stmt!r}. Forms: {_GRAMMAR}")
    if len(rest) == 1:
        m = _RANGE_RE.match(rest[0])
        if m:
            return _seconds(m.group(1)), _seconds(m.group(2))
        if press:   # `press SpaceBar 0.85s` -- a lone time is the press moment
            start = _time(rest[0], "time", stmt)
            return start, start + DEFAULT_PRESS_SECONDS
        raise ValueError(f"{rest[0]!r} is not a range like 0-2.0s in: {stmt!r}. Forms: {_GRAMMAR}")
    head = rest[0].lower()
    if head == "at":
        start = _time(rest[1], "start", stmt)
        tail = rest[2:]
        if not tail:
            if press:
                return start, start + DEFAULT_PRESS_SECONDS
            raise ValueError(f"`at {rest[1]}` needs `for <duration>` in: {stmt!r}")
        if len(tail) == 2 and tail[0].lower() == "for":
            return start, start + _time(tail[1], "duration", stmt)
        raise ValueError(f"unexpected {' '.join(tail)!r} after `at {rest[1]}` in: {stmt!r}")
    if head == "from" and len(rest) == 4 and rest[2].lower() == "to":
        return _time(rest[1], "start", stmt), _time(rest[3], "end", stmt)
    raise ValueError(f"cannot read the timing {' '.join(rest)!r} in: {stmt!r}. Forms: {_GRAMMAR}")


def parse_statement(stmt: str) -> dict:
    tokens = stmt.split()
    verb = tokens[0].lower()
    if verb not in ("hold", "press", "axis"):
        raise ValueError(f"unknown action {tokens[0]!r} in: {stmt!r} (hold / press / axis)")
    if len(tokens) < 2:
        raise ValueError(f"no key in: {stmt!r}")
    key = tokens[1]
    rest = tokens[2:]
    step: dict = {"kind": "hold", "key": key}
    if verb == "axis":
        if not rest or not _NUM_RE.match(rest[0]):
            raise ValueError(f"axis needs a value after the key (-1..1) in: {stmt!r}")
        step["kind"] = "axis"
        step["value"] = float(rest[0])
        rest = rest[1:]
    start, end = _parse_timing(rest, stmt, press=(verb == "press"))
    if start < 0 or end <= start:
        raise ValueError(f"needs 0 <= start < end (got {start}..{end}) in: {stmt!r}")
    step["start"] = round(start, 6)
    step["end"] = round(end, 6)
    return step


def _from_json(text: str) -> list[dict]:
    data = json.loads(text)
    steps = data.get("steps") if isinstance(data, dict) else data
    if not isinstance(steps, list) or not steps:
        raise ValueError("a JSON script is a list of steps, or {\"steps\": [...]}")
    out = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or "key" not in s or "start" not in s or "end" not in s:
            raise ValueError(f"JSON step {i} needs key, start and end")
        out.append({"kind": str(s.get("kind", "hold")).lower(), "key": str(s["key"]),
                    "value": float(s.get("value", 1.0)),
                    "start": float(s["start"]), "end": float(s["end"])})
    return out


def parse_script(text: str) -> list[dict]:
    """Script text -> list of {kind, key, [value], start, end}. Raises ValueError, naming the
    statement it could not read and the forms that are accepted."""
    body = (text or "").strip()
    if not body:
        raise ValueError(f"empty script. Forms: {_GRAMMAR}")
    if body[0] in "[{":
        return _from_json(body)
    steps = []
    for stmt in re.split(r"[;\n]", body):
        stmt = stmt.split("#", 1)[0].strip()
        if stmt:
            steps.append(parse_statement(stmt))
    if not steps:
        raise ValueError(f"no statements in the script. Forms: {_GRAMMAR}")
    return steps
