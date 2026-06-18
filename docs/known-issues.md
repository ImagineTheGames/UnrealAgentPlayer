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

## 2. No agent-facing PIE start/stop in the plugin -- FIXED

Was: the plugin only OBSERVED PIE over RC (`GetPIEPhase`, `GetPIEElapsedSeconds`); no
`StartPIE`/`StopPIE`, so the agent had to use the version-fragile raw engine API for the
single most error-prone step -- exactly where the docs were wrong (#1).

Fixed: added `StartPIE` / `StopPIE` / `IsInPIE` UFUNCTIONs on `UUAPAgentSubsystem`, wrapping
`ULevelEditorSubsystem::EditorRequestBeginPlay` / `EditorRequestEndPlay` / `IsInPlayInEditor`
(the version detail now lives in one C++ place). Exposed automatically via the preset, and
surfaced as `uap pie start` / `uap pie wait <seconds>` / `uap pie stop`. Verified live on UE
5.7: `is_in_pie False -> start_pie True -> is_in_pie True -> stop_pie True`.

## 3. A test can PASS with zero screenshot (false positive) -- FIXED

Was: nothing enforced that a screenshot was captured, so `uap report finish pass` succeeded
with no screenshot attached -- a false positive.

Fixed: `uap report start --require-screenshot` sets a per-report flag (persisted, survives the
cross-process start/finish split). `report finish pass` then auto-downgrades to `fail` and adds
an AUTO-FAIL note when no real screenshot is attached; `finish` emits `verdict` + `downgraded`
so the agent sees it. The gate only fires when the run opted into requiring visual proof.

## 4. `uap screenshot` silently "succeeds" with no file -- FIXED

Was: `uap screenshot` called without a rendered game frame returned `{ok:true, exists:false}`
and wrote nothing, with no reason -- it read exactly like a transient.

Fixed: after the ~8s poll, a missing file is now a hard `ok:false` with a concrete reason
("screenshot not written: CaptureViewportWithUI renders on the next game frame, but an idle
editor viewport never renders one. Requires active PIE (uap pie start) / a renderable frame.")
and a non-zero exit, so it can no longer be mistaken for a transient or a pass.

## 5. Screenshot examples imply they work anytime -- FIXED (docs)

`uap screenshot shot.png` appears in the general flow as if it works at any time, but it
requires a composited game frame (PIE running). Stated explicitly in
`agent-testing/agentplayertest.md` step 7.

## 6. CLI reports carried no diagnostics or perf (env/perf empty) -- FIXED

Was: the `uap` CLI reporting path never populated the report's env/diagnostics block OR the perf
(frame-rate) block. `_capture` only wrote timeline entries + screenshots; `set_env`/`set_perf` were
only ever called by the legacy MCP-tool path (`record_call`'s `bridge_status`/`perf_stat` branches),
which the CLI does not use. So every CLI-driven report finished with `env: {}` and `perf: None` --
no plugin version, level, PIE state, or frame timing, even though the render template has both
Environment and Perf sections. (And `uap status` would not have helped: in a multi-editor setup it
queries whatever holds :30010, often the wrong editor.)

Fixed: added `uap report diag --project <X>`, which sources diagnostics via `exec` (targets the
editor by project name, so it reads the RIGHT editor regardless of RC-port contention) and writes
both the env (`{plugin_version, world, is_in_pie, project, bridge}`) and perf (parsed from the
plugin's `GetStatGroupText`: `{frame_ms, game_ms, draw_ms, gpu_ms, fps}`). Call it while PIE is live
to record the game's frame rate. `report finish` warns when env is empty. Verified live: env +
perf populated (e.g. `frame_ms 333.33, draw_ms 5.57, gpu_ms 11.81, fps 3.0` -- the low fps was the
editor's background-CPU throttle, captured truthfully).

## 7. Two editors could not both run RemoteControl (port 30010 collision) -- FIXED

Was: RC HTTP is a single bind on port 30010 with no fallback. A second editor failed to bind
and had NO RC -- and `IWebRemoteControlModule::IsHttpServerRunning()` could not detect this: it
returns true even when the socket bind failed, so the second editor wrongly believed it owned
30010 (verified: netstat showed the first editor on 30010 while the second logged "running on
port 30010" yet listened on nothing). Python remote-exec already worked in both editors (UDP
multicast, addressed per-project); only RC was broken.

Fixed (editor): each editor assigns itself a DETERMINISTIC base port per project
(`30011 + crc32(projectName) % 80`, never the default 30010) and probes upward for the first
genuinely free TCP port, then rebinds RC to it -- so two DIFFERENT projects never collide, and a
socket bind test (reliable, unlike IsHttpServerRunning) is the only signal trusted.
`GetRemoteControlPort` reports the configured port only if a self socket-probe confirms it is
actually held (else 0), so a caller never drives the wrong editor.

Fixed (CLI): RC verbs (`status`/`rc`/`pie`/`read-ui`/`screenshot`) take `--project`; the RC port
is resolved per project by asking that editor (over exec) for `GetRemoteControlPort`, cached, with
a re-resolve on connection failure. `UAP_RC_PORT` still pins the port explicitly.

Note: editors no longer keep 30010 -- each uses its per-project port (e.g. SchoolsOut -> 30079).
External RC tooling hardcoded to 30010 should read the per-project port (via `uap status --project`).
Verified live on SchoolsOut (port 30079, RC reachable via `uap status --project SchoolsOut`).

## 8. CLI default target cross-targeted the wrong editor -- FIXED

Was: the `uap` CLI hardcoded `--project` defaults to "SchoolsOut" (and `tools/game.py` defaulted
the editor exe / uproject to School's Out). So a command run WITHOUT `--project` from another
project's context targeted School's Out -- e.g. `uap pie start` started PIE in the wrong editor.

Fixed: CLI project defaults now read `$UAP_PROJECT` (empty -> first responder, never a hardcoded
project). The per-project `uap.ps1` launcher -- generated by `Install-AgentTest.ps1` -- bakes that
project's name into `$env:UAP_PROJECT` (and `$UAP_UPROJECT`), so a command run from a project's
launcher always targets THAT editor. Verified: SchoolsOut launcher -> 30079, PBW launcher -> 30035,
each with no `--project`. Caller-set env still wins.

## Status

All eight resolved. 1 and 5 were doc fixes; 2, 3, 4 were code changes that retire the
fragile-API and false-positive footguns (2 also removes the cause of 1).
