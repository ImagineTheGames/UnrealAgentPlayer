"""The machine-wide foreground lock, for the MCP server's game-starting tools.

The CLI takes the lock in `cli._run_under_machine_lock`. The MCP server is a separate entry point
with no guard path of its own, so its two tools that put a game window up -- `pie_start` and
`game_launch` -- take it here, with the same rules: a sticky hold (the session outlives the call),
fail CLOSED when the lock cannot be read, and no waiting (an MCP tool call must not block for the
CLI's 15-minute cap; the caller is told who holds the machine instead).
"""
from __future__ import annotations

import os
from typing import Any

from unreal_agent_player import coordination as _coord
from unreal_agent_player.errors import ErrorCode, error_response


def machine_project() -> str:
    """Which project this server drives: $UAP_PROJECT, else the stem of $UAP_UPROJECT."""
    proj = os.environ.get("UAP_PROJECT", "").strip()
    if proj:
        return proj
    upro = os.environ.get("UAP_UPROJECT", "").strip()
    return os.path.splitext(os.path.basename(upro.replace("\\", "/")))[0] if upro else ""


def take_sticky(reason: str) -> tuple[dict | None, dict[str, Any] | None]:
    """(grant, refusal). Exactly one is set; a disabled lock returns (None, None)."""
    if not _coord.machine_lock_enabled():
        return None, None
    project = machine_project()
    try:
        grant = _coord.acquire_machine(project, reason=reason, agent=_coord.default_agent_id(),
                                       pid=0, wait=0, ttl=_coord.MACHINE_TTL,
                                       task=_coord.default_task())
    except Exception as exc:                 # fail closed, as the CLI does
        grant = {"busy": True, "coordination_unsafe": True,
                 "error": f"{type(exc).__name__}: {exc}"}
    if not grant.get("busy"):
        return grant, None
    if grant.get("blocked_by"):
        msg = (f"the machine-wide foreground lock is held by {grant.get('blocked_by')!r} "
               f"(project {grant.get('project')!r}, reason {grant.get('reason')!r}). One "
               "workstation runs one game at a time; nothing was started. "
               "`uap lease machine-status` shows the holder.")
    else:
        msg = (f"the machine-wide foreground lock could not be read ({grant.get('error', '')}), "
               "so nothing was started. Retry; $UAP_MACHINE_LOCK=0 disables the lock.")
    return None, error_response(ErrorCode.MACHINE_BUSY, msg, recoverable=True,
                                retry_hint="wait for the other project, then retry")


def give_back(grant: dict | None) -> None:
    """Undo a hold this call took, when what it was for did not start. Never raises."""
    if not grant or not grant.get("acquired"):
        return
    try:
        _coord.release_machine(agent=grant.get("agent"), project=machine_project(),
                               project_wide=False)
    except Exception:
        pass


def release_project() -> None:
    """A confirmed stop: the project's session is over, so its hold goes. Never raises."""
    if not _coord.machine_lock_enabled():
        return
    try:
        _coord.release_machine(agent=_coord.default_agent_id(), project=machine_project())
    except Exception:
        pass
