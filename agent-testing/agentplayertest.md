# AgentPlayerTest

Answer a plain-English question about the running game by driving the `uap` CLI,
TDD-style: state a claim, the command proves or disproves it and renders an HTML report.

`$ARGUMENTS` = the question/claim to verify. Examples:
- `/agentplayertest does the door open when the player presses E`
- `/agentplayertest does the main menu version label show the build id`
- `/agentplayertest is the character's walk animation actually moving the legs`

Treat the question as the **assertion**. Your job: translate it into the cheapest
deterministic read that settles it, run that in PIE, and report pass/fail with the
read-back value as evidence. A vague visual question ("legs moving?") becomes a
concrete state read ("locomotion state == Move AND a foot bone advances between
two samples") -- never "I looked at a screenshot and it seems fine."

## STOP -- common mistakes (read this first)

These are the errors agents make every time. Don't.

1. **Screenshots: use `uap screenshot <abs.png>`. NOT `HighResShot`, NOT the MCP
   `screenshot_viewport`.** `uap screenshot` reads the composited backbuffer (3D **+** UMG +
   CommonUI + custom Slate) -- HighResShot and most MCP shots capture the **3D scene only, no
   UI**. If your menu/HUD/globe is missing from a shot, you used the wrong tool, not "UI can't
   be captured." (`uap pie start` plays in a dedicated PIE window and the capture targets the game
   viewport widget, so the shot is cropped to the game -- no editor chrome -- and stays correct even
   if you open a Blueprint/asset editor mid-test.)
2. **Run via this project's `uap.ps1`** (it pins `UAP_PROJECT`). If you call the venv python
   directly without `--project`/`UAP_PROJECT`, you'll cross-target **another open editor** (e.g.
   start PIE in the wrong project).
3. **`read-ui` x/y are screen pixels for screen-space UMG/CommonUI** (where `uap click` works) --
   but for a **world-space VR menu** (a `WidgetComponent`) they're render-target coords; a screen
   `uap click` won't hit them (that menu needs the VR laser).
4. **A check isn't done until `uap report finish` emits the HTML report** -- cite the path.
   Read concrete state (helper / property / bone delta / log), not pixels alone.
5. **A PASS requires a screenshot FROM THE EDITOR UNDER TEST** -- capture with `uap screenshot
   <abs.png>` via **this project's `uap.ps1`** (it stamps which editor the shot came from). A
   shot of **another editor** (wrong project) **auto-fails** the report; so does a manual attach
   of unknown origin. And a screenshot proves nothing unless you **read what it shows**
   (`uap read-ui` / a state read) and assert on it -- never attach pixels you didn't verify.
   (`report finish pass` with no verified shot auto-downgrades to FAIL. Opt out only for a
   genuinely headless check: `report start --no-require-screenshot`.)
6. **Discover verbs with `uap help`** -- do not reverse-engineer by dumping `dir()` on the subsystem.
7. **"this editor's plugin has no `<Verb>` ... sync and rebuild `<project>`" is a TOOLING version
   gap, not a broken editor and not a product bug.** The `uap` CLI is shared by every project while
   each project vendors its own plugin copy, so a project that has not synced+rebuilt is behind the
   CLI. Flat `uap pie start` degrades to the legacy verb by itself; `--mode vr`, `uap input
   hold/axis`, `uap sample` and helper NAMES cannot -- they need the rebuild (`Restart-Editor.ps1`
   plus a re-sync of the vendored plugin copy). Never re-run it as if it were flaky.
8. **`uap exec` runs IN-PROCESS -- a bad call HARD-CRASHES the editor** (kills RC + your run, not
   just your command). Known landmine: the engine's
   `DataTableFunctionLibrary.ExportDataTableToJSONString` `check()`-crashes on some row-struct
   shapes (`JsonWriter` assert `Stack.Top() == EJson::Object`). **To read a DataTable, iterate
   rows** (`get_editor_property('row_names')` / row handles + per-row property reads) -- **never**
   `ExportDataTableToJSONString`. Treat any whole-asset `...ToJSONString` exporter as unsafe, and
   prefer `blueprint-mcp` / asset-author tools for inspecting assets rather than runtime `exec`.

## Layer mismatches -- five traps that silently give you a WRONG answer

**Three of these five are the same headline risk: input you injected silently never arrives.**
Say that once, plainly, rather than leaving a reader to infer it across five entries -- it is the
single biggest way this tooling produces a confident wrong answer. An injected event goes missing
three ways, and they look identical from outside (no error, a success return, and a feature that
appears broken):

- **wrong layer** -- it enters BELOW the Slate pre-processor chain (#2);
- **right layer, wrong user** -- Slate discards it because its user index does not match the
  handler's owner (#5);
- **right layer, right user, no one home** -- the Slate route depends on Slate focus/user state,
  which an unfocused, throttled editor is exactly the condition for (#1).

The rest are **not flaky tools -- they are layer mismatches** too. Each one fails because the
test is operating at a different layer than the thing being tested: below Slate, aimed at the
wrong Slate user, outside the focused window, outside the game window, or *before the work has
actually happened*. Name the layer you are testing at and you can predict the NEXT trap instead
of memorising five.

All five fail **silently**, and all five look like product bugs rather than tooling limits --
each has already produced a false conclusion (a filed AI-behaviour ticket that was not real; a
"still broken" verdict on a fix that was working; a "PIE stopped" that had not; a cursor fix
that was about to be reported as still broken). **State queries are unaffected by 1, 2, 3 and
5**: when you can read engine state instead of simulating input and inferring from what you see,
do that. The fourth is different in kind -- it is about *when* you read, not *where* -- and it is
the one most likely to be in code you wrote yourself.

### 1. An unfocused editor throttles to exactly 3.0 fps -- every timing number taken then is void

- **Failure mode:** the editor is not the foreground window, so it runs at 3.0 fps and any
  timing-based measurement is meaningless. Nothing warns you; the numbers just come back wrong.
- **What it cost:** an AI chase observed at 3 fps looked like broken pursuit behaviour. A ticket
  was filed off that observation and later closed as not-real.
- **Blast radius:** anything measured in seconds or per-frame from OUTSIDE the engine -- "did it
  reach me within 4s", "does it judder", any stopwatch wrapped around CLI calls.
- **Do instead:** force PIE window focus, then **confirm the frame rate in-engine BEFORE recording
  any number** -- read `WorldDeltaSeconds`, or check the `hz` a sampler window reports for itself.
  `Slate.bAllowThrottling 0` alone is **NOT** sufficient; foreground focus is the actual gate.
- **Immune:** `uap sample` and `uap input hold` / `uap input axis` run entirely in-engine and need
  no CLI calls during the measurement window. A sampler that comes back at ~3 Hz is itself telling
  you the editor was throttled.
- **The tool now tells you at the number:** any uap surface that reports a frame rate (`report
  diag`, `perf_stat`, the perf baselines, and `sample` via its own `hz`) answers `throttled: true`
  plus a one-paragraph `warning` when the rate is at or below **5 fps** -- the throttle pins
  exactly 3.0, and nothing real lives under 5. The HTML report renders the same warning, so a
  human reading "fps 3.0" later cannot draw the wrong conclusion second-hand either.
- **If focus CANNOT be taken** -- `SetForegroundWindow` returns False with `GetForegroundWindow()`
  reporting 0 because your shell has no foreground window on this station, and `AttachThreadInput`,
  a synthetic ALT and `SetWindowPos` TOPMOST all fail the same way (`Slate.bAllowThrottling 0` does
  not lift it either) -- then say so and **report every timing-based conclusion as unmeasurable**.
  Do not quietly use the numbers anyway. Non-timing work continues normally: state reads are
  unaffected, and `uap sample` / `uap input hold` still drive and measure correctly in-engine.

### 2. `InjectKey` never reaches Slate input pre-processors or focus handlers

- **Failure mode:** you inject a key, nothing happens, and it looks exactly like the feature is
  broken. No error, no warning -- the input simply never enters that layer.
- **Why:** `InjectKey` enters BELOW the Slate pre-processor chain. Anything registered via
  `RegisterInputPreProcessor`, and anything depending on Slate focus routing, never sees it.
  Concrete example: `FPBWAnalogCursor : FAnalogCursor` registered as an `IInputProcessor` -- the
  entire virtual cursor is invisible to injected input.
- **Not a blanket limitation:** `InjectKey` works fine for gameplay input routed through Enhanced
  Input -- driving menus, firing actions, triggering bound gameplay keys. It fails at ONE layer:
  Slate pre-processors and Slate focus routing. If what you are testing is a bound gameplay action,
  inject away; if it is cursor movement, focus, or anything registered as an input pre-processor,
  injection will never reach it.
- **Blast radius:** analog/virtual cursors, focus rings, CommonUI focus targets,
  activatable-widget navigation, anything gated on `HasFocusedDescendants`.
- **What it cost:** an agent testing the cursor with `InjectKey` would have reported "still broken"
  on a fix that was actually working -- avoided only because the trap was already documented from
  an earlier encounter. The cost was paid once; the note is what saved the second run.
- **Do instead:** real OS input (`keybd_event`) to the PIE window, OR skip input simulation
  entirely and query engine state -- for focus questions, reading `GetDesiredFocusTarget()` and
  `HasFocusedDescendants` is more reliable than synthesising a keypress and inferring from what
  you see.

(Firsthand from the Project Broken Wings director, who hit it first: "we hit it, lost a cycle, and
wrote it down -- no cleverness involved.")

### 3. `uap screenshot` captures only the editor viewport

- **Failure mode:** you screenshot to verify something in a separate **Standalone** PIE window and
  get the editor viewport instead -- often a plausible-looking but WRONG image, which is worse than
  a blank one, because you will believe it.
- **Do instead:** an OS `PrintWindow` capture of the target window (find the window by title, then
  `PrintWindow` it). Project Broken Wings keeps a `Tools/Capture-Window.ps1` for exactly this; the
  technique is the portable part, not the script.
- **Worth knowing:** `PrintWindow` also works while the game is **PAUSED**, which the viewport path
  does not -- useful when you need to freeze a frame to inspect it.

### 4. An operation that acks a QUEUED request instead of a completed one

- **The tell, and it generalises far past `uap`:** *if a call returns success faster than the work
  could plausibly have finished, it acked a queue, not a result.* A teardown, a level load, a save,
  an async task -- the API hands back "request accepted" and you read it as "done".
- **Failure mode:** everything downstream is built on a state that has not happened yet. It is the
  async-boundary version of the same layer mismatch: you are reading at the request layer while
  asserting about the completion layer.
- **Worked example (real, 2026-08-28):** `uap pie stop` returned `{"ok": true, "result": true}`
  almost immediately -- and PIE was still running. A PIE *start* is queued too: the editor tick
  creates the play world one or more frames later, and the engine's end-play request is a **no-op
  unless that world already exists**. The stop landed in the gap, did nothing, and the queued start
  brought PIE up ~4 seconds *after* the "successful" stop. A second stop tore it down for real.
- **Why it was worth more than the wasted minute:** the lease system is built on that answer. An
  agent that trusts the stop releases its lease and hands the *next* agent an editor still in PIE.
  A wrong answer you act on alone is recoverable; a wrong answer that corrupts another agent's run
  is not.
- **Do instead:** poll the authoritative completion signal, bounded, and fail loudly on timeout --
  and check that the signal you poll can actually see the state you care about. `IsInPIE` reads
  *false* while a start is merely queued, so polling it "confirms" the exact case that is broken;
  `IsPIEInProgress` (live **or** queued) is the honest one. Better still, where the API allows it,
  **cancel or absorb the pending work** rather than racing it -- `pie stop` now cancels a queued
  start, which stops the situation existing rather than detecting it afterwards.
- **The general rule, worth more than this one bug:** *check what your confirmation signal reports
  in the FAILURE case, not just the success case.* A signal that is unreliable precisely where it
  matters is worse than a plain wrong answer -- it corrupts the evidence you would use to prove the
  fix, so a bad fix passes review looking verified. `IsInPIE` is the worked example: correct
  whenever PIE is genuinely running, and false in exactly the window where the stop silently did
  nothing. Before you trust a check, ask what it returns when the thing you are checking for has
  gone wrong.
- **In this CLI today:** `uap pie stop` blocks until teardown is confirmed and FAILS on timeout
  rather than acking (`ok:true` + `stopped:true` is the only "it stopped"). `uap pie start` still
  returns at once by design, and now says so: `queued: true, confirmed: false` -- follow it with
  `uap pie wait <seconds>`, which is the confirmation half.

### 5. Injected input aimed at the WRONG SLATE USER -- right layer, discarded anyway

- **Failure mode:** you drive an analog stick, the Slate analog cursor does not move one pixel,
  and the call reports success. Before/after screenshots are pixel-identical and
  `GetMousePosition()` is unchanged at its start value. No error, no warning.
- **Why:** Slate stamps every input event with a USER index and handlers filter on it.
  `FAnalogCursor::IsRelevantInput()` is literally `GetOwnerUserIndex() ==
  InputEvent.GetUserIndex()` (engine `AnalogCursor.cpp:192`). `uap`'s Slate-path injections used
  to stamp user index **0** unconditionally, so on any project whose cursor is owned by a
  different Slate user the event was thrown away before the cursor ever ran. Worse, the engine
  helps: the 7-argument `FPointerEvent` constructor hardcodes the user index to 0 *inside
  SlateCore*, so "not passing a user" was never neutral.
- **Distinct from #2, and that is the point:** #2 is the WRONG LAYER (the event enters below the
  pre-processor chain, so no pre-processor of any user sees it). This is the RIGHT layer with the
  WRONG USER -- the chain runs, and the handler declines the event. Same silent shape, different
  mechanism, different fix. Assuming it was #2 costs you the fix.
- **Blast radius:** analog and virtual cursors, anything on `RegisterInputPreProcessor`,
  splitscreen / second-local-player input, and any Slate handler that filters by user.
- **What it cost:** the Project Broken Wings director hit it live and nearly filed a bug against
  their own working cursor fix -- the tooling's silence was indistinguishable from a broken
  feature (ClickUp 86ak7kay9).
- **Do instead:** name the user. `uap input axis <AxisKey> <v> --seconds N --user <N>` drives the
  **Slate** route as Slate user N; without `--user` the sample takes the game-viewport route,
  which never enters the pre-processor chain at all -- so "just drop the flag and retry" is the
  wrong instinct and gets you an `ok` with nothing moving. Every result now carries
  `route: slate|viewport` (and `user_index`), and `uap input status` reports it for a live hold,
  so which layer you drove is readable instead of assumed.
- **It refuses loudly now:** a `--user` Slate has no registered user for is rejected with the
  registered indices listed, rather than discarded. A silent discard is worse than a missing
  verb: a missing verb 404s and someone notices.

## Adding a verb? What a good failure message contains

When a verb cannot do what was asked, the message is the whole recovery path -- an agent either
fixes it in one step or files a false bug. Three parts, and the last one is the one people drop:

1. **Name the missing capability** (the exact verb/setting), not just "failed".
2. **Say why the plausible substitute would be wrong** -- otherwise the reader "helpfully" reaches
   for it and gets a confidently wrong answer.
3. **Give the exact remedy**, concrete enough to run.

The `--mode vr` refusal is the pattern to copy; it does all three in one string:

> this editor's plugin has no `StartPIEMode` (VR Preview needs it; the legacy `StartPIE` verb can
> only start FLAT PIE, and starting flat when you asked for vr would hide every HMD-only bug). The
> CLI is shared by every project while each project vendors its own plugin copy, so this one is
> behind the CLI: sync and rebuild `<project>` (`Restart-Editor.ps1`) to get the verb.

Same rule for a fallback: fall back only to a verb that answers the **same** question, and say in
the result that you did (`via`, `note`, `degraded`). Never to one that answers a different
question -- that is a silent wrong answer wearing a success.

## The bridge is the `uap` CLI

Invoke from your project root as:

```
powershell -NoProfile -File uap.ps1 <verb> [args...]
```

(`uap.ps1` is dropped at the project root by `Install-AgentTest.ps1`.) No MCP tools
required. The CLI owns the HTML report. It works in any agent session as long as the
editor is up. **Run `uap help` for the full verb catalog + copy-paste recipes** -- don't
reverse-engineer tools by dumping `dir()` on the subsystem.

## Quick recipes (copy-paste)

`uap click` handles buttons; `tab`/`nav` are still composed via `exec` for now:

```
# Click an on-screen UMG button by its label (one call):
uap click "VR TRAINING"

# ...or the underlying chain (what `uap click` runs) for a precise spot:
uap read-ui                                         # find the button's x,y in the dump
uap rc InjectMouseMove X=<x> Y=<y> bAbsolute=true
uap rc InjectMouseButton Button=Left bPressed=true
uap rc InjectMouseButton Button=Left bPressed=false

# Press a key ONCE (real input path, works backgrounded):
uap rc InjectKey KeyName=E bPressed=true
uap rc InjectKey KeyName=E bPressed=false

# HOLD a key -- sustained locomotion. `rc InjectKey bPressed=true` is one event: the CLI
# round-trip is ~1s so re-injecting per poll cannot cover a sub-second window, and any
# FlushPressedKeys silently drops a latched key. This re-asserts it every frame in-engine
# and returns immediately, so you can read state WHILE it is held.
uap input hold W --seconds 3
uap rc CallTestHelper Name=<HelperName> JsonArgs={}    # runs while W is still held

# VR locomotion -- the thumbstick is an AXIS, not a button:
uap input axis OculusTouch_Left_Thumbstick_Y 1.0 --seconds 3

# VR controller button:
uap rc InjectXRButton Hand=Right ButtonKeyName=OculusTouch_Right_Trigger_Click bPressed=true

# Measure something sub-second (judder, a 0.6s wind-up, a one-frame pop):
uap sample start PlayerCameraManager WorldLocation --seconds 2
# -> stats.delta_max / delta_p95 are the per-frame movement; a 1Hz sample cannot see this

# Tie log output to an action (cursor FIRST, then drive, then read):
uap log cursor
uap input hold W --seconds 2
uap log since <cursor> --grep "<the log text you expect>"

# Read game-truth (preferred over screenshots):
uap helpers --names
uap rc CallTestHelper Name=<HelperName> JsonArgs={}

# Select a CommonUI tab / move focus (no verb yet -- drive the live widget via exec):
uap exec "import unreal; ...CommonTabListWidgetBase.SelectTabByID(...)"
```

Deeper docs (in the UnrealAgentPlayer repo): `docs/agent-testing.md` (usage),
`docs/capabilities.md` (every tool + the input model), `docs/known-issues.md`.

Verbs used in this flow:
- `uap status` -- preflight; returns JSON `{ok, rc_reachable, plugin_version}`.
- `uap report start "<task>"` -- open a new report (writes `~/.uap-reports/<ts>/data.json`).
- `uap report assert "<label>" pass|fail "<evidence>"` -- record a pass/fail check.
- `uap report note "<text>"` -- add a free-text note.
- `uap report finish pass|fail "<summary>"` -- render + open `index.html`; prints the path.
- `uap exec "<python>"` -- run `import unreal; ...` in the live editor.
- `uap rc <FunctionName> [key=value ...]` -- call a UAP_Preset UFUNCTION (use `uap exec` for complex/nested args).
- `uap pie start [--mode flat|vr]` / `uap pie wait <sec>` / `uap pie stop [--timeout 30]` --
  start / await / stop Play-In-Editor (version-correct; wraps the engine call so you never touch
  the raw subsystem). `start` only QUEUES the session (`queued:true, confirmed:false`) -- the world
  does not exist when it returns, so always follow it with `pie wait`. `stop` is the opposite
  shape: it BLOCKS until the teardown is confirmed, and fails (`ok:false`) if it never happens
  rather than acking a stop that did not take. `ok:true` + `stopped:true` is the only "PIE is
  gone"; treat anything else as a still-live editor and do not release the lease.
  `--mode vr` starts VR Preview -- the HMD code path (OpenXR input, `IsHeadMountedDisplayEnabled()`
  branches) that flat PIE never takes. Needs a connected headset; it fails with a reason rather
  than silently falling back, so an HMD-only bug cannot look absent.
- `uap read-ui` -- dump viewport UMG text as JSON.
- `uap screenshot <file> [--caption "..."]` -- capture game+UMG; auto-attached to the active report.
- `uap input hold <Key> --seconds N` / `uap input axis <AxisKey> <v> --seconds N` /
  `uap input release [<Key>]` / `uap input status` -- sustained input, re-asserted every frame
  in-engine. Use these, not repeated `rc InjectKey`, for anything you must hold. Key names are
  exact FKeys (`W`, `C`, `LeftControl`, `OculusTouch_Left_Thumbstick_Y`); a refused hold
  presses nothing. Bare `uap input release` is the RECOVERY hatch -- it clears every hold and
  flushes any key the engine still has down, so a stuck key never needs a PIE restart.
- `uap sample start <object> <property> --seconds N` / `uap sample read` -- per-frame series +
  delta stats. The only way to see sub-second behaviour.
- `uap log cursor` / `uap log since <c> [--grep RE]` / `uap log tail [--grep RE]` --
  editor log through the plugin (same editor targeting as every other verb, captured into the report).
- `uap helpers [--grep RE] [--names]` -- list the project's test helpers with their arg schemas.
- `--agent <token>` and `--project <name>` are accepted on EVERY verb (ignored where irrelevant),
  so you can pass the same flags on every call without special-casing.

## Step 1 -- Translate the question into a verifiable assertion

Pick the **cheapest reliable channel** that answers it (in preference order):

1. **Project test helper** (`uap rc CallTestHelper Name=... JsonArgs={}`) -- game-specific
   truth a designer exposed (`IsDoorOpen("Front")`). Best when one exists. List them with
   `uap helpers --names`.
2. **Actor / component property** (`uap exec "<read actor property>"`) -- read a `bool`/value
   that encodes the answer (`bIsOpen`, a phase enum).
3. **Frame-rate sample** (`uap sample start <object> <property> --seconds N`) -- for anything
   sub-second: judder, a short wind-up, a one-frame pop. An `exec` round-trip is ~1s and cannot
   see any of it. The returned `delta_*` stats ARE the per-frame motion; two `exec` reads are not
   a substitute.
4. **Anim / bone read via `uap exec`** -- for animation/visual questions. Read the anim
   instance's exposed values (`Speed2D`, `bIsMoving`, current state-machine state) and/or
   **sample a bone's world transform across two ticks** to prove motion.
5. **Log line** (`uap log cursor` before the action, then `uap log since <c> --grep ...`)
   -- when the behavior emits a known log. Do not shell-tail `Saved/Logs/*.log`: that evidence
   never reaches the report and is not project-targeted.
6. **On-screen UMG text** (`uap read-ui`) -- for HUD/menu text. Screenshots omit UMG, so
   read text here, not from pixels.
7. **Screenshot** (`uap screenshot`) -- last resort / human-facing evidence only; never the
   sole basis for a pass. Attach it as supporting evidence.

State the concrete pass condition before running. A non-zero speed with a frozen bone is a FAIL.

## Step 2 -- Run the loop

1. **Preflight**: `uap status` -> require `rc_reachable:true`. Editor down? Launch the editor
   yourself (your project's launch script), wait for RC, retry. Do not fall back to raw RC
   without a report; the report is the point.
2. **`uap report start "<question>"`** -- question verbatim, `--project <YourProject>`. A passing
   report **requires a screenshot by default** -- `report finish pass` auto-downgrades to fail
   unless one is attached (no silent false positives). Add `--no-require-screenshot` only for a
   genuinely headless/no-visual check.
3. **Diagnostics**: `uap report diag --project <YourProject>` -- captures the editor's plugin
   version, open level, PIE state (env) AND frame timing / fps (perf) into the report (sourced
   via `exec`, so it reads the RIGHT editor even when another squats the RC port). Run it once
   after start, and again WHILE PIE is live (step 5) to record the game's frame rate rather than
   the idle editor's. The report nags at finish if env is empty.
   **If the perf it returns carries `throttled: true`, read its `warning` and act on it before
   anything else** -- a rate at or below 5 fps is the editor's not-foreground throttle (trap #1
   below), not a performance finding, and every timing already taken is void. The same flag rides
   on `uap sample` (from the sampler's own `hz`) and is rendered into the HTML report, so a
   throttled run cannot be quietly summarised as a slow game.
4. **Scene**: `uap exec` to `load_level('/Game/...')` if the question implies a specific map;
   else use the open level and `uap report note "using level X"`.
5. **Start PIE**: `uap pie start`, then `uap pie wait 12` -- blocks until the game world is
   live (up to 12s) and fails if it never comes up. (These wrap whichever engine call is
   correct for the version in use; do NOT reach for `PlayWorldEditorSubsystem`, which was
   removed and does not exist on any engine this supports.) If the question touches the HMD
   code path -- OpenXR input, or an `IsHeadMountedDisplayEnabled()` branch such as a world-space
   VR screen -- use `uap pie start --mode vr` (VR Preview) instead; flat PIE takes neither path,
   so the bug will look absent. Give it a beat for a frame to render before capturing a
   screenshot (see step 8). Grab a log cursor (`uap log cursor`) before driving the condition.
6. **Set up + drive** the exact condition: spawn/possess/teleport via `uap exec`; flip a flag
   with `uap exec`. For input:
   - a one-off press -> `uap rc InjectKey KeyName=E bPressed=true` (or `InjectXRButton` for VR);
   - anything that must be **held** -> `uap input hold W --seconds 3`, which returns at once and
     keeps the key held in-engine so you can read state during it. Never emulate a hold by
     re-injecting per poll (the round-trip is ~1s) or by teleporting the pawn (displacement with
     no velocity makes movement/perception logic behave differently).
   - VR locomotion -> `uap input axis OculusTouch_Left_Thumbstick_Y 1.0 --seconds 3`
     (thumbsticks are analog axes, not buttons).
7. **Read** via the channel chosen in Step 1. For motion proofs prefer
   `uap sample start <object> <property> --seconds N` -- it records every frame in-engine and
   returns `delta_mean`/`delta_max`/`delta_p95`, which is the only way to see judder or a
   sub-second window; two `uap exec` reads ~0.3s apart is the fallback when no sampler path fits.
   Then `uap report assert "<label>" pass|fail "<the values>"`.
8. **Evidence**: `uap screenshot shot.png --caption "..."` for human-facing evidence; `uap read-ui`
   for on-screen text. **`uap screenshot` requires a live, rendering game frame (PIE running)** --
   called against an idle editor viewport it writes no file and reports `exists:false`; treat
   that as a failure to capture, not a pass. On a fail, gather log lines with
   `uap log since <the cursor from step 5> --grep "<what you expect>"` -- that goes
   through the same editor targeting as everything else and lands in the report; do NOT
   shell-tail `Saved/Logs/*.log`, because that evidence never reaches the HTML. Put the
   hypothesis in the summary.
9. **Finish**: `uap report finish pass|fail "<summary>"` -> prints the `index.html` path; renders +
   opens the report. It **auto-stops PIE** for you (a finished test never leaves the editor stuck
   in Play-In-Editor; response includes `pie_stopped`; pass `--keep-pie` only if you deliberately
   want to keep inspecting the running game). A `pass` with no attached screenshot comes back as
   `verdict: fail, downgraded: true` -- that is the gate doing its job; attach a screenshot first.
   (You can still `uap pie stop` earlier if you want the editor idle before finishing.) The
   auto-stop is CONFIRMED, not fired-and-forgotten: `pie_stopped:true` means the teardown was
   observed. If it comes back with `pie_stop_error`, the editor is still in PIE -- say so and do
   not release an editor lease.
10. **Report back**: verdict, the read-back numbers, and the report path. State failures plainly.

## A verification is not done until the report exists

`uap report finish` must emit `~/.uap-reports/<ts>/index.html` and you must cite that path.
Never conclude a behavior "works" from a screenshot alone -- read concrete state (a test
helper, an actor/anim property, a bone delta across two samples, or a log line).

## Project-specific presets

This command is the general, question-driven form. To bake a repeatable checklist for one
system (e.g. a full acceptance run for a specific actor), copy this file to a new command
(e.g. `mytest.md`) and hard-code the scene, the helper list, and the assertions. See
`docs/agent-testing.md` in the UnrealAgentPlayer repo for the preset recipe.
