"""Explain an implausibly low frame rate AT the number, not in a doc nobody re-reads.

The editor throttles to exactly 3.0 fps whenever it is NOT the foreground window. Agents
that meet that number while staring at a perf reading have repeatedly read it as a finding
about the GAME -- one of them filed a bug ticket off timing measured in a throttled editor.
Documentation did not reach them, because at that moment they are looking at a number.

So every uap surface that reports a frame rate (report diag, perf_stat, perf baselines, the
per-frame sampler's own hz) runs its numbers through here and, when they are implausible,
attaches the explanation to the OUTPUT ITSELF.

Threshold: 5.0 fps / 200 ms per frame. The throttle signature is exactly 3.0 fps, and the
band up to 5 covers the jitter around it (a 3.0 fps editor reads back 3.0-3.3, and a
partially-populated `stat unit` can round oddly) while staying far below anything a real
scene produces: even a badly blown Quest frame budget lands at 10-20 fps, and a desktop
editor that genuinely renders that slowly is hitching, not sitting flat. Nothing legitimate
lives between 0 and 5 fps, so a false positive here costs one re-measure and a false
negative costs a wrong conclusion.
"""

from __future__ import annotations

from typing import Any

# Below this, a frame rate is not a measurement of the game -- see the module docstring.
THROTTLED_FPS_MAX = 5.0
THROTTLED_FRAME_MS_MIN = 1000.0 / THROTTLED_FPS_MAX  # 200.0 ms
# What the editor's not-foreground throttle actually pins the rate to.
EDITOR_THROTTLE_FPS = 3.0

_TRAP_REF = "trap #1 in agent-testing/agentplayertest.md"

# The one paragraph an agent sees next to the number. Terse on purpose: it has to be read
# in the middle of doing something else.
THROTTLE_NOTE = (
    "This is the editor's not-foreground throttle (it pins ~3.0 fps when the editor is not "
    "the foreground window), NOT the game's performance -- focus the PIE window and "
    "re-measure, and treat any timing already taken as void. If focus cannot be taken "
    "(SetForegroundWindow returns False / GetForegroundWindow is 0; Slate.bAllowThrottling 0 "
    "does NOT lift it), report the timing as unmeasurable instead of using it -- but "
    "`uap sample` and `uap input hold`/`input axis` still measure correctly, because they run "
    "in-engine and need no CLI calls during the window. See " + _TRAP_REF + "."
)

# Same message where the evidence is the sampler's own rate rather than a perf reading.
SAMPLER_THROTTLE_NOTE = (
    "The sampler ran at roughly the editor's not-foreground throttle rate (~3 Hz), so these "
    "samples are ~333 ms apart: the per-frame deltas, hz and any smoothness/judder verdict "
    "from them are void. Focus the PIE window and re-sample. If focus cannot be taken "
    "(SetForegroundWindow returns False / GetForegroundWindow is 0; Slate.bAllowThrottling 0 "
    "does NOT lift it), report the timing as unmeasurable instead of using it. See "
    + _TRAP_REF + "."
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def is_throttled_rate(fps: Any = None, frame_ms: Any = None) -> bool:
    """True when a measured rate is implausibly low, i.e. the editor was throttled.

    Either input alone is enough; 0 / None / non-numeric means "no reading", not "throttled",
    so a missing stat never fabricates a warning.
    """
    f = _num(fps)
    if f is not None and 0.0 < f <= THROTTLED_FPS_MAX:
        return True
    ms = _num(frame_ms)
    return ms is not None and ms >= THROTTLED_FRAME_MS_MIN


def is_throttled(metrics: Any) -> bool:
    """True when a perf dict ({fps, frame_ms, ...}) carries a throttled reading."""
    if not isinstance(metrics, dict):
        return False
    return is_throttled_rate(fps=metrics.get("fps"), frame_ms=metrics.get("frame_ms"))


def throttle_annotation(metrics: Any) -> dict[str, Any]:
    """Fields to merge into a perf-reporting result: `{}` when the rate is plausible."""
    if not is_throttled(metrics):
        return {}
    return {"throttled": True, "warning": THROTTLE_NOTE}


def sampler_annotation(hz: Any) -> dict[str, Any]:
    """Fields to merge into a sampler result, keyed off the window's own measured hz."""
    if not is_throttled_rate(fps=hz):
        return {}
    return {"throttled": True, "warning": SAMPLER_THROTTLE_NOTE}
