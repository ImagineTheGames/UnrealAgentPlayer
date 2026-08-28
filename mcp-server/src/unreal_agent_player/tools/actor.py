from __future__ import annotations

import json
from typing import Any

from unreal_agent_player.errors import AgentError, ErrorCode

_FIND_TEMPLATE = r"""
import json, unreal, re
world_kind = {world!r}
cls_name = {cls!r}
tag_name = {tag!r}
pattern = {pattern!r}

world = unreal.EditorLevelLibrary.get_editor_world()
actors = []
for a in unreal.EditorLevelLibrary.get_all_level_actors():
    if cls_name and a.get_class().get_name() != cls_name and a.get_class().get_path_name() != cls_name:
        continue
    if tag_name and unreal.Name(tag_name) not in a.tags:
        continue
    if pattern and not re.search(pattern, a.get_name()):
        continue
    loc = a.get_actor_location()
    actors.append({{
        'path': a.get_path_name(),
        'class': a.get_class().get_name(),
        'location': [loc.x, loc.y, loc.z],
        'tags': [t.string for t in a.tags],
    }})
print(json.dumps({{'actors': actors}}))
"""

_GET_PROPS_TEMPLATE = r"""
import json, unreal
path = {path!r}
names = {names!r}
obj = unreal.load_object(None, path)
if obj is None:
    print(json.dumps({{'error': 'ACTOR_NOT_FOUND'}}))
else:
    out = {{}}
    for n in (names or []):
        try:
            out[n] = obj.get_editor_property(n)
        except Exception as e:
            out[n] = {{'_error': str(e)}}
    def _coerce(v):
        try:
            json.dumps(v); return v
        except TypeError:
            return str(v)
    out = {{k: _coerce(v) for k, v in out.items()}}
    print(json.dumps({{'properties': out}}))
"""

_SET_PROP_TEMPLATE = r"""
import json, unreal
path = {path!r}; name = {name!r}; value = {value!r}
obj = unreal.load_object(None, path)
if obj is None:
    print(json.dumps({{'error': 'ACTOR_NOT_FOUND'}}))
else:
    obj.set_editor_property(name, value)
    print(json.dumps({{'ok': True}}))
"""

_CALL_FN_TEMPLATE = r"""
import json, unreal
path = {path!r}; fn = {fn!r}; args = {args!r}
obj = unreal.load_object(None, path)
if obj is None:
    print(json.dumps({{'error': 'ACTOR_NOT_FOUND'}}))
else:
    result = obj.call_method(fn, tuple(args.values()) if isinstance(args, dict) else tuple(args or []))
    print(json.dumps({{'result': str(result) if result is not None else None}}))
"""


def _run(py_exec, code: str) -> dict[str, Any]:
    resp = py_exec.exec_python(code)
    if resp.get("result") != "success":
        raise AgentError(ErrorCode.PYTHON_RUNTIME, str(resp))
    for entry in resp.get("output", []) or []:
        if entry.get("type") != "Error":
            raw = entry.get("output", "").strip()
            if raw:
                try:
                    return json.loads(raw.splitlines()[-1])
                except json.JSONDecodeError:
                    continue
    return {}


async def actor_find(
    *, rc: Any, py_exec: Any,
    class_: str | None = None, tag: str | None = None,
    name_pattern: str | None = None, world: str = "editor",
) -> dict[str, Any]:
    code = _FIND_TEMPLATE.format(world=world, cls=class_, tag=tag, pattern=name_pattern)
    body = _run(py_exec, code)
    return {"ok": True, "actors": body.get("actors", [])}


async def actor_get_properties(
    *, rc: Any, py_exec: Any, path: str, property_names: list[str] | None = None,
) -> dict[str, Any]:
    code = _GET_PROPS_TEMPLATE.format(path=path, names=property_names)
    body = _run(py_exec, code)
    if body.get("error") == "ACTOR_NOT_FOUND":
        raise AgentError(ErrorCode.ACTOR_NOT_FOUND, f"No object at {path}")
    return {"ok": True, "properties": body.get("properties", {})}


async def actor_set_property(
    *, rc: Any, py_exec: Any, path: str, name: str, value: Any,
) -> dict[str, Any]:
    code = _SET_PROP_TEMPLATE.format(path=path, name=name, value=value)
    body = _run(py_exec, code)
    if body.get("error") == "ACTOR_NOT_FOUND":
        raise AgentError(ErrorCode.ACTOR_NOT_FOUND, f"No object at {path}")
    return {"ok": True}


async def actor_call_function(
    *, rc: Any, py_exec: Any, path: str, function: str, args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code = _CALL_FN_TEMPLATE.format(path=path, fn=function, args=args or {})
    body = _run(py_exec, code)
    if body.get("error") == "ACTOR_NOT_FOUND":
        raise AgentError(ErrorCode.ACTOR_NOT_FOUND, f"No object at {path}")
    return {"ok": True, "result": body.get("result")}
