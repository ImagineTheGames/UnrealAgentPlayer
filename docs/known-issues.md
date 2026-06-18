# Known issues (UE 5.7, plugin v0.0.1)

Field findings from driving agent tests on UE 5.7. Each entry has a concrete repro and a
status. "Fixed" means corrected in this repo; "Open" means it still needs a code change.

## What works well

`uap status`, `uap exec`, `uap rc`, and the Remote Control bridge are reliable. `uap exec`
is a dependable Python channel into the live editor and is the recommended escape hatch --
on 5.7 it also sidesteps a broken UE multicast remote-exec node.

## 1. Documented PIE start/stop API does not exist on 5.7 -- FIXED (docs)

Older guidance used:

```python
unreal.get_editor_subsystem(unreal.PlayWorldEditorSubsystem).play_in_viewport()
unreal.get_editor_subsystem(unreal.PlayWorldEditorSubsystem).request_stop_play_in_editor()
```

On 5.7 this throws `AttributeError: module 'unreal' has no attribute 'PlayWorldEditorSubsystem'`.
Verified live: `hasattr(unreal, 'PlayWorldEditorSubsystem')` is `False`. The correct 5.7 calls
are on `LevelEditorSubsystem`:

```python
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
les.editor_request_begin_play()
les.editor_request_end_play()
les.is_in_play_in_editor()   # poll this to know PIE is actually up
les.editor_play_simulate()   # simulate mode
```

Verified live on 5.7: all four exist (`True`). Fixed in `agent-testing/agentplayertest.md`.
The real fix is #2 (don't make agents touch the raw subsystem at all).

## 2. No agent-facing PIE start/stop in the plugin -- OPEN

The plugin only OBSERVES PIE over RC (`GetPIEPhase`, `GetPIEElapsedSeconds`); there is no
`StartPIE`/`StopPIE` RC function. So the agent is forced to use the version-fragile raw
engine API for the single most error-prone step -- exactly where the docs were wrong (#1).

Proposed: add `uap pie start` / `uap pie stop` / `uap pie wait <seconds>` verbs backed by
new RC functions that wrap the version-correct engine call (`editor_request_begin_play` /
`editor_request_end_play`, polling `is_in_play_in_editor`). Agents then never touch the raw
subsystem, and the version detail lives in one C++ place.

## 3. A test can PASS with zero screenshot (false positive) -- OPEN

The flow says screenshots are required as evidence and "a verification is not done until the
report exists," but nothing enforces a screenshot was actually captured. `uap report finish
pass` succeeds with no screenshot attached -- a false positive.

Proposed: `report finish pass` should refuse (or auto-downgrade to `fail`) when the run
required visual proof and no screenshot is attached. Needs a way to mark a report/assertion
as "requires visual proof" so the gate only fires when relevant.

## 4. `uap screenshot` silently "succeeds" with no file -- OPEN

`CaptureViewportWithUI` / `uap screenshot` called without a rendered game frame (no PIE, or
an idle editor viewport) returns `{ok:true, exists:false}` and writes nothing, with no reason.
It reads exactly like a transient. `CaptureViewportWithUI` writes "on the next rendered
frame" -- fine -- but an idle Editor viewport never renders that frame, so it hangs as a no-op.

Proposed: return `ok:false` with a clear reason ("requires active PIE / a renderable frame"),
and have the CLI poll for the file with a timeout and hard-fail if it never lands. (The CLI
already polls ~8s and reports `exists`; the body should report `ok:false` when the file never
appears, and the RC layer should explain why.)

## 5. Screenshot examples imply they work anytime -- FIXED (docs)

`uap screenshot shot.png` appears in the general flow as if it works at any time, but it
requires a composited game frame (PIE running). Stated explicitly in
`agent-testing/agentplayertest.md` step 7.

## Priority

1 (done) and 5 (done) are docs. 2, 3, 4 are code changes that remove the remaining
false-positive / fragile-API footguns; 2 is the highest-value (it also retires the cause of 1).
