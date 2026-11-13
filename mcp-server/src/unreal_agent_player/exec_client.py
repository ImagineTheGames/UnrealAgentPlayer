"""`uap exec --client N`: run the Python with client N's PIE world as the target world [17tm466zztw].

With several PIE clients in ONE editor process there are several game worlds. `uap exec` used to
ignore `--client`, so every script ran with the editor's idea of "the" game world (the FIRST PIE
world, the host's): a 2-client "client reads" test read the host twice and passed. This module
wraps the user's Python in a preamble that, in the editor, picks client N's world by PIE instance
index (the same index `uap input clients` lists), and:

  * defines `UAP_CLIENT` (int) and `UAP_WORLD` (that client's `unreal.World`) for the script;
  * REFUSES a script that calls `get_game_world()` when that getter would answer a different
    world than client N's. unreal's wrapped classes are IMMUTABLE (measured: `cannot set
    'get_game_world' attribute of immutable type 'EditorLevelLibrary'`), so the getter cannot be
    pointed at client N; running the script anyway would read the wrong client and say ok;
  * REFUSES (the script is not run, `ok:false`) when that client does not exist, with the list of
    clients that do. It never falls back to the host.

The preamble reports through marker lines on stdout, which the CLI strips back out
(`parse_client_output`): the caller sees `client: {index, world, ...}` or the refusal, not noise.
No ".py" may appear anywhere in the generated text (UE's ExecuteFile mode reads the first ".py"
as a script path) -- `build_client_code` runs `guard_py_path_trigger` on the result.
"""

from __future__ import annotations

import re

from unreal_agent_player.transport import guard_py_path_trigger

MARK_WORLD = "@@UAP_CLIENT_WORLD@@"
MARK_REFUSED = "@@UAP_CLIENT_REFUSED@@"

# The editor-side half. Names are underscore-prefixed so they cannot collide with the user's.
# `__UAP_SEL__` is replaced by the integer client index; `__UAP_USER__` by the user's code literal.
_PREAMBLE = r'''
import re as _uap_re
import unreal as _uap_u


def _uap_cleanup():
    # Remote exec keeps module globals between calls, and a Python wrapper of a UWorld in them
    # roots that world: a PIE world still referenced after PIE ends is a 'World Memory Leaks'
    # editor crash. Drop every world reference this wrapper made, whatever the script did.
    for _n in [k for k in list(globals()) if k.startswith("_uap_") or k in ("UAP_WORLD", "UAP_CLIENT")]:
        del globals()[_n]


_uap_sel = __UAP_SEL__
_uap_err = None
_uap_world = None
_uap_found = {}
for _uap_w in _uap_u.ObjectIterator(_uap_u.World):
    try:
        _uap_names = _uap_w.get_name() + "|" + _uap_w.get_outer().get_name()
    except Exception:
        continue
    _uap_m = _uap_re.search(r"UEDPIE_(\d+)_", _uap_names)
    if _uap_m:
        _uap_found[int(_uap_m.group(1))] = _uap_w
if _uap_sel in _uap_found:
    _uap_world = _uap_found[_uap_sel]
elif not _uap_found:
    _uap_err = ("no PIE world is running, so there is no client %d to target. Start PIE "
                "(uap pie start), or drop --client / unset UAP_CLIENT for an editor-only script."
                % _uap_sel)
else:
    _uap_err = ("there is no client %d. Running the script against a different client than the "
                "one named is the silent wrong answer this refusal exists to stop. Clients right "
                "now: %s." % (_uap_sel, ", ".join(
                    "%d (%s)" % (_k, _uap_found[_k].get_outer().get_name()) for _k in sorted(_uap_found))))

_uap_code = __UAP_USER__
if _uap_err is None and "get_game_world" in _uap_code:
    # unreal's wrapped classes are immutable, so get_game_world() CANNOT be pointed at client N:
    # it answers the editor's own play world. If that is not client N's world, running the
    # script would read the wrong client while reporting success.
    try:
        _uap_dflt = _uap_u.get_editor_subsystem(_uap_u.UnrealEditorSubsystem).get_game_world()
    except Exception:
        _uap_dflt = None
    if _uap_dflt is not None and _uap_dflt != _uap_world:
        _uap_err = ("the script calls get_game_world(), which answers the editor's default play "
                    "world (%s), not client %d's (%s). Use the UAP_WORLD global instead: it is "
                    "client %d's world." % (_uap_dflt.get_outer().get_name(), _uap_sel,
                                            _uap_world.get_outer().get_name(), _uap_sel))

if _uap_err is not None:
    print("@@UAP_CLIENT_REFUSED@@ " + _uap_err)
    _uap_cleanup()
else:
    UAP_CLIENT = _uap_sel
    UAP_WORLD = _uap_world
    print("@@UAP_CLIENT_WORLD@@ %d|%s" % (_uap_sel, _uap_world.get_outer().get_name()))
    try:
        exec(compile(_uap_code, "<uap exec --client>", "exec"))
    finally:
        _uap_cleanup()
'''


def client_index(selector: str) -> int:
    """The PIE instance index a `--client` value names. Raises ValueError for anything else
    (`all` makes no sense for one script: it would run N times against N worlds)."""
    sel = str(selector or "").strip().lower()
    if sel == "all":
        raise ValueError("--client all makes no sense for exec: one script, one world. Name one "
                         "client (--client 0 / 1; `uap input clients` lists them), or run the "
                         "script once per client.")
    if not sel.isdigit():
        raise ValueError(f"--client must be a PIE instance index (0, 1, ...); got {selector!r}. "
                         "`uap input clients` lists the clients.")
    return int(sel)


def build_client_code(code: str, selector: str) -> str:
    """Wrap `code` so it runs against client `selector`'s PIE world. See the module docstring."""
    idx = client_index(selector)
    text = _PREAMBLE.replace("__UAP_SEL__", str(idx)).replace("__UAP_USER__", repr(code))
    return guard_py_path_trigger(text)


_WORLD_RE = re.compile(re.escape(MARK_WORLD) + r"\s+(\d+)\|([^\n]*)")
_REFUSED_RE = re.compile(re.escape(MARK_REFUSED) + r"\s+(.*)")


def parse_client_output(lines: list[str]) -> tuple[list[str], dict | None, str | None]:
    """Split the preamble's marker lines out of the exec output.

    Returns (user_lines, client_info | None, refusal | None). A world marker means the preamble
    ran and picked a world; a refusal means it did not run the script. Neither marker means the
    preamble itself did not run (e.g. a `-game` client without the editor module) -- the caller
    must treat that as NOT having honoured --client."""
    info: dict | None = None
    refusal: str | None = None
    kept: list[str] = []
    for raw in lines:
        # One output entry can hold several lines (print buffers), so scan line by line.
        out_lines = []
        for line in str(raw).splitlines(keepends=True):
            m = _WORLD_RE.search(line)
            if m:
                info = {"index": int(m.group(1)), "world": m.group(2).strip()}
                continue
            r = _REFUSED_RE.search(line)
            if r:
                refusal = r.group(1).strip()
                continue
            out_lines.append(line)
        text = "".join(out_lines)
        if text.strip() or not str(raw).strip():
            kept.append(text)
    return kept, info, refusal
