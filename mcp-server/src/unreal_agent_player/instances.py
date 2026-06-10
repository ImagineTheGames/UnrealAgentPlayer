from __future__ import annotations
import socket
import threading
from typing import Any, Optional
from unreal_agent_player.errors import AgentError, ErrorCode


class InstanceRegistry:
    def __init__(self, editor_port: int = 30010):
        self._editor_port = editor_port
        self._by_id: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._counter = 0
        self._reserved: set[int] = set()

    def register(self, *, port: int, pid: Optional[int]) -> str:
        with self._lock:
            self._counter += 1
            iid = f"inst{self._counter}"
            self._by_id[iid] = {"instance_id": iid, "port": port, "pid": pid}
            self._reserved.add(port)
            return iid

    def remove(self, instance_id: str) -> None:
        with self._lock:
            info = self._by_id.pop(instance_id, None)
            if info:
                self._reserved.discard(info["port"])

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._by_id.values()]

    def get(self, instance_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            v = self._by_id.get(instance_id)
            return dict(v) if v else None

    def resolve_port(self, target: Optional[str]) -> int:
        if target is None or target == "editor":
            return self._editor_port
        with self._lock:
            info = self._by_id.get(target)
        if not info:
            raise AgentError(
                ErrorCode.INSTANCE_NOT_FOUND,
                f"Unknown target {target!r}. Use 'editor' or an instance_id.",
            )
        return info["port"]

    def next_free_port(self, base: int = 30100) -> int:
        with self._lock:
            port = base
            while port in self._reserved or _port_in_use(port):
                port += 1
            self._reserved.add(port)
            return port


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True
