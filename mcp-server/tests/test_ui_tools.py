import pytest

from unreal_agent_player.tools.ui import ui_find_window, ui_list_menus, ui_menu_click


class FakeDriver:
    def __init__(self, available=True, click_result=True, menus=None, window_found=True):
        self.available = available
        self._click_result = click_result
        self._menus = menus or []
        self._window_found = window_found
        self.calls = []

    def click_menu_path(self, window_title, path):
        self.calls.append((window_title, tuple(path)))
        return self._click_result

    def find_window(self, window_title):
        return self._window_found

    def list_menus(self, window_title):
        return self._menus


@pytest.mark.asyncio
async def test_ui_menu_click_uses_driver():
    drv = FakeDriver()
    result = await ui_menu_click(
        driver=drv, window_title="SchoolsOut",
        path=["Window", "Viewports", "Viewport 1"])
    assert result["ok"] is True
    assert result["clicked"] is True
    assert drv.calls == [("SchoolsOut", ("Window", "Viewports", "Viewport 1"))]


@pytest.mark.asyncio
async def test_ui_menu_click_path_not_found():
    drv = FakeDriver(click_result=False)
    result = await ui_menu_click(driver=drv, window_title="X", path=["Nope"])
    assert result["ok"] is False
    assert result["error"]["code"] == "UIA_PATH_NOT_FOUND"


@pytest.mark.asyncio
async def test_ui_menu_click_unavailable_driver_errors():
    drv = FakeDriver(available=False)
    result = await ui_menu_click(driver=drv, window_title="X", path=["File"])
    assert result["ok"] is False
    assert result["error"]["code"] == "UIA_UNAVAILABLE"
    assert drv.calls == []


@pytest.mark.asyncio
async def test_ui_find_window():
    drv = FakeDriver(window_found=True)
    result = await ui_find_window(driver=drv, window_title="SchoolsOut")
    assert result == {"ok": True, "found": True}


@pytest.mark.asyncio
async def test_ui_list_menus():
    drv = FakeDriver(menus=["File", "Edit", "Window"])
    result = await ui_list_menus(driver=drv, window_title="SchoolsOut")
    assert result["ok"] is True
    assert result["menus"] == ["File", "Edit", "Window"]


@pytest.mark.asyncio
async def test_ui_list_menus_unavailable():
    drv = FakeDriver(available=False)
    result = await ui_list_menus(driver=drv, window_title="X")
    assert result["error"]["code"] == "UIA_UNAVAILABLE"
