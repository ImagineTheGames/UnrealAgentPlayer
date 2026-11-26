"""`input axis` must deliver exactly X on the first frame, not 2X [17tm4679azq].

InputAxis ACCUMULATES within a frame, and the hold ticker (UAPHoldTick) re-injects the held
value every frame. HoldAxis used to also inject once itself, so the first frame carried the
value twice (0.58 arrived as 1.16, enough to read a grip axis as a press). The plugin has no
C++ test harness, so this pins the structure: HoldAxis must not inject directly; the ticker
is the only injection path for a latched axis.
"""

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "Plugin" / "Source" / "UnrealAgentPlayerRuntime"
       / "Private" / "AgentInput.cpp").read_text(encoding="utf-8")


def _body(signature: str) -> str:
    start = SRC.index(signature)
    brace = SRC.index("{", start)
    depth, i = 0, brace
    while True:
        depth += SRC[i] == "{"
        depth -= SRC[i] == "}"
        i += 1
        if depth == 0:
            return SRC[brace:i]


def test_hold_axis_does_not_inject_itself():
    body = _body("bool FAgentInput::HoldAxis(")
    assert not re.search(r"\bInjectAxis(Key|Slate)\s*\(", body), (
        "HoldAxis injects directly AND the hold ticker re-injects the same frame: "
        "InputAxis accumulates, so the first frame delivers 2x the value")


def test_hold_ticker_is_the_injection_path():
    body = _body("bool UAPHoldTick(")
    assert "UAPHoldInject(H, H.Value)" in body
