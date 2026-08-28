"""Pre-push guard: a changed plugin UFUNCTION signature must SAY it changed.

Why this is enforced rather than merely written down (CONTRIBUTING.md, "Changing a
signature"): a commit that describes the fix but not the break is a silent trap with a delayed
fuse. Every other trap this repo documents is one you spring on yourself, in the session that
made it, with all the context in your head. This one is sprung weeks later by a teammate on a
different project who synced the plugin, has no reason to suspect anything, and whose only
symptom is a call that no longer compiles -- or worse, a BlueprintCallable node that breaks
only when that asset is next compiled. The person who could have warned them is the person
writing the commit message, and that is the last moment anyone reads anything before rebuild.

So: if this push changes or removes a UFUNCTION signature in a plugin PUBLIC header, one of
the pushed commit messages has to say so. Set UAP_SKIP_SIGCHECK=1 to override.
"""

from __future__ import annotations

import os
import subprocess
import sys

from unreal_agent_player.contract import PRESET_HEADERS, parse_header

# Any of these in a pushed commit message counts as declaring the break.
MARKERS = ("BREAKING", "SIGNATURE CHANGE", "SIGNATURE-CHANGE")


def signatures(text: str) -> dict[str, list[str]]:
    """{verb: [arg types, in order]} -- the shape a caller has to match."""
    return {name: list(args.values()) for name, args in parse_header(text).items()}


def diff_signatures(old: dict[str, list[str]],
                    new: dict[str, list[str]]) -> dict[str, list[str]]:
    """What an existing caller cannot survive, and what it can.

    `changed` and `removed` break callers on the old arity. `added` does not -- it is the
    other, already-handled skew direction (docs/known-issues.md #23): a project that has not
    rebuilt simply 404s, and `_rc_require` names that for what it is.
    """
    changed = [f"{n}({', '.join(old[n])}) -> ({', '.join(new[n])})"
               for n in old if n in new and old[n] != new[n]]
    return {
        "changed": sorted(changed),
        "removed": sorted(n for n in old if n not in new),
        "added": sorted(n for n in new if n not in old),
    }


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""


def _header_at(ref: str, path: str) -> str:
    return _git("show", f"{ref}:{path}")


def collect(base: str) -> dict[str, list[str]]:
    """Breaking signature changes between `base` and the working tree, across every plugin
    public header. A header that does not exist at `base` is new -- nothing to break."""
    acc: dict[str, set[str]] = {"changed": set(), "removed": set(), "added": set()}
    for path in PRESET_HEADERS.values():
        old_text = _header_at(base, path)
        if not old_text:
            continue
        new_text = _header_at("HEAD", path) or ""
        d = diff_signatures(signatures(old_text), signatures(new_text))
        for key in acc:
            acc[key].update(d[key])
    # The editor and runtime subsystems mirror most verbs, so the same break is found twice.
    # Report it once -- a duplicated list reads as two separate problems.
    return {key: sorted(vals) for key, vals in acc.items()}


def declared(base: str) -> bool:
    """Does any commit message in the pushed range declare the break?"""
    log = _git("log", "--format=%B", f"{base}..HEAD").upper()
    return any(m in log for m in MARKERS)


def report(d: dict[str, list[str]]) -> str:
    lines = ["", "[pre-push] BLOCKED: this push changes a plugin UFUNCTION signature and no",
             "           commit message says so.", ""]
    for verb in d["changed"]:
        lines.append(f"  changed: {verb}")
    for verb in d["removed"]:
        lines.append(f"  removed: {verb}")
    lines += [
        "",
        "Every project vendors its own copy of this plugin. A teammate syncs it weeks from now",
        "with no reason to suspect anything; callers on the old arity stop compiling, and any",
        "BlueprintCallable caller breaks only when that asset is next compiled. The commit",
        "message is the last thing anyone reads BEFORE they rebuild -- so it has to carry the",
        "break, not just the fix.",
        "",
        "Add a line like this to one of the pushed commits (git commit --amend / rebase -i):",
        "",
        "  BREAKING(plugin): <Verb> gained a required <Type> <Name> parameter.",
        "    Callers on the old arity break; these are BlueprintCallable, so Blueprint callers",
        "    break too, and only when that asset is next compiled.",
        "    Requires a plugin re-sync + rebuild (Restart-Editor.ps1) in every project.",
        "",
        "Then note it in docs/known-issues.md as well -- that is where it is read AFTER.",
        "Override (rare, and say why in the message): UAP_SKIP_SIGCHECK=1 git push",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("UAP_SKIP_SIGCHECK"):
        return 0
    base = argv[0] if argv else "origin/main"
    d = collect(base)
    if not (d["changed"] or d["removed"]):
        return 0
    if declared(base):
        return 0
    print(report(d))
    return 1


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
