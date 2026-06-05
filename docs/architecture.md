# Architecture

See also the [capabilities reference](./capabilities.md) and [use cases](./use-cases.md).

TL;DR:

- Thin C++ editor plugin (subsystem + log capture + input injection + helper discovery) exposes a handful of UFUNCTIONs to UE's built-in Remote Control.
- External Python MCP server (stdio) talks to UE via Remote Control HTTP (127.0.0.1:30010) and Python Remote Execution (UDP 239.0.0.1:6766 multicast).
- The agent-visible surface is ~22 MCP tools across 8 families. The Python layer grows without engine recompiles.

## Post-v1 additions

### VR input (V2)

Three injectable surfaces for desktop-mode VR testing (no headset required):

1. **Controller buttons** — `input_xr_button` / `InjectXRButton`. Quest Touch buttons
   (`OculusTouch_Left_X_Click`, `OculusTouch_Right_Trigger_Axis`, ...) are ordinary `FKey`s,
   so they route through the same Slate path as `input_key`.
2. **Controller pose** — `input_xr_pose` / `InjectXRControllerPose` (+ `input_xr_clear`).
   The plugin registers a fake `IMotionController` modular feature (`FAgentMotionController`)
   that returns agent-set Left/Right poses. `UMotionControllerComponent` polls it like a real
   device, so tracked-component transforms follow the injected pose. Clearing a hand makes the
   fake controller return `false` for that source, so real devices win again.
3. **HMD pose — DEFERRED.** There is no `IMotionController`-equivalent injection hook for the
   head pose; overriding it requires a fake `IXRTrackingSystem`/stereo render device, which is
   invasive and risks destabilizing the Meta XR runtime path. Revisit only when a concrete test
   needs head-pose control.

**Verified limitation (HMD-less PIE + VRExpansion + OpenXR):** `InjectXRControllerPose`
registers a valid `IMotionController` and returns `true`, but the injected pose is **not
necessarily consumed** by the live motion-controller component. Both the stock
`UMotionControllerComponent` and VRExpansion's `UGripMotionControllerComponent` poll
`IModularFeatures::GetModularFeatureImplementations<IMotionController>()` and use the **first**
controller that returns a pose. In an OpenXR project the runtime registers its own motion
controller, so iteration order / runtime precedence can mean the agent's fake never wins, and
the component stays at its rest pose. Confirmed empirically: injecting Left/Right poses in
HMD-less PIE left the grip components' relative transforms unchanged. **Reliable controller
pose-follow needs an actual XR runtime (e.g. Meta XR Simulator) or deeper integration that
guarantees override precedence.** Button injection (FKey path) is unaffected and works. The
injection API itself (subsystem + modular feature registration) is correct and works for a
project whose components have no competing real controller.

### Perf regression baselines (V2)

`perf_baseline_save` captures the current `stat unit`/`fps` parse and stores it by name in a
JSON file (`BaselineStore`). `perf_baseline_compare` re-reads stats and flags any metric that
exceeds its baseline by more than `tolerance_pct` (default 10%). Pure Python — no engine change.

### Editor menu UIAutomation (V3)

`ui_menu_click` / `ui_find_window` / `ui_list_menus` drive Slate menus via Windows UIAutomation
(`comtypes`, optional `[windows]` extra). The driver is import-guarded: on non-Windows or without
`comtypes`, `available` is `False` and tools return a `UIA_UNAVAILABLE` envelope instead of
crashing. Windows-only.

### Non-Windows (Mac) support — BLOCKED

Not implemented: no Mac hardware available to build/test. Enabling it would require (a) dropping
the `Win64`-only `PlatformAllowList`/`SupportedTargetPlatforms` from the `.uplugin`, (b) a Mac
analog for Phase D — UIAutomation is Windows-only, so editor-menu driving would need an
AppleScript/Accessibility (AX) rewrite, and (c) a Mac running the UE Meta fork to compile and run
the Tier 2/3 tests. Blocked pending hardware.
