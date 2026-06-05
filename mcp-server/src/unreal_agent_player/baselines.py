from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class BaselineStore:
    """JSON-file store of named perf baselines: {name: {metric: value}}."""

    def __init__(self, path: Path):
        self._path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, name: str, metrics: dict[str, float]) -> None:
        data = self._read()
        data[name] = metrics
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self, name: str) -> dict[str, float] | None:
        return self._read().get(name)

    def list_names(self) -> list[str]:
        return list(self._read().keys())
