from __future__ import annotations

import re
import time
from typing import Any

from unreal_agent_player.tools.pie import _LIVE_PHASES, _get_phase
from unreal_agent_player.transport import RemoteControlClient

# Net emulation with no PIE/game world asserts in UnrealEngine.cpp (HandleInvalidWorldContext <-
# NetEmulationHelper::ApplySimulationSettingsOnNetDrivers) and takes the shared editor down
# (2026-11-15 `NetEmulation.Off` after PIE stopped).
_NET_EMULATION_RE = re.compile(
    r"^\s*(netemulation\.|net\s+pkt|pkt(lag|loss|dup|order))",
    re.IGNORECASE)

NET_EMULATION_REFUSAL = ("refused: net emulation needs a live PIE world (crashes editor otherwise). "
                         "Start PIE first; the command was NOT run.")

# Same commands anywhere inside a blob of Python (`execute_console_command("NetEmulation.Off")`).
NET_EMULATION_SCAN_RE = re.compile(
    r"\b(netemulation\.\w+|net\s+pkt\w+|pkt(lag|loss|dup|order)\w*)", re.IGNORECASE)


# Travel commands also need a world (`open <url>` with a None world context hit
# UnrealEngine.cpp:16996 HandleInvalidWorldContext = check(false) and killed the shared editor,
# 2026-11-16), and with no game world there is nothing to travel.
_WORLD_COMMAND_RE = re.compile(
    r"^\s*(open|travel|servertravel|clienttravel)(\s|$)|^\s*(netemulation\.|net\s+pkt|pkt(lag|loss|dup|order))",
    re.IGNORECASE)

WORLD_COMMAND_REFUSAL = ("refused: this console command needs a live PIE world; with no world the "
                         "engine asserts (UnrealEngine.cpp:16996 HandleInvalidWorldContext) and kills "
                         "the editor. Start PIE first; the command was NOT run.")

# Python `execute_console_command(None, ...)` / `ExecuteConsoleCommand(None, ...)`: no world context.
NONE_WORLD_CALL_RE = re.compile(
    r"\b(?:execute_console_command|ExecuteConsoleCommand)\s*\(\s*"
    r"(?:(?:world_context_object|WorldContextObject)\s*=\s*)?(?:None|null)\b\s*(?:,\s*(?P<rest>[^\n]*))?",
    re.IGNORECASE)
_LITERAL_CMD_RE = re.compile(r"""^(?:command\s*=\s*)?(?P<q>['"])(?P<cmd>.*?)(?P=q)\s*[,)]""")

NONE_WORLD_REFUSAL = ("refusing: this Python calls execute_console_command with a None world context. "
                      "World-requiring commands (open, travel, NetEmulation.*) then assert in "
                      "UnrealEngine.cpp:16996 HandleInvalidWorldContext and kill the SHARED editor "
                      "(2026-11-15/16). Use `uap console <cmd>` (resolves the world, refuses without "
                      "PIE) or pass a real world object (UAP_WORLD). The script was NOT run.")


def is_net_emulation_command(command: str) -> bool:
    return bool(_NET_EMULATION_RE.match(command or ""))


def is_world_command(command: str) -> bool:
    return bool(_WORLD_COMMAND_RE.match(command or ""))


def none_world_console_call(code: str) -> bool:
    """True if CODE calls execute_console_command with a None world and the command is not a
    plain string literal of a command that needs no world (dynamic commands cannot be vetted)."""
    for m in NONE_WORLD_CALL_RE.finditer(code or ""):
        lit = _LITERAL_CMD_RE.match((m.group("rest") or "").strip())
        if not lit or is_world_command(lit.group("cmd")):
            return True
    return False


async def exec_console(
    *, rc: RemoteControlClient, py_exec: Any, command: str,
) -> dict[str, Any]:
    t0 = time.monotonic()
    if is_world_command(command) and await _get_phase(rc) not in _LIVE_PHASES:
        return {"ok": False, "error": WORLD_COMMAND_REFUSAL, "duration_ms": 0}
    output = await rc.exec_console(command)
    dt_ms = int((time.monotonic() - t0) * 1000)
    return {"ok": True, "output": output, "duration_ms": dt_ms}
