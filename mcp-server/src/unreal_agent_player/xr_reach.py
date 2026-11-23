"""Editor-side probes for `uap xr reach` and `uap screenshot --look-at`.

Both verbs need the live pawn's geometry (where the item is, where the hand's grab sphere is, how
the tracking space is placed in the world). That is a READ, so it runs as `uap exec` Python in the
client's PIE world (UAP_WORLD); the pose itself goes out through the normal `xr pose` RC call.

`xr pose` is TRACKING-space and the hand mesh / grab sphere sit at an offset from the controller
(plus a measured, unexplained +8 cm on x in flat PIE), so guessing the pose misses small items. The
verb is therefore CLOSED LOOP: pose, read the grab sphere back, move the pose by the remaining error.

The scripts print one marker line holding JSON; nothing in them may contain the text of a python
file extension (UE's remote exec reads the first one as a script path).
"""

from __future__ import annotations

import json

MARK = "@@UAP_XR_PROBE@@"

_COMMON = r'''
import json as _j
import unreal as _u

_w = UAP_WORLD
_pc = _u.GameplayStatics.get_player_controller(_w, 0)
_pawn = _pc.get_controlled_pawn() if _pc else None


def _emit(d):
    print("@@UAP_XR_PROBE@@" + _j.dumps(d))


def _v(x):
    return [x.x, x.y, x.z]


def _cname(c):
    return c.get_class().get_name()


def _find_actor(spec):
    want = spec.lower()
    exact, part = [], []
    for a in _u.GameplayStatics.get_all_actors_of_class(_w, _u.Actor):
        names = [a.get_name().lower()]
        try:
            names.append(a.get_actor_label().lower())
        except Exception:
            pass
        if want in names:
            exact.append(a)
        elif any(want in n for n in names):
            part.append(a)
    hits = exact or part
    if len(hits) != 1:
        return None, [h.get_name() for h in hits][:8]
    return hits[0], []
'''

_REACH = r'''
_hand = "__HAND__"
_spec = "__TARGET__"
_off = __OFFSET__
if _pawn is None:
    _emit({"ok": False, "error": "client has no controlled pawn (no PIE, or the VR pawn is not possessed)"})
else:
    _an, _cn = (_spec.split(".", 1) + [""])[:2]
    _act, _amb = _find_actor(_an)
    if _act is None:
        _emit({"ok": False, "error": "target actor %r matched %d actors: %s" % (_an, len(_amb), _amb)})
    else:
        _tp = None
        _how = "actor location"
        _root = _act.get_component_by_class(_u.SceneComponent) if not _cn else None
        _comp = None
        if _cn:
            for _c in _act.get_components_by_class(_u.SceneComponent):
                if _c.get_name().lower() == _cn.lower():
                    _comp = _c
                    break
            if _comp is None:
                _emit({"ok": False, "error": "actor %s has no component %r" % (_act.get_name(), _cn)})
        else:
            _comp = _act.get_component_by_class(_u.PrimitiveComponent)
        if not (_cn and _comp is None):
            if _comp is not None:
                try:
                    for _s in _comp.get_all_socket_names():
                        if any(k in str(_s).lower() for k in ("grip", "hand", "grab")):
                            _tp = _comp.get_socket_location(_s)
                            _how = "socket " + str(_s)
                            break
                except Exception:
                    pass
            if _tp is None and _cn and _comp is not None:
                _tp = _u.SystemLibrary.get_component_bounds(_comp)[0]
                _how = "component bounds centre"
            if _off is not None:
                _btf = _comp.get_world_transform() if _comp is not None else _act.get_actor_transform()
                _tp = _u.MathLibrary.transform_location(_btf, _u.Vector(_off[0], _off[1], _off[2]))
                _how = "component local offset %s" % (_off,)
            if _tp is None:
                _tp = _act.get_actor_location()
            _ctrl = None
            _names = []
            for _c in _pawn.get_components_by_class(_u.SceneComponent):
                if _cname(_c) == "GripMotionControllerComponent":
                    _names.append(_c.get_name())
                    if _hand in _c.get_name().lower() and "aim" not in _c.get_name().lower():
                        _ctrl = _c
            if _ctrl is None:
                _emit({"ok": False, "error": "no %s GripMotionControllerComponent on pawn %s (found %s)" % (_hand, _pawn.get_name(), _names)})
            else:
                _grab, _rad, _gname = _ctrl, 0.0, _ctrl.get_name()
                for _c in _ctrl.get_children_components(True):
                    if "SphereComponent" in _cname(_c) and "grab" in _c.get_name().lower():
                        _grab, _gname = _c, _c.get_name()
                        try:
                            _rad = float(_c.get_editor_property("sphere_radius"))
                        except Exception:
                            pass
                        break
                _par = _ctrl.get_attach_parent()
                _ptf = _par.get_world_transform() if _par else _u.Transform()
                _gp = _grab.get_world_transform().translation
                _ti = _u.MathLibrary.inverse_transform_location
                _t1, _g1 = _ti(_ptf, _tp), _ti(_ptf, _gp)
                _rl = _ctrl.relative_location
                _rr = _ctrl.relative_rotation
                _emit({"ok": True, "actor": _act.get_name(), "target_how": _how, "target": _v(_tp),
                       "grab": _v(_gp), "grab_component": _gname, "grab_radius": _rad,
                       "controller": _ctrl.get_name(),
                       "delta_tracking": [_t1.x - _g1.x, _t1.y - _g1.y, _t1.z - _g1.z],
                       "ctrl_rel_loc": _v(_rl), "ctrl_rel_rot": [_rr.pitch, _rr.yaw, _rr.roll]})
'''

_LOOK = r'''
_spec = "__TARGET__"
_eye = __EYE__
if _pawn is None:
    _emit({"ok": False, "error": "client has no controlled pawn (no PIE, or the VR pawn is not possessed)"})
else:
    _cam = None
    for _c in _pawn.get_components_by_class(_u.SceneComponent):
        if "camera" in _cname(_c).lower() and "replicated" in _c.get_name().lower():
            _cam = _c
    if _cam is None:
        for _c in _pawn.get_components_by_class(_u.SceneComponent):
            if "camera" in _cname(_c).lower():
                _cam = _c
    _par = _cam.get_attach_parent() if _cam else None
    _prot = _pawn.get_actor_rotation()
    if not _spec:
        _emit({"ok": True, "pitch": 0.0, "yaw": 0.0, "target": None})
    else:
        _spec, _cn = (_spec.split(".", 1) + [""])[:2]
        _act, _amb = _find_actor(_spec)
        if _act is None:
            _emit({"ok": False, "error": "look-at actor %r matched %d actors: %s" % (_spec, len(_amb), _amb)})
        else:
            _tp = _act.get_actor_location()
            if _cn:
                for _c in _act.get_components_by_class(_u.SceneComponent):
                    if _c.get_name().lower() == _cn.lower():
                        _tp = _c.get_world_transform().translation
            _eyew = _cam.get_world_transform().translation
            _lr = _u.MathLibrary.find_look_at_rotation(_eyew, _tp)
            _yaw = (_lr.yaw - _prot.yaw + 180.0) % 360.0 - 180.0
            _emit({"ok": True, "pitch": _lr.pitch, "yaw": _yaw, "target": _v(_tp), "actor": _act.get_name()})
'''


def _lit(s: str) -> str:
    # The value lands inside a double-quoted literal in the script.
    return json.dumps(str(s))[1:-1]


def reach_code(hand: str, target: str, offset: tuple[float, float, float] | None = None) -> str:
    """`offset` (cm) is in the target component's LOCAL space (actor root when no component is
    named); it replaces the socket / bounds-centre point so a handle off the origin can be reached."""
    off = "None" if offset is None else repr(tuple(float(v) for v in offset))
    return (_COMMON + _REACH.replace("__HAND__", _lit(hand.lower())).replace("__TARGET__", _lit(target))
            .replace("__OFFSET__", off))


def look_code(target: str, eye: float) -> str:
    return _COMMON + _LOOK.replace("__TARGET__", _lit(target)).replace("__EYE__", repr(float(eye)))


def parse_probe(lines: list[str]) -> dict:
    """The JSON the probe printed (last marker wins), or an `ok:false` saying it printed none."""
    found = None
    for line in lines:
        for part in str(line).splitlines():
            if MARK in part:
                try:
                    found = json.loads(part.split(MARK, 1)[1])
                except ValueError:
                    pass
    return found or {"ok": False, "error": "the editor-side probe printed nothing (no PIE, or the "
                                           "script failed): " + " | ".join(str(x) for x in lines)[:300]}
