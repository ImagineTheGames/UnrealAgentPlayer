"""Editor-side probe for `uap actor teleport`.

Teleports ANY actor in a PIE world (the server world by default) with K2_TeleportTo, the same
plain gameplay call `uap pawn teleport` uses. NO property is written, so no PostEditChange /
RerunConstructionScripts fires on the PIE actor (the failure the exec PIE-write guard exists for,
CRASH-0BD72FF1). Afterwards a pawn's movement is stopped (movement component velocity, and the
controller's current move / AI path), so an AI does not resume a path from the old floor.
Refuses when the world is not a PIE one. Every move is logged to the UE log (LogTemp).

The script prints one marker line of JSON (xr_reach.MARK); nothing in it may contain the text of a
python file extension (UE's remote exec reads the first one as a script path).
"""

from __future__ import annotations

from unreal_agent_player.xr_reach import _COMMON, _lit

_TELEPORT = r'''
_spec = "__ACTOR__"
_dx, _dy, _dz = __X__, __Y__, __Z__
_yv = [__YAW__]
_all = __ALL__
if "UEDPIE" not in _w.get_outer().get_name():
    _emit({"ok": False, "error": "refusing: world %s is not a PIE world (start PIE first)" % _w.get_outer().get_name()})
else:
    _want = _spec.lower()
    _exact, _part, _cls = [], [], []
    for _a in _u.GameplayStatics.get_all_actors_of_class(_w, _u.Actor):
        _names = [_a.get_name().lower()]
        try:
            _names.append(_a.get_actor_label().lower())
        except Exception:
            pass
        _cn = _cname(_a).lower()
        if _want in _names:
            _exact.append(_a)
        elif _want == _cn or (_cn.endswith("_c") and _want == _cn[:-2]):
            _cls.append(_a)
        elif any(_want in _n for _n in _names):
            _part.append(_a)
    _hits = _exact or _cls or _part
    if not _hits:
        _emit({"ok": False, "error": "no actor matches %r (name, label or class) in world %s" % (_spec, _w.get_name())})
    elif len(_hits) > 1 and not _all:
        _emit({"ok": False, "error": "actor %r matched %d actors (pass --all to move every one): %s"
               % (_spec, len(_hits), [_h.get_name() for _h in _hits][:8])})
    else:
        _res = []
        for _act in _hits:
            _from = _act.get_actor_location()
            _dest = _u.Vector(_from.x if _dx is None else _dx, _from.y if _dy is None else _dy,
                              _from.z if _dz is None else _dz)
            _yaw = _act.get_actor_rotation().yaw if _yv[0] is None else _yv[0]
            _rot = _act.get_actor_rotation()
            _ok = _act.teleport(_dest, _u.Rotator(_rot.roll, _rot.pitch, _yaw))
            _stopped = []
            if _ok:
                try:
                    _ctl = _act.get_controller()
                except Exception:
                    _ctl = None
                if _ctl is not None:
                    try:
                        _ctl.stop_movement()
                        _stopped.append("controller.stop_movement")
                    except Exception:
                        pass
                try:
                    _mc = _act.get_component_by_class(_u.MovementComponent)
                except Exception:
                    _mc = None
                if _mc is not None:
                    try:
                        _mc.stop_movement_immediately()
                        _stopped.append("stop_movement_immediately")
                    except Exception:
                        pass
            _to = _act.get_actor_location()
            _u.log("uap actor: %s %s -> %s yaw %.1f ok=%s world=%s stopped=%s" % (
                _act.get_name(), _v(_from), _v(_to), _yaw, _ok, _w.get_name(), _stopped))
            _res.append({"actor": _act.get_name(), "class": _cname(_act), "from": _v(_from), "to": _v(_to),
                         "ok": bool(_ok), "stopped": _stopped})
        _bad = [_r["actor"] for _r in _res if not _r["ok"]]
        _emit({"ok": not _bad, "world": _w.get_name(), "moved": _res, "to": _res[0]["to"], "from": _res[0]["from"],
               "yaw": _yv[0],
               "error": None if not _bad else "K2_TeleportTo returned false (blocked at the destination?): %s" % _bad})
'''


def _num(v: float | None) -> str:
    return "None" if v is None else repr(float(v))


def teleport_code(actor: str, x: float | None = None, y: float | None = None, z: float | None = None,
                  yaw: float | None = None, move_all: bool = False) -> str:
    # The actor spec is substituted LAST so text inside it is never rewritten.
    return (_COMMON + _TELEPORT.replace("__ALL__", repr(bool(move_all)))
            .replace("__X__", _num(x)).replace("__Y__", _num(y)).replace("__Z__", _num(z))
            .replace("__YAW__", _num(yaw)).replace("__ACTOR__", _lit(actor)))
