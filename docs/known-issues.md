# Known issues (UE 5.7 / 5.8, plugin v0.0.1)

Field findings from driving agent tests on UE 5.7 and 5.8. Each entry has a concrete repro and a
status. "Fixed" means corrected in this repo; "Open" means it still needs a code change.
Entries written against 5.7 were re-verified on 5.8 unless noted.

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

UPDATE (#11 supersedes the rebind half of this): the runtime *rebind* above had a flaw -- see #11.

## 11. Runtime RC rebind left a leftover listener -> ports tangled between editors -- FIXED

Was: the deterministic rebind from #7 happens AFTER WebRemoteControl has already auto-bound a
port at startup. The rebind goes through `URemoteControlSettings::OnSettingChanged`, and the
engine's HTTP server (`FHttpServerModule`) caches a listener per port and **never releases the
original** -- so every editor ended up serving on TWO ports: its per-project port (30079/30035)
PLUS a leftover default-range one (30010/30020). The leftover floated to whichever editor booted
first, so a bare `:30010` (or a stale cache) cross-targeted the wrong editor. Proven via netstat:
one editor listening on 30010 + 30020 + 30079 at once.

Fixed (config pin, no rebind):
- Each project pins its RC HTTP port in `Config/DefaultRemoteControl.ini` under the **UE5.7**
  section `[/Script/RemoteControlCommon.RemoteControlSettings]` (the pre-5.7
  `[/Script/RemoteControl.RemoteControlSettings]` section is IGNORED -- the class moved modules;
  a stale ini using it silently does nothing, which is why an earlier 30011 pin never applied):

      [/Script/RemoteControlCommon.RemoteControlSettings]
      RemoteControlHttpServerPort=30079    ; SchoolsOut (PBW uses 30035)

  WebRemoteControl now binds the pinned port at startup -- never the default 30010, so there is
  no startup-port listener to leak.
- `EnsureRCPortBound` now RESPECTS an already-bound configured non-default port (returns without
  rebinding), so the leak-on-rebind never triggers. The deterministic auto-assign remains only as
  a fallback for projects WITHOUT a pin (still on the default 30010).

Result: exactly ONE RC HTTP port per editor, boot-order irrelevant. Verified live: SchoolsOut
binds only 30079 (no 30010), `uap status` -> `rc_port: 30079`. (The separate RC *WebSocket* port,
default 30020, is unrelated -- the `uap` CLI uses HTTP only; pin
`RemoteControlWebSocketServerPort` per project too if you want netstat fully de-duped.)

ADOPTING THE PLUGIN IN A NEW PROJECT: add the `Config/DefaultRemoteControl.ini` pin above with a
port unique to that project (the old `30011 + crc32(name)%80` value is a fine choice; or any free
3001x-3008x port). Without a pin the editor falls back to the leaky default-port path.

## 8. CLI default target cross-targeted the wrong editor -- FIXED

Was: the `uap` CLI hardcoded `--project` defaults to "SchoolsOut" (and `tools/game.py` defaulted
the editor exe / uproject to School's Out). So a command run WITHOUT `--project` from another
project's context targeted School's Out -- e.g. `uap pie start` started PIE in the wrong editor.

Fixed: CLI project defaults now read `$UAP_PROJECT` (empty -> first responder, never a hardcoded
project). The per-project `uap.ps1` launcher -- generated by `Install-AgentTest.ps1` -- bakes that
project's name into `$env:UAP_PROJECT` (and `$UAP_UPROJECT`), so a command run from a project's
launcher always targets THAT editor. The installer also resolves the project's engine editor exe
from its `.uproject` EngineAssociation (GUID -> registry Builds; version like "5.7" ->
EpicGames\Unreal Engine\<ver>\InstalledDirectory; or an explicit path) and bakes `$UAP_EDITOR_EXE`,
since different projects use different engines (custom fork vs stock UE) -- so the MCP
`game_launch` path spawns the right editor too. Verified: SchoolsOut -> IG_MetaEngine, PBW -> UE_5.7;
RC SchoolsOut launcher -> 30079, PBW launcher -> 30035, each with no `--project`. Caller-set env wins.

## 9. `uap screenshot` captured the wrong surface when an asset editor was open -- FIXED

Was: `CaptureViewportWithUI` used `FScreenshotRequest`, which grabs whatever window/tab is in the
FOREGROUND. Agents routinely open a Blueprint/WBP via `blueprint-mcp` mid-test; that tab comes to
the front, and the screenshot captured the Blueprint editor (node graph) instead of the game --
then "passed" on it. Embedded PIE made it worse: the game plays inside a level-viewport tab that an
asset-editor tab can fully occlude.

Fixed (two parts): (a) `CaptureViewportWithUI` now targets the PIE game viewport `SWidget` via
`FSlateApplication::TakeScreenshot` -- it draws THAT widget's own window (not the foreground one),
crops to the game view (no editor chrome), and writes the PNG synchronously. (b) `StartPIE` launches
PIE in its OWN floating window (`FRequestPlaySessionParams` with `DestinationSlateViewport` unset),
which can't be tab-occluded. Verified live in both editors: with a Blueprint editor open in front,
the shot shows the game (SchoolsOut classroom + VR hands; PBW hangar), not the node graph.

## 10. `uap exec` can hard-crash the editor (e.g. ExportDataTableToJSONString) -- GUIDANCE

`uap exec` runs Python IN-PROCESS, so a bad call crashes the whole editor (taking RC + the run with
it), not just the command. Observed in the wild: a Python call to the engine's
`DataTableFunctionLibrary.ExportDataTableToJSONString` `check()`-crashed in the JSON writer
(`Assertion failed: Stack.Top() == EJson::Object`, `JsonWriter.h:272`) -- the engine's DataTable
JSON exporter trips on certain row-struct shapes. This is an engine bug, not a plugin one, but the
plugin's exec path is how an agent hits it.

Guidance (docs + `uap help`): to read a DataTable, iterate rows (`row_names` + per-row reads),
never `ExportDataTableToJSONString`; treat any whole-asset `...ToJSONString` exporter as unsafe; and
prefer asset-author tooling (`blueprint-mcp`) over runtime `exec` for inspecting assets.

## 12. Tight-looping `uap exec` during PIE startup hard-crashes the editor -- GUIDANCE

Observed in the wild (burned 3+ editor restarts before it was pinned): polling `uap exec` (Python
remote-exec) in a tight loop -- e.g. the "poll `get_game_world()` until ready" recipe -- WHILE PIE
is still initializing crashes the editor with:

    Assertion failed: ++Queue(QueueIndex).RecursionGuard == 1  [TaskGraph.cpp:689]

Root cause: Python remote-exec runs ON THE GAME THREAD. Calling it repeatedly while PIE is mid-init
re-enters an engine task graph that is already running -> the recursion guard trips -> hard crash.
(This is the same assert earlier mis-attributed to an "EOS teardown race"; the real trigger is
game-thread re-entrancy during a PIE transition.)

Guidance: do NOT exec-poll during PIE startup/teardown. Wait for PIE by watching for the OS window
whose title contains the project name + "Preview", add a short settle delay, THEN start exec /
injecting input. A window-based wait never touches the game thread. `uap pie wait` should likewise
prefer a window-based signal over game-thread polling (follow-up). Reflected in `uap help`, the
/AgentPlayerTest common-mistakes block, and each project's AGENTS.md/CLAUDE.md.

## 13. Multiple agents on one editor stepped on each other / hard-failed -- FIXED

Was: several agents share ONE editor per project (one level, one PIE, and a rebuild takes it down).
An agent using `uap` while another rebuilt would hard-fail and stop, needing a human to say "editor
is free now"; two agents both wanting PIE / a rebuild collided.

Fixed: a per-project lease (`coordination.py`, file at `<reports>/.leases/<project>.json`, atomic via
an O_EXCL lockfile). Model: shared reads + one exclusive writer; acquire blocks (polls) until free,
evicting holders whose PID is dead OR whose heartbeat is stale (crash-safe). `Restart-Editor.ps1`
takes `exclusive(rebuild)` anchored to its own PID (reclaimed on exit) before touching the editor, so
a DIRECT script call coordinates too. Every editor-touching `uap` verb auto-waits through a rebuild
(identity-free `wait_while_rebuild`, fail-open). Holding PIE/level across calls is explicit
(`uap lease acquire exclusive --reason pie --agent <tok>` ... `release`), because this harness has NO
stable per-agent id (verified: `$PPID` is a shared `1`, shell PID changes every call) -- so a
standalone `lease acquire` is TTL-only (`--pid 0`) and needs a consistent `--agent` token. Rule lives
in each project's AGENTS.md/CLAUDE.md. Full design: `docs/agent-coordination.md`.

## 14. `GGPUFrameTime` removed in UE 5.8 -> plugin failed to compile -- FIXED

`UUAPAgentSubsystem::GetStatGroupText` read the RHI global `GGPUFrameTime` for the GPU timing it
reports through `uap report diag`. That global was deprecated in 5.6 (`UE_DEPRECATED(5.6, "Direct
use of GGPUFrameTime is deprecated. Call the global scope RHIGetGPUFrameCycles() function
instead.")`) and **removed outright in 5.8** -- it is gone from `Runtime/RHI/Public/RHIGlobals.h`.
On 5.8 the module does not build.

Fixed: call `RHIGetGPUFrameCycles()` (declared in `Runtime/RHI/Public/DynamicRHI.h`, so the file
now includes `DynamicRHI.h`). Verified present on stock 5.7, stock 5.8, and the Meta 5.7.3 fork,
and it is the documented replacement from 5.6 on -- so the single call is correct on every engine
this plugin supports. No `#if ENGINE_MINOR_VERSION` guard is needed, and on 5.7 it additionally
silences the deprecation warning.

Also dropped the unused `RemoteControlWebInterface` plugin dependency from `UnrealAgentPlayer.uplugin`.
The HTTP server the plugin actually needs is the `WebRemoteControl` *module*, which ships inside the
**RemoteControl** plugin (confirmed on 5.7, 5.8, and the Meta fork); `RemoteControlWebInterface` is a
separate plugin providing the browser UI, which this plugin never uses.

## Status

Items 1-9, 11, 13, 14 resolved (1 and 5 doc fixes; 2, 3, 4, 7, 8, 9, 14 code changes; 6 reporting;
11 config pin; 13 coordination lease). Items 10 and 12 are guidance -- both are engine-side crashes
surfaced through `exec` (a DataTable exporter bug; game-thread re-entrancy during PIE transitions).
