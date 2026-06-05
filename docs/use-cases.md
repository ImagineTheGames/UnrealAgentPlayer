# Use cases

Worked examples of what an agent does with UnrealAgentPlayer. These are written as the *intent* the agent carries out via MCP tool calls — not as literal client transcripts — so they read independently of any one MCP client.

Every loop starts the same way:

> Call `bridge_status`. If `ue_running`/`rc_reachable` aren't both true, stop and tell the human to launch the editor.

---

## 1. Closed-loop self-test of a gameplay change

**Goal:** the agent changed how a door opens and wants to prove it works in play.

1. Compile the change (Live Coding for body-only edits, or a full build + editor restart for class-layout changes).
2. `exec_python` → `LevelEditorSubsystem.load_level("/Game/Maps/L_DoorTest")`.
3. `pie_start`.
4. Grab a log cursor (`log_tail` with `max_lines: 1` → note the cursor).
5. Move the player to the door: `input_key("W", pressed=true)`, wait, `input_key("W", pressed=false)`.
   - Movement keys stay held between down and up, so the pawn walks for as long as you hold.
6. Trigger the interaction: `input_key("E", pressed=true)` then `pressed=false`.
7. Assert:
   - `helper_list` → call the project helper `IsDoorOpen("Front")` via `CallTestHelper`; expect `true`; **or**
   - `actor_find` the door, `actor_get_properties` → check `bIsOpen`; **or**
   - `log_since(cursor)` → expect a `"Door opened"` log line.
8. `pie_stop`.

If the assertion fails, the agent reads the logs, forms a hypothesis, edits, and repeats — no human in the loop.

> **Reading state, not pixels.** Prefer `CallTestHelper` / `actor_get_properties` / `log_since` over screenshots. They're deterministic and don't depend on the UMG-capture limitation.

---

## 2. Reproduce-and-verify a bug fix

**Goal:** a bug reproduces only after a specific input sequence; confirm the fix and keep the repro.

1. Encode the trigger as an `input_sequence` (the exact keys/clicks/timing that cause the bug).
2. Before the fix: run it under `pie_start`, capture the failing log/state as the repro signature.
3. Apply the fix, recompile.
4. Run the same `input_sequence` again; assert the signature is gone.
5. Keep the sequence + assertion as a regression check to re-run on later changes.

---

## 3. Perf regression guard

**Goal:** don't let a change blow the frame budget on a target device profile.

1. On a known-good build: `pie_start`, let it settle, `perf_baseline_save("hub_idle")`.
   - `perf_stat` reads real draw/GPU/render-thread milliseconds, not estimates.
2. After a later change: `pie_start`, `perf_baseline_compare("hub_idle", tolerance_pct=10)`.
3. If any metric exceeds its baseline by >10%, the agent treats the change as a regression — investigates or backs it out.

---

## 4. Headset-free VR iteration

**Goal:** exercise a Quest Touch button flow without donning a headset.

1. `pie_start` on the VR map (desktop PIE, no HMD).
2. Inject Touch buttons as ordinary FKeys: `input_xr_button(hand="Left", button="OculusTouch_Left_X_Click", pressed=true/false)`.
   - These route through the same Slate path as keyboard input and **work without a headset**.
3. Assert on the resulting state via helpers / actor props / logs.

> Controller **pose** injection (`input_xr_pose`) is available but may be overridden by a live OpenXR runtime in HMD-less PIE (see [architecture.md](architecture.md)). Button flows are the reliable headset-free surface.

---

## 5. Driving two editors at once

**Goal:** run a change against two projects (or a host/client pair) in parallel.

- **Input** is injected in-process per editor through Slate, so each editor's PIE receives its own input independently — no OS-foreground contention, and both can be driven at the same time.
- **Address the right editor:**
  - Remote Control is per-port (HTTP `30010`, etc.) — unambiguous by port.
  - Python Remote Execution is multicast; use the client's project-substring filter to target a specific editor when several answer.

---

## 6. Setting up a scenario before acting

**Goal:** put the world in a known state, then test.

1. `exec_python` to spawn/possess/teleport as needed, or `actor_set_property` to flip a flag (e.g. unlock a door, give an item).
2. `pie_start` (or set up inside PIE).
3. Inject the input under test.
4. Assert.

---

## Patterns that hold across all loops

- **Perceive with the cheapest reliable channel.** Order of preference: project test helper → actor property → log line → screenshot. Screenshots are last because `HighResShot` omits UMG.
- **Use log cursors.** Snapshot a cursor before an action, `log_since` after, and you see exactly what that action produced — nothing else.
- **Held vs. tapped input.** A down-event with no up-event stays held (continuous movement). For a tap, send down then up.
- **Don't fight OS focus for input.** Injection is in-process; the editor need not be foreground. Reserve `FocusEditorWindow` for screenshots and native menu driving.
- **Fail structured.** Tools return error envelopes, not exceptions — branch on `code`/`retry_hint` and recover.
