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
  * is tracked, so `_uap_cb_guard.release_all()` drops every one still live;
  * is tagged with its OWNER (the `--agent` token / $UAP_AGENT_ID of the exec that registered it,
    "" when tokenless), so `release_all(owner)` drops just that agent's callbacks. `uap lease
    release` and `uap pie stop` call it. GAP: a tokenless exec shares the owner "" with every other
    tokenless exec, so its callbacks are only released by a tokenless release/stop. Pass --agent.

Only the slate tick callbacks are wrapped. The UE Python API has no PIE delegate host
(FEditorDelegates is a plain C++ struct, not reflected; there is no `unreal.EditorDelegates`), so
there is nothing else to wrap. A tick callback registered during PIE is unregistered once PIE has
ended (checked when it fires and at the start of every later exec).

Handlers must still not read exec-scope globals: the namespace is rebuilt per exec, so capture
values via default args or closures. The guard only bounds the damage when they do, and its single
warning then names the missing global. The text avoids a literal dot-p-y, which UE's ExecuteFile
mode would read as a script path.
"""

from __future__ import annotations

GUARD_SOURCE = r'''
def _uap_guard_install(u, owner):
    # Self-contained: the preamble cleanup deletes every `_uap_*` global after each exec, so the
    # installed closures must hold everything they use themselves.
    import re
    import sys
    import traceback
    import types
    m = sys.modules.get("_uap_cb_guard")
    if m is not None and getattr(m, "VERSION", 0) >= 2:
        m.owner[0] = owner
        m.sweep()
        return m
    if m is not None:
        m.release_all()  # v1 guard: drop its callbacks and unwrap below
        for _n in ("register_slate_post_tick_callback", "register_slate_pre_tick_callback"):
            _f = getattr(u, _n, None)
            if getattr(_f, "_uap_orig", None) is not None:
                setattr(u, _n, _f._uap_orig)

    def pie_live():
        try:
            return u.get_editor_subsystem(u.UnrealEditorSubsystem).get_game_world() is not None
        except Exception:
            return False

    m = types.ModuleType("_uap_cb_guard")
    m.VERSION = 2
    m.live = {}
    m.log = []
    m.seq = [0]
    m.owner = [owner]

    def drop(rec, why):
        if rec["dead"]:
            return
        rec["dead"] = True
        m.live.pop(rec["id"], None)
        try:
            rec["undo"]()
        except Exception:
            pass
        line = "uap: %s callback %s unregistered: %s" % (rec["kind"], rec["name"], why)
        m.log.append(line)
        try:
            u.log_warning(line)
        except Exception:
            pass

    def why_raised(exc):
        last = traceback.format_exc().strip().splitlines()[-1]
        if isinstance(exc, NameError):
            gm = re.search(r"name '([^']+)' is not defined", str(exc))
            gname = getattr(exc, "name", None) or (gm.group(1) if gm else "?")
            return ("raised, first error: %s -- the handler read global '%s', which does not exist: "
                    "each uap exec rebuilds its namespace, so callbacks must capture values via "
                    "default args or closures (def cb(dt, %s=%s)), never read exec-scope globals"
                    % (last, gname, gname, gname))
        return "raised, first error: " + last

    def make_guarded(rec, fn):
        def guarded(*args, **kw):
            if rec["dead"]:
                return None
            if rec["pie"] and not pie_live():
                drop(rec, "PIE ended")
                return None
            try:
                return fn(*args, **kw)
            except BaseException as exc:
                drop(rec, why_raised(exc))
                return None
        return guarded

    def new_rec(kind, fn):
        m.seq[0] += 1
        return {"id": m.seq[0], "kind": kind, "name": getattr(fn, "__name__", repr(fn)),
                "dead": False, "undo": None, "pie": pie_live(), "owner": m.owner[0], "fn": fn}

    def wrap(unreg_name, orig):
        def register(fn, *a, **k):
            rec = new_rec("tick", fn)
            guarded = make_guarded(rec, fn)
            handle = orig(guarded, *a, **k)
            rec["undo"] = lambda: getattr(u, unreg_name)(handle)
            m.live[rec["id"]] = rec
            return handle
        register._uap_guarded = True
        register._uap_orig = orig
        return register

    for reg_name, unreg_name in (
            ("register_slate_post_tick_callback", "unregister_slate_post_tick_callback"),
            ("register_slate_pre_tick_callback", "unregister_slate_pre_tick_callback")):
        orig = getattr(u, reg_name, None)
        if orig is not None and not getattr(orig, "_uap_guarded", False):
            setattr(u, reg_name, wrap(unreg_name, orig))

    def release_all(owner=None):
        # owner None = everyone's; otherwise only callbacks registered by that exec owner.
        n = 0
        for rec in list(m.live.values()):
            if owner is None or rec["owner"] == owner:
                drop(rec, "released")
                n += 1
        return n

    def sweep():
        if not pie_live():
            for rec in list(m.live.values()):
                if rec["pie"]:
                    drop(rec, "PIE ended")

    m.release_all = release_all
    m.sweep = sweep
    sys.modules["_uap_cb_guard"] = m
    return m


_uap_guard_install(_uap_u, __UAP_OWNER__)
'''


def guard_source(owner: str = "") -> str:
    """GUARD_SOURCE with the exec owner (agent token) baked in as a Python literal."""
    return GUARD_SOURCE.replace("__UAP_OWNER__", repr(str(owner or "")))


def release_code(owner: str = "") -> str:
    """Editor-side snippet: drop every guarded callback `owner` registered. Prints a marker with
    the count; a no-op when the guard was never installed in this editor process."""
    return (
        "import sys as _s\n"
        "_m = _s.modules.get('_uap_cb_guard')\n"
        "print('@@UAP_CB_RELEASED@@ %d' % (_m.release_all(" + repr(str(owner or "")) + ") "
        "if _m is not None else 0))\n"
    )
