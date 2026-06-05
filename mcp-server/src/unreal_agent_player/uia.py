"""Windows UIAutomation driver for clicking Unreal Editor Slate menus.

Slate renders native top-level windows whose menu bar and pop-up menus are exposed
through the Windows UIAutomation tree. This driver finds the editor window by title
substring, then walks/expands menu items by name to reach and invoke a target.

Everything Windows/COM-specific is imported lazily inside __init__/methods and guarded,
so importing this module never fails on non-Windows or when `comtypes` is absent —
`available` is simply False and callers surface a UIA_UNAVAILABLE error.
"""

from __future__ import annotations

import sys
from typing import Any


class UIADriver:
    def __init__(self):
        self.available = False
        self._uia: Any = None
        self._UIA: Any = None
        if sys.platform != "win32":
            return
        try:
            import comtypes.client
            from comtypes.gen import UIAutomationClient as UIA

            self._uia = comtypes.client.CreateObject(
                "{ff48dba4-60ef-4201-aa87-54103eef594e}",
                interface=UIA.IUIAutomation,
            )
            self._UIA = UIA
            self.available = True
        except Exception:
            self.available = False

    # --- internal helpers ---

    def _find_window(self, window_title: str):
        """Return the first top-level window element whose name contains window_title."""
        root = self._uia.GetRootElement()
        UIA = self._UIA
        cond = self._uia.CreateTrueCondition()
        walker = self._uia.ControlViewWalker
        child = walker.GetFirstChildElement(root)
        while child:
            try:
                name = child.CurrentName or ""
            except Exception:
                name = ""
            if window_title.lower() in name.lower():
                return child
            child = walker.GetNextSiblingElement(child)
        return None

    def _find_descendant_by_name(self, element, name: str):
        UIA = self._UIA
        cond = self._uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, name)
        return element.FindFirst(UIA.TreeScope_Descendants, cond)

    def _invoke_or_expand(self, element) -> bool:
        UIA = self._UIA
        # Prefer ExpandCollapse (submenus); fall back to Invoke (leaf items).
        try:
            ec = element.GetCurrentPattern(UIA.UIA_ExpandCollapsePatternId)
            if ec:
                ec.QueryInterface(UIA.IUIAutomationExpandCollapsePattern).Expand()
                return True
        except Exception:
            pass
        try:
            inv = element.GetCurrentPattern(UIA.UIA_InvokePatternId)
            if inv:
                inv.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
                return True
        except Exception:
            pass
        return False

    # --- public API used by tools/ui.py ---

    def find_window(self, window_title: str) -> bool:
        if not self.available:
            return False
        return self._find_window(window_title) is not None

    def list_menus(self, window_title: str) -> list[str]:
        if not self.available:
            return []
        UIA = self._UIA
        win = self._find_window(window_title)
        if win is None:
            return []
        cond = self._uia.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_MenuItemControlTypeId
        )
        found = win.FindAll(UIA.TreeScope_Descendants, cond)
        names: list[str] = []
        for i in range(found.Length):
            el = found.GetElement(i)
            try:
                if el.CurrentName:
                    names.append(el.CurrentName)
            except Exception:
                continue
        return names

    def click_menu_path(self, window_title: str, path: list[str]) -> bool:
        if not self.available:
            return False
        win = self._find_window(window_title)
        if win is None:
            return False
        context = win
        for i, segment in enumerate(path):
            item = self._find_descendant_by_name(context, segment)
            if item is None:
                return False
            if not self._invoke_or_expand(item):
                return False
            context = item  # descend into the expanded submenu
        return True
