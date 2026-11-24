"""Editor-side probes for `uap pawn teleport` / `uap pawn place`.

The pawn is moved with K2_TeleportTo (a plain gameplay call, like the game's own teleports) and the
controller rotation is set with set_control_rotation. NO property is written, so no PostEditChange /
RerunConstructionScripts fires on the PIE actor -- the failure the exec PIE-write guard exists for
(CRASH-0BD72FF1). The probe runs through the same client-world wrapper as `xr reach`, so it lands in
the PIE world of --client and never the editor world; it refuses when that world is not a PIE one.
Every move is logged to the UE log (LogTemp) so a log sweep shows it.

The script prints one marker line of JSON (xr_reach.MARK); nothing in it may contain the text of a
python file extension (UE's remote exec reads the first one as a script path).
"""

from __future__ import annotations

from unreal_agent_player.xr_reach import (
    _COMMON,
    _lit,
)

_MOVE_TAIL = r'''
    _from = _pawn.get_actor_location()
    _ok = _pawn.teleport(_u.Vector(_dest[0], _dest[1], _dest[2]), _u.Rotator(0.0, 0.0, _yaw))
    if _ok and _pc is not None:
        _pc.set_control_rotation(_u.Rotator(0.0, 0.0, _yaw))
    _to = _pawn.get_actor_location()
    _u.log("uap pawn: %s %s -> %s yaw %.1f ok=%s world=%s" % (_pawn.get_name(), _v(_from), _v(_to), _yaw, _ok, _w.get_name()))
    _emit({"ok": bool(_ok), "pawn": _pawn.get_name(), "world": _w.get_name(), "from": _v(_from), "to": _v(_to),
           "yaw": _yaw, "error": None if _ok else "K2_TeleportTo returned false (blocked at the destination?)"})
'''

_HEAD = r'''
_player = __PLAYER__
_pc = _u.GameplayStatics.get_player_controller(_w, _player)
_pawn = _pc.get_controlled_pawn() if _pc else None
if "UEDPIE" not in _w.get_outer().get_name():
    _emit({"ok": False, "error": "refusing: world %s is not a PIE world (start PIE first)" % _w.get_outer().get_name()})
elif _pawn is None:
    _emit({"ok": False, "error": "player %d has no controlled pawn" % _player})
else:
'''

_TELEPORT = _HEAD + r'''    _dest = (__X__, __Y__, __Z__)
    _yv = [__YAW__]
    _yaw = _yv[0] if _yv[0] is not None else _pawn.get_actor_rotation().yaw
''' + _MOVE_TAIL

_PLACE = _HEAD + r'''    _act, _amb = _find_actor("__ACTOR__")
    if _act is None:
        _emit({"ok": False, "error": "actor %r matched %d actors: %s" % ("__ACTOR__", len(_amb), _amb)})
    else:
        import math as _m
        _al = _act.get_actor_location()
        _pl = _pawn.get_actor_location()
        _b = _m.radians(__BEARING__)
        _dest = (_al.x + __DIST__ * _m.cos(_b), _al.y + __DIST__ * _m.sin(_b), _pl.z)
        _yaw = _m.degrees(_m.atan2(_al.y - _dest[1], _al.x - _dest[0]))
''' + "\n".join(("    " + ln) if ln else ln for ln in _MOVE_TAIL.split("\n"))


def teleport_code(x: float, y: float, z: float, yaw: float | None = None, player: int = 0) -> str:
    return (_COMMON + _TELEPORT.replace("__PLAYER__", repr(int(player)))
            .replace("__X__", repr(float(x))).replace("__Y__", repr(float(y)))
            .replace("__Z__", repr(float(z)))
            .replace("__YAW__", "None" if yaw is None else repr(float(yaw))))


def place_code(actor: str, dist: float, bearing: float = 180.0, player: int = 0) -> str:
    return (_COMMON + _PLACE.replace("__PLAYER__", repr(int(player))).replace("__ACTOR__", _lit(actor))
            .replace("__DIST__", repr(float(dist))).replace("__BEARING__", repr(float(bearing))))


