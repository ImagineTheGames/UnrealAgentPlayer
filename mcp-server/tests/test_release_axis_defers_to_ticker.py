"""A released analog hold must zero via the hold ticker, not by a same-frame inject [17tm4679bq9].

InputAxis ACCUMULATES within a frame. If the hold ticker already injected Value this frame, a
release that injects 0 itself sums to Value, drops the entry, and nothing injects again: the axis
stays latched (input sequence "axis ... 0-1.0s" left FreeGrip at 0.58). ReleaseHeld must expire
analog entries so UAPHoldTick sends the 0, and UAPHoldTick must release expired analog entries.
"""

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


def test_release_held_defers_analog_zero_to_ticker():
    body = _body("int32 FAgentInput::ReleaseHeld(")
    assert "bAnalog" in body
    assert "EndRealTime = 0.0" in body and "Value = 0.f" in body
    # the only UAPReleaseOne left in ReleaseHeld is the digital branch
    assert body.count("UAPReleaseOne(") == 1
    assert body.index("UAPReleaseOne(") > body.index("else")


def test_hold_ticker_zeroes_expired_analog():
    body = _body("bool UAPHoldTick(")
    assert "Now >= H.EndRealTime" in body
    assert "UAPReleaseOne(H)" in body
