"""Tick callbacks registered through `uap exec` must not outlive their job [ClickUp 17tm4675e8t].

On 2026-11-18 an agent's `uap exec` script registered a slate post-tick callback. PIE ended, the
callback kept firing every frame, its except-handler read an exec-scope global that no longer
existed, and it wrote 2.4 M `NameError` lines into the editor log in 1 h 43 min.

Remote exec runs Python inside the editor, so the fix lives there too: every `uap exec` preamble
installs (once per editor process) a guard that wraps `unreal.register_slate_pre/post_tick_callback`.
A guarded callback

  * unregisters itself the first time it raises, and logs ONE warning with the traceback tail;
  * unregisters itself once PIE ends, if PIE was running when it was registered (a PIE-scoped
    probe has nothing left to do afterwards);
  * is tracked, so `_uap_cb_guard.release_all()` drops every one still live.

Handlers must still not read exec-scope globals: the namespace is rebuilt per exec. The guard
only bounds the damage when they do. The text avoids a literal dot-p-y, which UE's ExecuteFile
mode would read as a script path.
"""

from __future__ import annotations

GUARD_SOURCE = r'''
def _uap_guard_install(u):
    # Self-contained: the preamble cleanup deletes every `_uap_*` global after each exec, so the
    # installed closures must hold everything they use themselves.
    import sys
    import traceback
    import types
    m = sys.modules.get("_uap_cb_guard")
    if m is not None and getattr(m, "VERSION", 0) >= 1:
        return m

    def pie_live():
        try:
            return u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world() is not None
        except Exception:
            return False

    m = types.ModuleType("_uap_cb_guard")
    m.VERSION = 1
    m.live = {}
    m.log = []
    m.seq = [0]

    def drop(rec, why):
        if rec["dead"]:
            return
        rec["dead"] = True
        m.live.pop(rec["id"], None)
        un = getattr(u, rec["unreg"], None)
        try:
            if un is not None and rec["handle"] is not None:
                un(rec["handle"])
        except Exception:
            pass
        line = "uap: tick callback %s unregistered: %s" % (rec["name"], why)
        m.log.append(line)
        try:
            u.log_warning(line)
        except Exception:
            pass

    def wrap(unreg_name, orig):
        def register(fn, *a, **k):
            m.seq[0] += 1
            rec = {"id": m.seq[0], "name": getattr(fn, "__name__", repr(fn)), "dead": False,
                   "handle": None, "unreg": unreg_name, "pie": pie_live()}

            def guarded(*args, **kw):
                if rec["dead"]:
                    return None
                if rec["pie"] and not pie_live():
                    drop(rec, "PIE ended")
                    return None
                try:
                    return fn(*args, **kw)
                except BaseException:
                    drop(rec, "raised, first error: " + traceback.format_exc().strip().splitlines()[-1])
                    return None
            rec["handle"] = orig(guarded, *a, **k)
            m.live[rec["id"]] = rec
            return rec["handle"]
        register._uap_guarded = True
        return register

    for reg_name, unreg_name in (
            ("register_slate_post_tick_callback", "unregister_slate_post_tick_callback"),
            ("register_slate_pre_tick_callback", "unregister_slate_pre_tick_callback")):
        orig = getattr(u, reg_name, None)
        if orig is not None and not getattr(orig, "_uap_guarded", False):
            setattr(u, reg_name, wrap(unreg_name, orig))

    def release_all():
        for rec in list(m.live.values()):
            drop(rec, "released")
    m.release_all = release_all
    sys.modules["_uap_cb_guard"] = m
    return m


_uap_guard_install(_uap_u)
'''
