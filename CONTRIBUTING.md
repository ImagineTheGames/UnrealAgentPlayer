# Contributing

Small repo, two moving parts, one rule that matters more than the rest.

```
Plugin/        UE plugin (C++). Every project vendors its OWN COPY of this.
mcp-server/    Python MCP server + `uap` CLI. Every project SHARES THIS ONE.
agent-testing/ The kit installed into a project (uap.ps1 template, command + AGENTS snippets).
docs/          Setup, architecture, capabilities, known issues.
```

## Before you push

```powershell
git config core.hooksPath .githooks        # once per clone
cd mcp-server
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pytest -q
```

The pre-push hook runs the same lint + tests CI runs, plus the signature check below.

A check that does not run the same thing as the gate it stands in for is not a check. The hook
and `ci-python` run the same `ruff check src tests`, which is necessary and was not sufficient:
`ruff>=0.5` let the hook lint with the venv's ruff while CI installed a newer one with a wider
default rule set, so the hook said "clean" and CI failed on the identical command. The dev extra
now pins `ruff==0.16.5` exactly. If you add a local gate, pin what it runs -- and if you bump the
pin, run `ruff check src tests` before pushing, because the new version is allowed to disagree.

Two more of the same shape were hiding behind that lint failure, both invisible locally.
`mcp>=1.2.0` let CI resolve mcp 2.x, which renamed the 1.x `inputSchema` field every Tool in
`registry.py` is built with - now bounded `<2`. And the installer tests run `pwsh` in CI but
fall back to Windows PowerShell 5.1 on a machine without it; 5.1 accepts a bare backtick-u in
a double-quoted string that pwsh 7.2+ rejects as a parse error, so a broken `Install-AgentTest.ps1`
passed here and failed there. That fallback now warns instead of standing in silently.

## The shape of this repo, and why it bites

A CLI change is live in every project the moment someone pulls. A plugin change reaches a
project only when that project **syncs and rebuilds**. So the two halves are never in step,
and **CLI/plugin skew is permanent and expected, not a transient state** (see
[docs/known-issues.md](docs/known-issues.md) #23, #27). Write for it:

- **Never call a new plugin verb bare from the CLI.** Route it through `_rc_require(func,
  params, project, needs)` so a project that has not rebuilt gets "this editor's plugin has no
  `<Verb>` (`<what it is for>`) ... sync and rebuild `<project>`" instead of a raw
  RemoteControl 404 that reads like a broken editor.
- **Fall back only to a verb that answers the SAME question.** Never to one that answers a
  different question more conveniently.
- **`uap status` reports the gap up front.** It compares the UFUNCTION declarations in this
  repo's plugin headers against what the editor actually exports (see
  `mcp-server/src/unreal_agent_player/contract.py`). Both sides are derived, so neither is a
  version number anyone has to remember to bump. Nothing to do when you add a verb -- the
  header IS the declaration.

## Merging a PR from a branch you are still pushing to

Merging a PR does not close the branch, and everything pushed to that branch AFTER the merge
commit is silently orphaned: it sits on the branch, outside `main`, with the PR showing as
merged and nothing indicating anything is missing.

Worse, **the CI trigger goes quiet.** With `ci-python` on `pull_request`, a merged PR means
later pushes to that branch run no checks at all - and nothing says so. That is a gate that
stopped applying without announcing it, which is the same failure family as everything in
[docs/known-issues.md](docs/known-issues.md): a check that is not running looks identical to
a check that is passing.

This happened on 2026-08-28: PR #1 merged, three further commits landed on the branch - two
docs commits that went through no CI at all, and the fix that unblocked another project's
verification - and none of them reached `main` until someone thought to look.

So: **after merging, either stop pushing to that branch or open the next PR immediately.**
Before merging, check what has landed since the PR was opened (`git log --oneline
origin/main..<branch>`), not just that the PR is green.

## Changing a signature

**If you change a signature, say so where someone will read it BEFORE they rebuild -- not
only where they would read it after.**

A changelist or commit that describes the fix but not the break is a silent trap with a
delayed fuse. Every other trap catalogued in `docs/known-issues.md` is one you spring on
yourself, in the session that made it, with the context still in your head. This one is
different: the victim is a teammate weeks later, on another project, who synced the plugin for
an unrelated reason, has no reason to suspect anything, and gets a compile error (or a
Blueprint node that breaks only when that asset is next compiled) with nothing connecting it
to your change.

The commit message is the last thing anybody reads before they rebuild. `docs/known-issues.md`
is where it is read *after*. Both, in that order.

Real example, and the one the check was written for: `InjectAxis`, `InjectGamepad` and
`HoldAxis` gained a required `FString SlateUser` parameter
([#26](docs/known-issues.md)). They are `BlueprintCallable`, so every caller on the old
arity breaks -- C++ at compile time, Blueprint whenever that asset is next compiled.

Write it like this, in the commit that makes the change:

```
BREAKING(plugin): InjectAxis/InjectGamepad/HoldAxis gained a required FString SlateUser

Callers on the old arity break. These are BlueprintCallable, so Blueprint callers break
too, and only when that asset is next compiled.
Requires a plugin re-sync + rebuild (Restart-Editor.ps1) in every project.
```

The pre-push hook parses the plugin's public headers on both sides of the push and blocks if a
UFUNCTION signature changed or disappeared and no pushed commit message contains `BREAKING`
(or `signature change`). Adding a NEW verb is not blocked -- that direction is already handled
by `_rc_require`, which names it as skew rather than letting it 404. Override with
`UAP_SKIP_SIGCHECK=1 git push`, and say why in the message if you do.

## Failure messages

Three parts, always (`agent-testing/agentplayertest.md`, "What a good failure message
contains"):

1. **Name the mismatch** -- and cite it where a citation exists (`AnalogCursor.cpp:192`).
2. **Say why the obvious retry is wrong.** This is the part people drop and the part that
   matters: dropping `--user` and retrying lands on a route that answers `ok` and does nothing.
3. **Give the exact remedy**, with the values needed to act on it.

A silent wrong answer is worse than a missing verb: a missing verb 404s and somebody notices.

**Know where this stops working.** A response field also implies a SHAPE, and callers act on the
shape before they read the hint: `uap pie start` answered `queued: true, confirmed: false,
next: "uap pie wait <seconds>"` -- every field true -- and agents read "async job", built
background watchers and ended their turns over a 1-5 second call, three times in the day after
that shipped ([#29](docs/known-issues.md)). Response-field guidance reaches an agent already in a
failure state, hunting for why; it does not reach one confidently proceeding. **Wrong-but-confident
needs the DEFAULT changed so the wrong path is not reachable** -- which is why `pie start` now
blocks instead of advising you to wait. A hint is not a default. Full write-up: "The limit of
putting the answer in the response" in `agent-testing/agentplayertest.md`.

## Tests

`mcp-server/tests/`. Pytest, no live editor. Conventions worth copying:

- The docstring says what the test *protects*, with the incident it came from. These read as
  the repo's memory, not as coverage.
- Where behaviour is authored in C++ that pytest cannot execute, assert on the **source text**
  (`test_input_slate_user.py::test_plugin_source_carries_that_refusal_verbatim`). That is what
  stops a hard-won failure message decaying back into `return false`.
- Anything reaching a live editor must be stubbed. `conftest.py` defaults
  `cli._live_contract` to `None` so a real editor on the developer's machine can never change
  what the suite asserts.
