# UnrealAgentPlayer

**Let an AI coding agent drive the Unreal Editor — start Play-In-Editor, inject real input, read logs and game state, capture screenshots, and run project-specific test helpers — to close the loop on its own changes.**

UnrealAgentPlayer is a thin C++ editor plugin plus an external Python [MCP](https://modelcontextprotocol.io) server. Together they give an agent (Claude Code, Claude Desktop, Cursor, or any MCP client) a structured, scriptable handle on a running editor: make a change, compile it, play it, drive it with keyboard/mouse/gamepad/VR input, observe what happened, and decide what to do next — without a human pressing keys.

- **Plugin** (`Plugin/`): a `UEditorSubsystem` that exposes a handful of `UFUNCTION`s to UE's built-in Remote Control, plus log capture, input injection, a fake motion-controller modular feature, and Blueprint test-helper discovery.
- **MCP server** (`mcp-server/`): a stdio MCP server that talks to the editor over Remote Control HTTP and Python Remote Execution, and presents ~30 agent-facing tools across 12 families. The Python layer grows new tools without recompiling the engine.

Supports UE 5.6, 5.7, and the Meta 5.7 fork. **Windows editor only** (see [Platform support](#platform-support)).

> **Status:** Functional and runtime-proven on real projects (see [Proven](#whats-proven-vs-known-gaps)). Versioned `0.0.1`; API may still change.

---

## Why

Agentic coding tools can write and compile UE C++ and Blueprints, but they're usually blind after the build: they can't tell whether the thing they changed actually *works in play*. Verifying a gameplay change normally means a human launches PIE, presses some keys, and eyeballs the result.

UnrealAgentPlayer turns that manual loop into an API:

```
edit → compile → pie_start → inject input → read logs / state / screenshot → assert → pie_stop
```

The agent perceives and acts inside the editor the way a tester would, so "does this actually work?" becomes a thing it can answer itself.

---

## Architecture

```
  MCP client (Claude Code / Desktop / Cursor)
        │  stdio (MCP)
        ▼
  Python MCP server  ── Remote Control HTTP ───────────►  ┐
  (~30 tools)        ── Python Remote Execution (UDP) ──►  │  Unreal Editor
                                                           │   ├─ UnrealAgentPlayer plugin
                                                           │   │   ├─ UAPAgentSubsystem (UFUNCTIONs)
                                                           │   │   ├─ log capture ring buffer
                                                           │   │   ├─ input injection (Slate, in-process)
                                                           │   │   ├─ fake IMotionController (VR pose)
                                                           │   │   └─ Blueprint helper discovery
                                                           │   ├─ Remote Control API (built-in)
                                                           │   └─ Python Editor Script Plugin (built-in)
                                                           ┘
```

- **Remote Control HTTP** (`127.0.0.1:30010`) — structured calls to the subsystem's `UFUNCTION`s. Deterministic, typed, per-port (so two editors are addressed independently).
- **Python Remote Execution** (UDP multicast `239.0.0.1:6766`) — arbitrary `import unreal; ...` scripting for anything not exposed as a `UFUNCTION` (load a level, read a pawn transform, start PIE).

Input injection is **in-process via Slate** — `FSlateApplication::ProcessKeyDownEvent` after focusing the PIE viewport — so it does **not** depend on the editor being the OS-foreground window, and two editors in separate processes can each be driven independently. See [docs/architecture.md](docs/architecture.md).

---

## Features

| Family | Tools | What it does |
| --- | --- | --- |
| **Status** | `bridge_status` | Liveness of the editor, Remote Control, and Python Remote Exec; plugin version. |
| **PIE** | `pie_start` `pie_stop` `pie_pause` `pie_resume` `pie_status` | Drive Play-In-Editor; query phase + elapsed time. |
| **Input** | `input_key` `input_mouse_move` `input_mouse_button` `input_axis` `input_gamepad` `input_sequence` | Inject keyboard / mouse / gamepad input into the running game (in-process, focus-independent). |
| **VR input** | `input_xr_button` `input_xr_pose` `input_xr_clear` | Inject Quest Touch buttons and controller poses for headset-free VR testing. |
| **Logs** | `log_tail` `log_since` | Cursor-based log capture with category + verbosity filtering. |
| **Perf** | `perf_stat` `perf_trace_start` `perf_trace_stop` | Read `stat unit`/`fps` (real draw/GPU/RT timings); Unreal Insights traces. |
| **Perf baselines** | `perf_baseline_save` `perf_baseline_compare` | Save named perf snapshots; flag regressions over a tolerance. |
| **Console** | `exec_console` | Run any console command. |
| **Python** | `exec_python` | Run arbitrary `import unreal` editor scripting (level load, state reads, etc.). |
| **Actors** | `actor_find` `actor_get_properties` `actor_set_property` | Introspect and tweak actors in the world. |
| **Read UI** | `read_viewport_ui` | Read on-screen UMG text + focus so the agent can respond to prompts/menus instead of guessing. |
| **Screenshots** | `screenshot_viewport` | Capture the viewport (HighResShot — see [gaps](#whats-proven-vs-known-gaps)). |
| **Test helpers** | `helper_list` + `CallTestHelper` | Auto-discover and call project-specific Blueprint/C++ assertion helpers (e.g. `IsDoorOpen`). |
| **Editor menus** | `ui_menu_click` `ui_find_window` `ui_list_menus` | Drive native editor menu bar via Windows UIAutomation. |
| **Editor focus** | `FocusEditorWindow` (UFUNCTION) | Bring the editor window to front from in-process, for capture/native-chrome driving. |

Full reference with parameters and return shapes: **[docs/capabilities.md](docs/capabilities.md)**.

---

## Use cases

- **Closed-loop self-testing** — the agent changes gameplay code, plays it, injects the input that exercises the change, and asserts on logs / actor state. See [docs/use-cases.md](docs/use-cases.md).
- **Regression guarding** — save a perf baseline on a known-good build; fail the loop if a later change busts the draw/GPU budget.
- **Headset-free VR iteration** — exercise Quest Touch button flows in desktop PIE without donning a headset.
- **Reproducing and verifying bug fixes** — script the exact input sequence that triggers a bug, confirm the fix, keep the script as a check.
- **Multi-editor workflows** — drive two projects/editors at once; input routing is in-process per editor, addressed by Remote Control port.

---

## Quickstart

```bat
:: 1. Install the plugin into your UE project (junction the Plugin/ folder)
mklink /J C:\Path\To\YourProject\Plugins\UnrealAgentPlayer C:\path\to\unreal-agent-player\Plugin

:: 2. Install the MCP server
cd unreal-agent-player\mcp-server
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
:: add the [windows] extra for editor-menu UIAutomation: pip install -e ".[dev,windows]"
```

Then enable **Remote Control API**, **Python Editor Script Plugin**, and **Unreal Agent Player** in your project, turn on **Enable Remote Execution** (Project Settings → Python), and register the MCP server with your client. Full steps: **[docs/setup.md](docs/setup.md)** and **[docs/claude-client-config.md](docs/claude-client-config.md)**.

Verify:

> Call the `bridge_status` tool.

Expected: `ue_running: true`, `rc_reachable: true`, `remote_exec_reachable: true`, `plugin_version: 0.0.1`.

---

## What's proven vs. known gaps

**Proven (runtime-verified on a shipping VR project):**
- In-process key injection drives the PIE player pawn while the editor is **not** the foreground window, and works across two open editors.
- `read_viewport_ui` reads live on-screen UMG text + focus (verified reading real connection/onboarding screens).
- Real `stat unit` draw/GPU/render-thread timings; perf baseline save/compare.
- PIE lifecycle, log capture, console + Python bridges, actor introspection, Blueprint helper discovery, editor-menu UIAutomation, in-process editor focus.

**Known gaps / roadmap:**
- **Screenshots omit UMG.** `screenshot_viewport` uses `HighResShot`, which does not capture the UMG layer — so on-screen UI/prompts don't appear *in the image*. To **read** on-screen UI, use `read_viewport_ui` (deterministic text + focus). A backbuffer/desktop capture path for pixel-level UI (icon/glyph prompts with no text) is still on the roadmap.
- **VR HMD pose injection is deferred** — no clean modular-feature hook; needs a fake `IXRTrackingSystem`.
- **VR controller pose-follow is limited in HMD-less PIE** — a real XR runtime (e.g. Meta XR Simulator) wins modular-feature precedence over the agent's fake controller. Button injection is unaffected. See [docs/architecture.md](docs/architecture.md).
- **Windows only.** See below.

## Platform support

The plugin and Remote Control / Python bridges are cross-platform in principle, but this project is **built and tested on Windows only**. Editor-menu UIAutomation is Windows-specific (`comtypes`). macOS is unimplemented (no hardware to build/test the UE Meta fork; editor-menu driving would need an AppleScript/Accessibility rewrite).

---

## Repository layout

```
Plugin/        UE editor plugin (C++)
mcp-server/    Python MCP server + tests
docs/          Setup, architecture, capabilities, use cases, writing test helpers
examples/      Example project-side test helpers
```

## License

MIT. See [LICENSE](LICENSE).
