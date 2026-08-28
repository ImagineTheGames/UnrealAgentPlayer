# Capabilities reference

The full agent-facing surface of UnrealAgentPlayer: every MCP tool and every plugin `UFUNCTION`, what it does, its inputs, and what it returns.

Two layers:

- **MCP tools** — what an MCP client (the agent) calls. Listed by family below.
- **Plugin UFUNCTIONs** — the C++ surface on `UAPAgentSubsystem`, reachable directly over Remote Control HTTP. Most MCP tools are thin wrappers over these; a few tools use Python Remote Execution instead.

All MCP tools return a JSON envelope. On failure they return a structured error (`code`, `message`, optional `retry_hint`) rather than throwing, so an agent can branch on the result.

---

## Status

### `bridge_status`
Health of the whole bridge in one call.
**Returns:** `ue_running`, `rc_reachable`, `remote_exec_reachable`, `plugin_version`.
Use this first — it tells the agent whether the editor is up and which channels are live before attempting anything else.

---

## PIE (Play-In-Editor)

| Tool | Purpose |
| --- | --- |
| `pie_start` | Begin a PIE session (`editor_request_begin_play`). |
| `pie_stop` | End the PIE session. |
| `pie_pause` | Pause PIE. |
| `pie_resume` | Resume a paused PIE session. |
| `pie_status` | Current phase + elapsed seconds. |

**`pie_status` returns:** `phase` (`NotPlaying` / `Playing` / `Paused` / `Ending`), `elapsed_seconds`.

> PIE starts the level currently open in the editor. To play a specific map, load it first via `exec_python` (`LevelEditorSubsystem.load_level("/Game/...")`).

### VR Preview (`StartPIEMode`, `uap pie start --mode vr`)

`uap pie start` gives **flat PIE**, which never takes the HMD code path. Two whole classes of bug are invisible there:

- **OpenXR input.** The XR input device only exists in a VR play session; injected XR keys behave differently (notably, OpenXR-fed actions ignore `bConsumeInput`).
- **`IsHeadMountedDisplayEnabled()` branches.** A screen that spawns a world-space kiosk + laser pointers on HMD but falls back to `AddToViewport` in flat PIE is a different screen in each mode.

`uap pie start --mode vr` requests `EPlaySessionPreviewType::VRPreview`. It **requires a connected headset** and fails with a concrete reason when there is none (`no XR system loaded` / `no HMD connected`) rather than silently starting flat PIE — a silent fallback would make an HMD-only bug look absent.

It also **requires a plugin copy new enough to export `StartPIEMode`**. Against an older copy the CLI refuses with "sync and rebuild <project>" rather than falling back to `StartPIE`, which can only start flat PIE. Flat `uap pie start` *does* fall back to `StartPIE` automatically, since that starts the identical session -- see "CLI/plugin version skew" below.

State reads, `exec`, `read-ui` and screenshots behave the same in VR Preview as in flat PIE: they address the same PIE world and game viewport. The visible difference is the render path (stereo, spectator screen) and the input device set.

---

## Input injection

Key/button input is injected **in-process** through the game viewport client
(`UGameViewportClient::InputKey`), the same path the engine uses for a real keypress. It
does not require the editor to be the OS-foreground window (an earlier Slate keyboard-focus
path did, and silently dropped keys when backgrounded). Analog axis samples take the same
viewport path (`UGameViewportClient::InputAxis`); mouse and gamepad *buttons* stay on Slate
(a gamepad face/DPad press is also how UMG focus navigation is driven).

A down-event latches in `UPlayerInput` until a matching up-event — **but do not rely on that
for a hold.** Any `APlayerController::FlushPressedKeys` (input-mode change, focus loss, PC
recreation) clears it silently, and the CLI round-trip is ~1s, so you cannot notice or repair
it in time. Use the sustained-input verbs below.

| Tool | Inputs | Notes |
| --- | --- | --- |
| `input_key` | `key` (FKey name e.g. `W`, `SpaceBar`), `pressed` (bool), `repeat` (bool) | One event. For a sustained hold use `HoldKey` / `uap input hold` — see below. |
| `input_mouse_move` | `x`, `y`, `absolute` (bool) | Absolute viewport coords or relative delta. |
| `input_mouse_button` | `button` (Left/Right/Middle), `pressed` | |
| `input_axis` | `axis` (name), `value` (float) | One analog sample. Routed through the game viewport (`UGameViewportClient::InputAxis`), not Slate. |
| `input_gamepad` | `button` (enum), `pressed`, `analog_value` | Buttons and analog sticks/triggers. |
| `input_sequence` | list of input steps | Convenience: run an ordered batch of the above. |

### Sustained input (held keys, analog sticks)

A single injected event **cannot** drive sustained locomotion from outside the editor:

- The CLI round-trip is ~1s, so re-injecting per poll cannot cover a sub-second window (a 0.6s AI wind-up, say).
- Anything that calls `APlayerController::FlushPressedKeys` — an input-mode change, focus loss, a player-controller recreation — silently drops a latched key, and the caller gets no signal. Injecting only one `IE_Pressed` and never an `IE_Repeat` means the engine's own re-latch path (`bAutoReconcilePressedEventsOnFirstRepeat`) never fires either, so the key stays dead.
- **An analog stick is not a button.** A real thumbstick re-sends its value every frame; a one-shot sample is not what the game sees.

So the plugin holds the input **in-engine**, re-asserting it once per frame on the core ticker until the duration expires, then releasing cleanly (digital → `IE_Released`, analog → recentre to 0). Holds are dropped automatically when the game viewport goes away (PIE ended).

| UFUNCTION | CLI | Inputs | Notes |
| --- | --- | --- | --- |
| `HoldKey` | `uap input hold <Key> --seconds N` | `KeyName`, `Seconds` | Press now, `IE_Repeat` every frame, auto-release. |
| `HoldAxis` | `uap input axis <AxisKey> <v> --seconds N` | `AxisKeyName`, `Value`, `Seconds` | **The VR locomotion verb** — e.g. `OculusTouch_Left_Thumbstick_Y 1.0`. |
| `ReleaseHeldInput` | `uap input release [<Key>]` | `KeyName` (empty = **recovery**) | See below. |
| `GetHeldInput` | `uap input status` | — | `{"held":[{key, analog, value, remaining_seconds, down}]}` |

All four return a JSON envelope so a refusal can say *why*. Key names are exact FKeys resolved against **the engine's own registry** (`FKey::IsValid`) — `W`, `C`, `LeftControl`, `SpaceBar`, `Gamepad_LeftY`, `OculusTouch_Left_Thumbstick_Y` all work; there is no hand-maintained allow-list to drift out of date.

`uap input hold/axis` returns **immediately** by default — the hold continues in-engine, which is the point: you read game state *while* it is held. Pass `--wait` to block until it expires.

**A refused call presses nothing.** Validation (key name → duration → live viewport) happens entirely before any injection, and the refusal envelope carries `"pressed": false` so a caller can assert it. If a later step fails, the hold is unwound rather than left half-started.

### Recovery: `uap input release`

With no key, this is the escape hatch: it releases every registry entry **and** calls `APlayerController::FlushPressedKeys` on every local controller, clearing any key the engine still has down that the registry does not know about. Reach for it whenever input starts behaving oddly — a single stuck key silently corrupts every later test in the same PIE session, and this clears it without restarting PIE. With a key name it force-releases that key whether or not it is in the registry.

`uap input status` reports `down` (engine ground truth) next to each registry entry. If those ever disagree, run `uap input release`.

> **Return values mean "delivered", not "handled".** `UGameViewportClient::InputKey` forwards `UPlayerInput::InputKey`, which for `IE_Pressed` returns `IsKeyHandledByAction()` — a lookup in the **legacy** `ActionMappings` array only. A project on Enhanced Input has no legacy mappings, so a perfectly delivered keypress reports `false`. The plugin therefore ignores that bit and reports whether the event reached the game.

### VR input

| Tool | Inputs | Notes |
| --- | --- | --- |
| `input_xr_button` | `hand` (Left/Right), `button` (FKey e.g. `OculusTouch_Left_X_Click`), `pressed` | Touch buttons are ordinary FKeys → same Slate path as `input_key`. **Works headset-free.** |
| `input_xr_pose` | `hand`, `position` (vec3), `orientation` (rotator), `tracked` (bool) | Sets the pose returned by the plugin's fake `IMotionController`. |
| `input_xr_clear` | `hand` | Stop overriding that hand; real devices win again. |

> **VR pose caveat:** in HMD-less PIE with an active OpenXR runtime, the runtime's own motion controller can win modular-feature precedence, so an injected pose may not move the live component. Button injection is unaffected. See [architecture.md](architecture.md).

---

## Logs

Cursor-based capture from a ring buffer inside the plugin — the agent reads only what's new since its last cursor, with filtering.

| Tool | Inputs | Returns |
| --- | --- | --- |
| `log_tail` | `max_lines`, `category_filter`, `min_verbosity` | The most recent N lines (+ a cursor). |
| `log_since` | `after_cursor`, `max_lines`, `category_filter`, `min_verbosity` | Everything since a cursor — the loop-friendly form. |

`min_verbosity` filters by `Error` / `Warning` / `Display` / `Log` / `Verbose`. Pattern: grab a cursor before injecting input, then `log_since` that cursor to see exactly what the action produced.

### From the CLI

```
uap log cursor                              # grab this BEFORE driving the condition
uap log since <cursor> --grep "Janitor|Catch"
uap log tail --lines 200 --grep "Error" --category LogUAP --verbosity Warning
```

All three go through the same per-project RC targeting as every other verb, so they read the **same editor** you are driving — and the lines land in the report's timeline instead of being tailed out-of-band with shell tools. `--grep` is a case-insensitive regex over the message.

> This reads the plugin's in-process ring buffer (4096 lines, populated from subsystem init onward), **not** `Saved/Logs/<Project>.log`. For a whole-session history, read that file directly.

---

## Frame-rate property sampling

The finest granularity reachable from outside the editor is one exec round-trip — roughly **one second**. That is useless for anything sub-second: animation judder, a 0.6s AI wind-up, a one-frame pop. So the plugin records the value **in-engine, once per frame** for a bounded window and hands the whole series back in one call.

| UFUNCTION | CLI | Purpose |
| --- | --- | --- |
| `StartPropertySample` | `uap sample start <object> <property> --seconds N` | Begin a series. Returns `{ok}` or `{ok:false, error}`. |
| `ReadPropertySample` | `uap sample read` | `{active, object, property, count, samples:[{t, v}]}` |
| `StopPropertySample` | — | Stop early, keeping what was collected. |

**`<object>`** — a full object path (`/Game/...`), an actor name (exact, else substring) in the live game world, or one of `PlayerPawn` / `PlayerController` / `PlayerCameraManager`.

**`<property>`** — a dot-separated `FProperty` chain that walks through object *and* struct properties, with a component name usable as a step (`CharacterMovement.Velocity`, `CameraComponent.RelativeLocation`). The final step may instead be a computed leaf: `WorldLocation`, `WorldRotation`, `WorldScale`, `WorldTransform`, `ForwardVector`, `Velocity`.

`uap sample start` blocks for the window by default, then returns the series plus derived `stats`: `delta_mean`, `delta_max`, `delta_p95` (the frame-to-frame movement of the value) and `hz`. Pass `--no-wait` to return immediately and collect later with `uap sample read`, or `--summary` to drop the raw series.

```
# Prove a VR player's camera no longer judders while being carried
uap sample start PlayerCameraManager WorldLocation --seconds 2
# -> stats.delta_p95 is the per-frame jitter. A spiky p95 with a calm delta_mean IS the judder.
```

One series at a time; a new `start` replaces the old. Windows are clamped to 60s / 20000 samples — this holds every sample in memory and is meant for short measurements.

---

## Performance

| Tool | Purpose |
| --- | --- |
| `perf_stat` | Parse `stat unit` / `stat fps` — frame, game, draw, GPU, and render-thread milliseconds. Draw/GPU/RT come from real engine timers (`GGPUFrameTime`, `GRenderThreadTime`), not estimates. |
| `perf_trace_start` / `perf_trace_stop` | Start/stop an Unreal Insights trace. |

### Perf regression baselines

| Tool | Purpose |
| --- | --- |
| `perf_baseline_save` | Snapshot current metrics under a name into a JSON store. |
| `perf_baseline_compare` | Re-read metrics and flag any that exceed the named baseline by more than `tolerance_pct` (default 10%). |

Pure Python — no engine change. Use it to fail a change that regresses the frame budget.

---

## Console & Python

| Tool | Purpose |
| --- | --- |
| `exec_console` | Run any UE console command; returns captured output. |
| `exec_python` | Run arbitrary `import unreal; ...` in the editor via Python Remote Execution. Returns `success` + captured stdout. |

`exec_python` is the escape hatch for anything not exposed as a tool: load a level, read a pawn transform, possess a pawn, spawn an actor, query subsystems.

> **Multi-editor targeting.** Python Remote Execution is multicast; if more than one editor is open, the first responder answers. The client supports a project-substring filter so you can target a specific editor. Remote Control (HTTP, per-port) is unambiguous by port.

---

## Actors

| Tool | Inputs | Purpose |
| --- | --- | --- |
| `actor_find` | class / name filter | Find actors in the current world. |
| `actor_get_properties` | actor, property names | Read properties. |
| `actor_set_property` | actor, property, value | Write a property. |

Useful for asserting on world state ("did the door actor's `bIsOpen` flip?") and for setting up a scenario before injecting input.

---

## Read on-screen UI (UMG)

### `read_viewport_ui`
Reads the live UMG layer of the running PIE game so the agent can **see** prompts, labels, and menu buttons — and respond to them — instead of injecting input blind.

**Inputs:** none.
**Returns:**
```json
{
  "available": true,
  "count": 14,
  "focused": "Next >",
  "texts": [
    {"text": "Press E to open", "x": 960.0, "y": 540.0, "focused": false},
    {"text": "Next >",          "x": 1394.0, "y": 950.0, "focused": true}
  ]
}
```
- `texts` — every visible UMG text element (`UTextBlock`/`UCommonTextBlock` subclasses + `URichTextBlock`), with absolute screen-pixel position. Unpainted/off-screen ghosts (position `0,0`) are filtered out.
- `focused` — the text under the currently keyboard/user-focused widget (the highlighted menu item in gamepad/CommonUI navigation), or `""`.
- `available` — `false` when there is no active PIE session.

Use it to read a prompt's required key (`"Press E"` → inject `E`), enumerate menu buttons, and know which one is highlighted before navigating. Backed by the plugin's `DumpViewportUI` UFUNCTION (UMG layer, so it captures what `HighResShot` screenshots cannot).

---

## Screenshots

### `screenshot_viewport`
**Inputs:** `resolution` (e.g. `1920x1080`), filename, flags.
**Captures** the game viewport via `HighResShot`.

> **Limitation:** `HighResShot` does **not** include the UMG layer, so on-screen UI/HUD/prompts will not appear in the image. To *read* on-screen UI, use **`read_viewport_ui`** above (deterministic text, no pixels). A backbuffer/desktop capture path for pixel-level UI (icons/glyphs with no text) is still on the roadmap.

---

## Run reporting

Capture an agent run into a self-contained, tabbed HTML report and open it.

| Tool | Purpose |
| --- | --- |
| `report_start` | Begin a run report (task, optional project). Auto-captures every subsequent tool call + screenshots/perf/logs/env until finish. |
| `report_assert` | Record a pass/fail check with evidence. |
| `report_note` | Add a curated note (optional section). |
| `report_caption` | Caption a gallery screenshot (by index/filename; omit for most recent). |
| `report_finish` | `verdict` (`pass`/`fail`) + `summary`; renders `index.html` (Overview/Screenshots/Timeline/Diagnostics tabs) and auto-opens it. |

Reports are written to `~/.uap-reports/<timestamp>__<task>/` (override with
`UAP_REPORTS_DIR`): `index.html` + `screenshots/*.png` + `data.json`.

---

## Project test helpers

A project can expose its own assertion functions (C++ `UFUNCTION`s or Blueprint functions) tagged for discovery. The plugin finds them; the agent calls them by name.

| Tool / UFUNCTION | Purpose |
| --- | --- |
| `helper_list` (`ListTestHelpers`) | Enumerate discovered helpers (name, params, return type). MCP path only — it uses `/remote/object/call`, which serializes the struct correctly. |
| `ListTestHelpersJson` | Same list as a JSON string. **This is what a CLI must use** (see below). |
| `CallTestHelper` | Call one by name with JSON args; returns the JSON result. |

From the CLI: `uap helpers [--grep RE] [--names]`. `uap rc ListTestHelpers` also works — it is transparently routed to `ListTestHelpersJson`.

> **Why a JSON twin exists.** RemoteControl's *preset-call* route (`/remote/preset/<p>/function/<f>`, which the CLI uses) serializes the return value through a property filter that only admits the function's own out/return params. Every nested field of a returned struct is therefore dropped, and `ListTestHelpers` comes back as `[{}, {}, ...]` — the right number of helpers with zero usable information. The `/remote/object/call` route does not have this bug (its filter admits nested properties), which is why the MCP tool is unaffected. Any UFUNCTION that must return structured data to the CLI should return a JSON `FString`, as `DumpViewportUI`, `GetLogsSince` and `CallTestHelper` already do.

This is how the agent reads *game-specific* truth — `GetPlayerHealth()`, `IsDoorOpen("Front")`, `HasCompletedTutorial()` — without the plugin knowing anything about your game. See [writing-test-helpers.md](writing-test-helpers.md) and [`examples/`](../examples).

---

## Editor menus (Windows UIAutomation)

For driving the **native editor chrome** (menu bar), not in-game UI.

| Tool | Purpose |
| --- | --- |
| `ui_find_window` | Locate an editor window by title. |
| `ui_list_menus` | Enumerate available menu paths. |
| `ui_menu_click` | Click a menu path, e.g. `["Window", "Viewports", "Viewport 1"]`. |

Requires the `[windows]` extra (`comtypes`). Import-guarded: on a platform/without `comtypes`, these return a `UIA_UNAVAILABLE` envelope instead of failing hard.

---

## Editor focus

### `FocusEditorWindow` (UFUNCTION)
Brings the editor's main window to the foreground **from inside the editor process**, so an external agent can make the editor frontmost before capturing or driving native chrome. Because it runs in-process it bypasses the Win32 cross-process foreground lock; it title-matches the main editor window (not a transient dialog) and uses an `AttachThreadInput` + `AllowSetForegroundWindow` sequence to take focus reliably. Windows only.

> Note: in-process **input injection does not need this** — it routes through Slate regardless of OS focus. `FocusEditorWindow` is for screen capture and for driving native menus, which do need real OS focus. Only one window can be OS-foreground at a time, so this is a single-editor operation.

---

## Standalone game instances

Launch and drive standalone `-game` processes independently from the editor, each on its own Remote Control port.

| Tool | Inputs | Purpose |
| --- | --- | --- |
| `game_launch` | `port` (int), `map` (optional), `extra_args` (list, optional) | Spawn a `-game` process with `-RCWebControlEnable -UAPRCPort=<port>`, wait for RC to become reachable, return `instance_id` + `port`. |
| `game_attach` | `port` (int) | Register an already-running game process by port; returns `instance_id`. |
| `game_list` | — | Return all live instances (id, port, pid). |
| `game_stop` | `instance_id` | Terminate the managed instance and deregister it. |

**`target` parameter on play tools:** all input injection, log, perf, console, actor, and screenshot tools accept an optional `target` argument. Pass an `instance_id` (from `game_launch`/`game_attach`) to route the call to that standalone process; omit (or pass `"editor"`) to address the editor as usual. Multiple standalone instances and the editor are addressed independently by their RC port.

**Runtime object path:** the subsystem inside a standalone game is at `/Script/UnrealAgentPlayerRuntime.Default__UAPAgentRuntimeSubsystem`.

**Requirements:** `-game` must be launched from the editor binary (built `WITH_EDITOR`); the `UnrealAgentPlayerRuntime` module (Type `DeveloperTool`, `PlatformAllowList: [Win64]`) loads automatically in Development builds and is excluded from Shipping and Android.

---

## Plugin UFUNCTION surface

The C++ functions on `UAPAgentSubsystem`, callable directly over Remote Control:

`GetPluginVersion` · `ExecuteConsoleCommand` · `GetRemoteControlPort` · `FocusEditorWindow` · `GetPIEPhase` · `GetPIEElapsedSeconds` · `StartPIE` · `StartPIEMode` · `StopPIE` · `IsInPIE` · `GetLogCursor` · `GetLogsSince` · `InjectKey` · `InjectMouseMove` · `InjectMouseButton` · `InjectAxis` · `InjectGamepad` · `InjectXRButton` · `InjectXRControllerPose` · `ClearXRControllerOverride` · `HoldKey` · `HoldAxis` · `ReleaseHeldInput` · `GetHeldInput` · `ListTestHelpers` · `ListTestHelpersJson` · `CallTestHelper` · `StartPropertySample` · `ReadPropertySample` · `StopPropertySample` · `GetStatGroupText` · `DumpViewportUI` · `SelectTab` · `NavigateUI` · `CaptureViewportWithUI`

The same surface (minus the editor-only PIE and focus verbs) exists on `UAPAgentRuntimeSubsystem` for standalone `-game` processes.

New tools can be added in the Python layer over `exec_python` and the existing UFUNCTIONs **without recompiling the engine**; only genuinely new in-process capabilities require a plugin change.

### CLI/plugin version skew

Every project vendors its **own copy** of the plugin, while all of them share **one** CLI (each project's `uap.ps1` resolves this repo). The CLI therefore updates the instant it is pulled; a plugin copy only catches up when that project syncs and **rebuilds**. Skew is permanent and expected.

A verb the plugin does not export is not a preset field, so RemoteControl answers `404 "Unable to resolve the preset field."` -- which reads like a broken editor. Rules for anyone adding a verb here:

- Never call a new verb bare from the CLI. Route it through `_rc_require(func, params, project, needs)` (or `_rc_json(..., needs=...)`), which reports "this editor's plugin has no `<Verb>` ... sync and rebuild `<project>`".
- Fall back to an older verb **only** when it answers the same question (flat `pie start` -> `StartPIE`; `helpers` -> `ListTestHelpers`). Never fall back to a verb that answers a *different* question -- that is why `--mode vr` refuses.
- Only a 404 is skew. A verb that exists and fails answers HTTP 200 with its own result, so a real failure is never masked by a fallback.

See known-issues #23.
