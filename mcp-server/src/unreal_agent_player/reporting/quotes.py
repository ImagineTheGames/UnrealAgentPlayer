from __future__ import annotations

import random

QUOTES: list[str] = [
    "You shipped proof, not promises.",
    "Green tests are the best kind of green.",
    "Evidence over assertions, always.",
    "Another loop closed by your own hand.",
    "Slow is smooth, smooth is fast.",
    "The pawn moved because you made it move.",
    "Small, verified steps beat big, hopeful ones.",
    "Done is good. Proven-done is better.",
]


def pick_quote() -> str:
    return random.choice(QUOTES)
