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

---

## Input injection

All input is injected **in-process via Slate** into the focused PIE viewport. It does not require the editor to be the OS-foreground window, and key/button down-events stay "held" until a matching up-event (so movement keys produce continuous motion).

| Tool | Inputs | Notes |
| --- | --- | --- |
| `input_key` | `key` (FKey name e.g. `W`, `SpaceBar`), `pressed` (bool), `repeat` (bool) | Down without a later up = held. |
| `input_mouse_move` | `x`, `y`, `absolute` (bool) | Absolute viewport coords or relative delta. |
| `input_mouse_button` | `button` (Left/Right/Middle), `pressed` | |
| `input_axis` | `axis` (name), `value` (float) | Analog axis input. |
| `input_gamepad` | `button` (enum), `pressed`, `analog_value` | Buttons and analog sticks/triggers. |
| `input_sequence` | list of input steps | Convenience: run an ordered batch of the above. |

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

## Project test helpers

A project can expose its own assertion functions (C++ `UFUNCTION`s or Blueprint functions) tagged for discovery. The plugin finds them; the agent calls them by name.

| Tool / UFUNCTION | Purpose |
| --- | --- |
| `helper_list` (`ListTestHelpers`) | Enumerate discovered helpers (name, params, return type). |
| `CallTestHelper` | Call one by name with JSON args; returns the JSON result. |

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

## Plugin UFUNCTION surface

The C++ functions on `UAPAgentSubsystem`, callable directly over Remote Control:

`GetPluginVersion` · `ExecuteConsoleCommand` · `FocusEditorWindow` · `GetPIEPhase` · `GetPIEElapsedSeconds` · `GetLogCursor` · `GetLogsSince` · `InjectKey` · `InjectMouseMove` · `InjectMouseButton` · `InjectAxis` · `InjectGamepad` · `InjectXRButton` · `InjectXRControllerPose` · `ClearXRControllerOverride` · `ListTestHelpers` · `CallTestHelper` · `GetStatGroupText`

New tools can be added in the Python layer over `exec_python` and the existing UFUNCTIONs **without recompiling the engine**; only genuinely new in-process capabilities require a plugin change.
