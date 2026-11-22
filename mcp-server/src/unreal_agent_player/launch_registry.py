"""School's Out launch registry: every game client uap starts is recorded so it is always closed.

Schema and naming follow SchoolsOutVR/Tools/Ops/LaunchRegistry-Lib.ps1 (one JSON file per launch
in %USERPROFILE%/.sovr-ops/launched/, $SOVR_LAUNCH_REGISTRY overrides the directory). Optional and
harmless: Windows only, and every failure is swallowed -- a registry problem never breaks a launch.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path
from typing import Any

_NET_EPOCH_OFFSET = 504911232000000000   # .NET ticks (since 0001) at the FILETIME epoch (1601)


def registry_dir() -> Path:
    env = os.environ.get("SOVR_LAUNCH_REGISTRY")
    if env:
        return Path(env)
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / ".sovr-ops" / "launched"


def launch_owner(agent: str | None = None) -> tuple[str, str]:
    """(owner, ownerKind): $SOVR_JOB_ID (job), else $UAP_AGENT / --agent token (agent), else 'uap'."""
    job = os.environ.get("SOVR_JOB_ID")
    if job:
        return job, "job"
    for cand in (os.environ.get("UAP_AGENT"), agent, os.environ.get("UAP_AGENT_ID")):
        cand = (cand or "").strip()
        if cand and not cand.isdigit():
            return cand, "agent"
    return "uap", "agent"


def _pid_start_ticks(pid: int) -> int:
    """UTC .NET ticks of the process start time (matches the PowerShell pidStart); 0 if unknown."""
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return 0
        try:
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return 0
            return ((c.dwHighDateTime << 32) | c.dwLowDateTime) + _NET_EPOCH_OFFSET
        finally:
            k32.CloseHandle(h)
    except Exception:
        return 0


def register_launch(pid: int, agent: str | None = None, *, directory: Path | None = None,
                    force: bool = False) -> Path | None:
    """Write the registry entry for a PC game client. Returns the file, or None (never raises)."""
    try:
        if not pid or (os.name != "nt" and not force):
            return None
        d = Path(directory) if directory else registry_dir()
        d.mkdir(parents=True, exist_ok=True)
        owner, kind = launch_owner(agent)
        now = _dt.datetime.now(_dt.timezone.utc)
        entry_id = f"pc-{int(pid)}-{now.strftime('%Y%m%dT%H%M%S')}{now.microsecond // 1000:03d}"
        entry: dict[str, Any] = {
            "id": entry_id, "kind": "pc", "pid": int(pid), "pidStart": _pid_start_ticks(pid),
            "device": "", "package": "", "appId": "", "hostName": "",
            "launcher": "uap", "owner": owner, "ownerKind": kind, "ownerPid": os.getpid(),
            "started": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 10:07d}Z",
        }
        f = d / f"{entry_id}.json"
        f.write_text(json.dumps(entry, indent=4), encoding="utf-8")
        return f
    except Exception:
        return None


def unregister_launch(file: Path | None) -> None:
    try:
        if file:
            Path(file).unlink(missing_ok=True)
    except Exception:
        pass


def unregister_on_exit(proc: Any, file: Path | None) -> None:
    """Remove the entry when the process exits (daemon watcher; no-op without an entry)."""
    if not file or not hasattr(proc, "wait"):
        return

    def _watch() -> None:
        try:
            proc.wait()
        except Exception:
            return
        unregister_launch(file)

    threading.Thread(target=_watch, daemon=True, name="uap-launch-registry").start()
