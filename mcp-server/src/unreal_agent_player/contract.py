"""CLI <-> plugin contract: what this CLI expects, what a project's editor actually exports,
and the gap between the two -- known BEFORE a verb fails instead of after.

Why this exists
---------------
Every project vendors its OWN copy of the uap plugin; they all share ONE CLI. A pulled CLI
is live everywhere instantly, a plugin copy only catches up when that project syncs and
REBUILDS, so CLI/plugin skew is permanent and expected (docs/known-issues.md #23). Until now
the CLI only ever learned it was ahead by FAILING -- a raw RemoteControl 404 on the one verb
someone had remembered to guard -- and never learned it at all when the gap was a missing
PARAMETER rather than a missing verb (`HoldAxis` without `SlateUser` accepts the call and
silently drops the argument: #26's silent discard, one layer up).

Both sides are DERIVED. Nothing here is a number anyone has to remember to bump:

  expected -- parsed from the UFUNCTION declarations in this repo's own
              Plugin/Source/**/Public/UAPAgent*Subsystem.h. That header IS the newest plugin;
              a project's copy is a copy of it. Change the header and the expectation moves
              with it, in the same commit, with no second edit.
  live     -- read from the running editor's RemoteControl preset
              (GET /remote/preset/UAP_Preset), which RemoteControl builds from the COMPILED
              binary's reflection data. It is what that editor can really do, and it is the
              exact route `_rc_call` uses, so it cannot disagree with the calls we make.

The plugin's own `GetPluginVersion()` is deliberately NOT used for this. It is a hardcoded
`TEXT("0.0.1")` in two .cpp files that has never been bumped across every verb added since
the first release -- a stamp that reports "up to date" while being wrong is worse than no
stamp, so it stays in `uap status` as an informational build string and decides nothing.

Absence is "unknown", never "current": no header (a non-editable install), no editor, an old
RemoteControl -- every one of those yields `state: "unknown"` and the CLI proceeds exactly as
it did before. This module can only ever add information.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import httpx

# The preset each subsystem is exposed on, and the header it is compiled from. The CLI's verbs
# all go through the editor preset; the runtime one serves the standalone-game path.
PRESET_HEADERS = {
    "UAP_Preset": "Plugin/Source/UnrealAgentPlayer/Public/UAPAgentSubsystem.h",
    "UAP_RuntimePreset": (
        "Plugin/Source/UnrealAgentPlayerRuntime/Public/UAPAgentRuntimeSubsystem.h"),
}
DEFAULT_PRESET = "UAP_Preset"

# What a verb is FOR, quoted back when a plugin copy lacks it, so the refusal says what is
# unavailable and not just which symbol is missing. A verb with no entry degrades to the
# generic phrasing -- it never degrades to a false "up to date".
PURPOSE = {
    "StartPIEMode": "`pie start --mode vr` (the HMD code path; flat PIE cannot show HMD-only bugs)",
    "StopPIEEx": "`pie stop` teardown confirmation (a stop that is not confirmed can be a no-op)",
    "IsPIEInProgress": "`pie stop` / lease-release confirmation that the editor is really free",
    "HoldKey": "`input hold` -- sustained in-engine input; a single injected event is not a hold",
    "HoldAxis": "`input axis` -- analog/VR locomotion held at frame rate",
    "ReleaseHeldInput": "`input release` -- the recovery path for a stuck key",
    "GetHeldInput": "`input status` -- what is held, and whether it is really down",
    "StartPropertySample": "`sample` -- per-frame sampling; an exec round-trip cannot see a "
                           "sub-second window",
    "ReadPropertySample": "`sample read`",
    "StopPropertySample": "`sample` teardown",
    "ListTestHelpersJson": "`helpers` -- helper names and arg schemas (the struct return "
                           "arrives empty)",
    "GetLogCursor": "`log cursor` -- reading the editor log ring buffer through the plugin",
    "GetLogsSince": "`log since` / `log tail`",
    "DumpViewportUI": "`read-ui` / `click` -- reading on-screen UMG text and focus",
    "SelectTab": "`tab` -- selecting a CommonUI tab",
    "NavigateUI": "`nav` -- Slate focus navigation",
    "CaptureViewportWithUI": "`screenshot` -- capturing the composited game+UMG frame",
}

# Argument-level purposes: a verb can be present while the PARAMETER the CLI wants is not.
ARG_PURPOSE = {
    ("HoldAxis", "SlateUser"): "`input axis --user N` -- the SLATE route, the only one an "
                               "analog/virtual cursor or input pre-processor can see",
    ("InjectAxis", "SlateUser"): "`rc InjectAxis SlateUser=N` -- the Slate route",
    ("InjectGamepad", "SlateUser"): "`rc InjectGamepad SlateUser=N` -- the Slate route",
}

# UHT type name -> how a `key=value` string should be encoded on the wire. RemoteControl binds
# the argument struct by JSON type: a number offered to an FString parameter does not bind, and
# the zero-initialised struct hands the plugin "" instead -- accepted, silently wrong (#27).
_STRING_TYPES = {"FString", "FName", "FText"}
_BOOL_TYPES = {"bool"}
_INT_TYPES = {"int8", "int16", "int32", "int64",
              "uint8", "uint16", "uint32", "uint64", "int", "byte"}
_FLOAT_TYPES = {"float", "double"}

# UHT gives every UCLASS an ExecuteUbergraph thunk. It is reflection plumbing, not a verb, and
# it appears in RemoteControl's function list -- filtered out so it never reads as "this
# project's plugin is ahead of the CLI".
_IGNORED_VERBS = {"ExecuteUbergraph"}


def kind_of(type_name: str | None) -> str:
    """'string' | 'bool' | 'int' | 'float' | 'unknown' for a declared UHT argument type.

    Enums and structs are 'unknown' on purpose: RemoteControl accepts an enumerator by NAME
    and by index, so there is no single right encoding and the caller's own text is the best
    guess available. 'unknown' is reported as a guess rather than passed off as declared.
    """
    if not type_name:
        return "unknown"
    t = re.sub(r"^\s*const\s+", "", type_name).strip().rstrip("&*").strip()
    if t in _STRING_TYPES:
        return "string"
    if t in _BOOL_TYPES:
        return "bool"
    if t in _INT_TYPES:
        return "int"
    if t in _FLOAT_TYPES:
        return "float"
    return "unknown"


# --- expected: parsed from this repo's plugin headers -------------------------------------

# `UFUNCTION(...)` immediately followed by the declaration. Return types include templates
# (TArray<FAgentHelperDescriptor>), declarations wrap across lines (GetLogsSince), and some
# are const -- all of which this covers. Anything it cannot parse is simply not expected,
# which is the safe direction: the check under-reports rather than inventing a gap.
_UFUNCTION_RE = re.compile(
    r"UFUNCTION\s*\([^)]*\)\s*"          # the macro and its specifiers
    r"[\w:]+(?:\s*<[^>]*>)?[\s*&]+"      # return type (optionally templated)
    r"(\w+)\s*\(([^)]*)\)\s*(?:const\s*)?;",
    re.S,
)


def _split_args(arglist: str) -> list[str]:
    """Split a C++ parameter list on top-level commas (TMap<A,B> has its own)."""
    parts, depth, cur = [], 0, ""
    for ch in arglist:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p for p in (s.strip() for s in parts) if p]


def _arg_name_and_type(decl: str) -> tuple[str, str] | None:
    """('KeyName', 'FString') from `FString KeyName` / `const FVector& Position`."""
    words = re.findall(r"[A-Za-z_]\w*(?:\s*<[^>]*>)?", decl)
    words = [w for w in words if w != "const"]
    if len(words) < 2:
        return None
    return words[-1], words[-2]


def parse_header(text: str) -> dict[str, dict[str, str]]:
    """{verb: {arg_name: arg_type}} for every UFUNCTION declared in a plugin header."""
    out: dict[str, dict[str, str]] = {}
    for name, arglist in _UFUNCTION_RE.findall(text):
        args: dict[str, str] = {}
        for decl in _split_args(arglist):
            pair = _arg_name_and_type(decl)
            if pair:
                args[pair[0]] = pair[1]
        out[name] = args
    return out


def repo_root() -> pathlib.Path:
    """This repo's root: <root>/mcp-server/src/unreal_agent_player/contract.py."""
    return pathlib.Path(__file__).resolve().parents[3]


def expected_contract(preset: str = DEFAULT_PRESET,
                      root: pathlib.Path | None = None) -> dict[str, dict[str, str]] | None:
    """What the newest plugin exports, read from this repo's header. None when it is not on
    disk (e.g. a non-editable install) -- which makes the comparison 'unknown', not 'current'.
    """
    rel = PRESET_HEADERS.get(preset)
    if not rel:
        return None
    path = (root or repo_root()) / rel
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parsed = parse_header(text)
    return parsed or None


# --- live: read from the running editor's RemoteControl preset -----------------------------

def parse_preset(body: Any) -> dict[str, dict[str, str]] | None:
    """{verb: {arg_name: declared_type}} from a GET /remote/preset/<name> body."""
    if not isinstance(body, dict):
        return None
    preset = body.get("Preset")
    if not isinstance(preset, dict):
        return None
    out: dict[str, dict[str, str]] = {}
    for group in preset.get("Groups") or []:
        for exposed in (group or {}).get("ExposedFunctions") or []:
            fn = (exposed or {}).get("UnderlyingFunction") or {}
            name = fn.get("Name")
            if not name or name in _IGNORED_VERBS:
                continue
            out[name] = {a.get("Name"): a.get("Type", "")
                         for a in (fn.get("Arguments") or []) if a.get("Name")}
    return out or None


def fetch_live_contract(port: int, *, preset: str = DEFAULT_PRESET,
                        host: str = "127.0.0.1",
                        timeout: float = 5.0) -> dict[str, dict[str, str]] | None:
    """Ask the editor what it actually exports. None on any failure -- an unreachable editor
    or a RemoteControl too old to describe a preset must never break a call that would
    otherwise have worked.

    This is a plain RemoteControl endpoint, not a plugin verb, so it answers on OLD plugin
    copies too. That is the whole point: the copy that cannot tell you its own capabilities is
    exactly the copy you need to ask about.
    """
    url = f"http://{host}:{port}/remote/preset/{preset}"
    try:
        resp = httpx.get(url, timeout=timeout)
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code >= 400:
        return None
    try:
        return parse_preset(resp.json())
    except (json.JSONDecodeError, ValueError):
        return None


# --- the gap --------------------------------------------------------------------------------

def compare(expected: dict[str, dict[str, str]] | None,
            live: dict[str, dict[str, str]] | None) -> dict[str, Any]:
    """The gap, as data. `state` is one of:

      unknown  either side could not be read -- say so, never claim "current"
      behind   this editor's plugin copy lacks a verb, or a verb's parameter
      current  everything this CLI's headers declare is present and callable
    """
    if not expected or not live:
        return {"state": "unknown", "checked_verbs": 0,
                "reason": ("no plugin header in this checkout" if not expected
                           else "editor did not answer the RemoteControl preset query")}
    missing_verbs = sorted(v for v in expected if v not in live)
    missing_args: dict[str, list[str]] = {}
    for verb, args in expected.items():
        got = live.get(verb)
        if got is None:
            continue
        gap = sorted(a for a in args if a not in got)
        if gap:
            missing_args[verb] = gap
    report: dict[str, Any] = {
        "state": "behind" if (missing_verbs or missing_args) else "current",
        "checked_verbs": len(expected),
        "missing_verbs": missing_verbs,
        "missing_args": missing_args,
    }
    ahead = sorted(v for v in live if v not in expected)
    if ahead:
        # The editor has verbs this checkout does not declare: that project rebuilt from a
        # NEWER commit than the one running this CLI. Nothing breaks -- the CLI just does not
        # call them -- so it stays informational and does not make the state "behind".
        report["plugin_ahead"] = ahead
    return report


def _unavailable(report: dict[str, Any]) -> list[str]:
    """What the caller loses, in the caller's own vocabulary, for the message below."""
    out: list[str] = []
    for verb in report.get("missing_verbs") or []:
        out.append(PURPOSE.get(verb) or f"`rc {verb}`")
    for verb, args in (report.get("missing_args") or {}).items():
        for arg in args:
            out.append(ARG_PURPOSE.get((verb, arg)) or f"the `{arg}` argument of `{verb}`")
    return out


def skew_message(report: dict[str, Any], project: str | None) -> str | None:
    """The house-style refusal: name the gap, say what is unavailable because of it, give the
    one remedy. None when there is nothing to say.
    """
    if report.get("state") != "behind":
        return None
    gaps: list[str] = list(report.get("missing_verbs") or [])
    gaps += [f"{verb}.{arg}" for verb, args in (report.get("missing_args") or {}).items()
             for arg in args]
    lost = _unavailable(report)
    return (
        f"this editor's plugin copy is BEHIND the CLI: it does not export "
        f"{', '.join(gaps)}. Every project vendors its own plugin copy while the CLI is "
        f"shared, so a pulled CLI runs ahead of a project until that project rebuilds -- this "
        f"is expected skew, not a broken editor. Unavailable until it catches up: "
        f"{'; '.join(lost)}. Remedy: sync and rebuild {project or 'that project'} "
        f"(Restart-Editor.ps1). Everything else keeps working."
    )


def arg_skew_message(func: str, arg: str, project: str | None) -> str:
    """The same refusal for ONE argument, used where the CLI is about to send a parameter the
    editor's copy cannot receive. Worth its own message because this failure is SILENT:
    RemoteControl accepts the call, drops the unbindable argument, and the plugin reads the
    zero-initialised default -- so the run reports success having done something else.
    """
    lost = ARG_PURPOSE.get((func, arg)) or f"the `{arg}` argument of `{func}`"
    return (
        f"this editor's plugin copy has no `{arg}` parameter on `{func}`, so it is BEHIND the "
        f"CLI (each project vendors its own copy; the shared CLI updates on pull). Refusing "
        f"rather than sending it: RemoteControl would accept the call, drop the argument it "
        f"cannot bind, and the plugin would run its DEFAULT behaviour and report success -- "
        f"the silent wrong answer, not an error. Unavailable until it catches up: {lost}. "
        f"Remedy: sync and rebuild {project or 'that project'} (Restart-Editor.ps1)."
    )
