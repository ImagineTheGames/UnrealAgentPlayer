# UnrealAgentPlayer

**Let an AI coding agent drive the Unreal Editor — start Play-In-Editor, inject real input, read logs and game state, capture screenshots, and run project-specific test helpers — to close the loop on its own changes.**

UnrealAgentPlayer is a thin C++ editor plugin plus an external Python [MCP](https://modelcontextprotocol.io) server. Together they give an agent (Claude Code, Claude Desktop, Cursor, or any MCP client) a structured, scriptable handle on a running editor: make a change, compile it, play it, drive it with keyboard/mouse/gamepad/VR input, observe what happened, and decide what to do next — without a human pressing keys.

- **Plugin** (`Plugin/`): a `UEditorSubsystem` that exposes a handful of `UFUNCTION`s to UE's built-in Remote Control, plus log capture, input injection, a fake motion-controller modular feature, and Blueprint test-helper discovery.
- **MCP server** (`mcp-server/`): a stdio MCP server that talks to the editor over Remote Control HTTP and Python Remote Execution, and presents ~30 agent-facing tools across 12 families. The Python layer grows new tools without recompiling the engine.

Supports UE 5.6, 5.7, 5.8, and the Meta 5.7 fork. **Windows editor only** (see [Platform support](#platform-support)).

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

- **Remote Control HTTP** (`127.0.0.1:<per-editor port>`) — structured calls to the subsystem's `UFUNCTION`s. Deterministic, typed, per-port (so two editors are addressed independently). Each editor gets its own port rather than the stock 30010; `uap status` reports it as `rc_port`. You should never need to set it by hand — see [Setup](docs/setup.md).
- **Python Remote Execution** (UDP multicast `239.0.0.1:6766`) — arbitrary `import unreal; ...` scripting for anything not exposed as a `UFUNCTION` (load a level, read a pawn transform, start PIE).

Key/button injection routes straight through the game viewport client
(`UGameViewportClient::InputKey`) -- the same call the engine makes on a real keypress --
so it does **not** depend on Slate keyboard focus or the editor being the OS-foreground
window. This is in-process, so two editors in separate processes can each be driven
independently. (An earlier Slate-focus path silently dropped keys when the editor was
backgrounded or PIE ran inside the level viewport; see `docs/architecture.md`.)

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
| **Reporting** | `report_start` `report_assert` `report_note` `report_caption` `report_finish` | Auto-capture a run into a tabbed HTML report (verdict, checks, screenshot gallery, tool timeline, diagnostics) and auto-open it. |
| **Screenshots** | `screenshot_viewport` | Capture the viewport (HighResShot — see [gaps](#whats-proven-vs-known-gaps)). |
| **Test helpers** | `helper_list` + `CallTestHelper` | Auto-discover and call project-specific Blueprint/C++ assertion helpers (e.g. `IsDoorOpen`). |
| **Editor menus** | `ui_menu_click` `ui_find_window` `ui_list_menus` | Drive native editor menu bar via Windows UIAutomation. |
| **Editor focus** | `FocusEditorWindow` (UFUNCTION) | Bring the editor window to front from in-process, for capture/native-chrome driving. |
| **Standalone** | `game_launch` `game_attach` `game_list` `game_stop` | Launch/attach standalone game instances (each on its own RemoteControl port) and drive them like a player; play tools take a `target`. Parallel instances + editor coexist. |

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
:: 1. Clone this repo and build its venv (once per machine)
git clone https://github.com/ImagineTheGames/UnrealAgentPlayer.git
cd UnrealAgentPlayer\mcp-server
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
:: add the [windows] extra for editor-menu UIAutomation: ".[dev,windows]"

:: 2. Install the plugin into your UE project (junction the Plugin/ folder)
mklink /J C:\Path\To\YourProject\Plugins\UnrealAgentPlayer C:\path\to\UnrealAgentPlayer\Plugin
```

Then enable **Remote Control API**, **Python Editor Script Plugin**, and **Unreal Agent Player** in your project, and turn on **Enable Remote Execution** (Project Settings → Python).

Two things that stop people cold, both covered in [docs/setup.md](docs/setup.md):

- **UE 5.8+ only:** tick **Allow Any Remote Function Call** (Project Settings → Plugins → Remote Control → Security). It defaults to off in 5.8 and blocks every call with `Executing function '...' is not allowed by remote control settings`. The setting does not exist on 5.6/5.7.
- **Do not set the Remote Control port by hand.** Each editor picks its own; `uap status` reports it as `rc_port`.

Full steps: **[docs/setup.md](docs/setup.md)** and **[docs/claude-client-config.md](docs/claude-client-config.md)**.

### On a team

Copy `Plugin/` into your project as real files and commit it, and commit the `uap.ps1` +
`.claude/commands/agentplayertest.md` that `Install-AgentTest.ps1` generates — they contain
no machine-local paths, so they work for every teammate as-is. Each person then does the
one-time clone + venv above (and sets `UAP_HOME` if they cloned it somewhere unusual).

If those files are missing from source control, every agent following your `AGENTS.md` fails
at its first step, and the error does not say why.

Verify, from your project root with the editor running:

```powershell
powershell -NoProfile -File uap.ps1 status
```

Expected: `{"ok": true, "rc_reachable": true, "plugin_version": "0.0.1", "rc_port": <n>}`.
`rc_port` differs per project; any number is fine as long as `rc_reachable` is true.

With the MCP server registered, ask your client to call `bridge_status` instead and expect
`ue_running: true`, `rc_reachable: true`, `remote_exec_reachable: true`.

Stuck? [docs/setup.md](docs/setup.md#if-it-does-not-work) has a symptom → cause → fix table.

---

## What's proven vs. known gaps

**Proven (runtime-verified on a shipping VR project):**
- In-process key injection drives the PIE player pawn while the editor is **not** the foreground window, and works across two open editors.
- `read_viewport_ui` reads live on-screen UMG text + focus (verified reading real connection/onboarding screens).
- Real `stat unit` draw/GPU/render-thread timings; perf baseline save/compare.
- PIE lifecycle, log capture, console + Python bridges, actor introspection, Blueprint helper discovery, editor-menu UIAutomation, in-process editor focus.
- `report_*` produces a tabbed HTML run report with auto-captured timeline + screenshot gallery.
- Runtime module (DeveloperTool, Win64-only) drives a standalone `-game` process; verified two instances + the editor on distinct RC ports simultaneously.

**Known gaps / roadmap:**
- **Screenshots omit UMG.** `screenshot_viewport` uses `HighResShot`, which does not capture the UMG layer — so on-screen UI/prompts don't appear *in the image*. To **read** on-screen UI, use `read_viewport_ui` (deterministic text + focus). A backbuffer/desktop capture path for pixel-level UI (icon/glyph prompts with no text) is still on the roadmap.
- **VR HMD pose injection is deferred** — no clean modular-feature hook; needs a fake `IXRTrackingSystem`.
- **VR controller pose-follow is limited in HMD-less PIE** — a real XR runtime (e.g. Meta XR Simulator) wins modular-feature precedence over the agent's fake controller. Button injection is unaffected. See [docs/architecture.md](docs/architecture.md).
- **Windows only.** See below.
- **Non-VR standalone:** `game_launch` defaults to `no_vr=true`, adding `-nohmd` so a VR project's boot flow takes its no-HMD/desktop path instead of waiting for a headset — verified end-to-end (boot → EOS login → onboarding menu, readable + clickable by the agent). Run with the **editor closed** to avoid GPU contention (a standalone sharing the GPU with a live editor can stall its render thread → RC times out). Per-instance ports require `-game` launched from the editor binary (`WITH_EDITOR`); packaged-build per-instance ports are a follow-up.

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
