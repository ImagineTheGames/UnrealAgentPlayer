from __future__ import annotations

from typing import Any

from unreal_agent_player.errors import error_response, ok_response, ErrorCode


def _unavailable():
    return error_response(
        ErrorCode.UIA_UNAVAILABLE,
        "Windows UIAutomation driver unavailable (non-Windows or comtypes not installed).",
        recoverable=False,
        retry_hint="pip install 'unreal-agent-player[windows]' on a Windows host",
    )


async def ui_menu_click(*, driver, window_title: str, path: list[str]) -> dict[str, Any]:
    if not driver.available:
        return _unavailable()
    if driver.click_menu_path(window_title, path):
        return ok_response({"clicked": True, "path": path})
    return error_response(
        ErrorCode.UIA_PATH_NOT_FOUND,
        f"menu path {path} not found in window '{window_title}'",
        recoverable=True,
    )


async def ui_find_window(*, driver, window_title: str) -> dict[str, Any]:
    if not driver.available:
        return _unavailable()
    return ok_response({"found": bool(driver.find_window(window_title))})


async def ui_list_menus(*, driver, window_title: str) -> dict[str, Any]:
    if not driver.available:
        return _unavailable()
    return ok_response({"menus": driver.list_menus(window_title)})
