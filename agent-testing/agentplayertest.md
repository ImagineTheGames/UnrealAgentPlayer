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

## The bridge is the `uap` CLI

Invoke from your project root as:

```
powershell -NoProfile -File uap.ps1 <verb> [args...]
```

(`uap.ps1` is dropped at the project root by `Install-AgentTest.ps1`.) No MCP tools
required. The CLI owns the HTML report. It works in any agent session as long as the
editor is up.

Verbs used in this flow:
- `uap status` -- preflight; returns JSON `{ok, rc_reachable, plugin_version}`.
- `uap report start "<task>"` -- open a new report (writes `~/.uap-reports/<ts>/data.json`).
- `uap report assert "<label>" pass|fail "<evidence>"` -- record a pass/fail check.
- `uap report note "<text>"` -- add a free-text note.
- `uap report finish pass|fail "<summary>"` -- render + open `index.html`; prints the path.
- `uap exec "<python>"` -- run `import unreal; ...` in the live editor.
- `uap rc <FunctionName> [key=value ...]` -- call a UAP_Preset UFUNCTION (use `uap exec` for complex/nested args).
- `uap pie start` / `uap pie wait <sec>` / `uap pie stop` -- start / await / stop Play-In-Editor
  (version-correct; wraps the engine call so you never touch the raw subsystem).
- `uap read-ui` -- dump viewport UMG text as JSON.
- `uap screenshot <file> [--caption "..."]` -- capture game+UMG; auto-attached to the active report.

## Step 1 -- Translate the question into a verifiable assertion

Pick the **cheapest reliable channel** that answers it (in preference order):

1. **Project test helper** (`uap rc CallTestHelper Name=... JsonArgs={}`) -- game-specific
   truth a designer exposed (`IsDoorOpen("Front")`). Best when one exists. List them with
   `uap rc ListTestHelpers`.
2. **Actor / component property** (`uap exec "<read actor property>"`) -- read a `bool`/value
   that encodes the answer (`bIsOpen`, a phase enum).
3. **Anim / bone read via `uap exec`** -- for animation/visual questions. Read the anim
   instance's exposed values (`Speed2D`, `bIsMoving`, current state-machine state) and/or
   **sample a bone's world transform across two ticks** to prove motion.
4. **Log line** (`uap exec` to query the log after an action) -- when the behavior emits a known log.
5. **On-screen UMG text** (`uap read-ui`) -- for HUD/menu text. Screenshots omit UMG, so
   read text here, not from pixels.
6. **Screenshot** (`uap screenshot`) -- last resort / human-facing evidence only; never the
   sole basis for a pass. Attach it as supporting evidence.

State the concrete pass condition before running. A non-zero speed with a frozen bone is a FAIL.

## Step 2 -- Run the loop

1. **Preflight**: `uap status` -> require `rc_reachable:true`. Editor down? Launch the editor
   yourself (your project's launch script), wait for RC, retry. Do not fall back to raw RC
   without a report; the report is the point.
2. **`uap report start "<question>"`** -- question verbatim, `--project <YourProject>`. If the
   verdict needs visual proof, add `--require-screenshot` -- then `report finish pass`
   auto-downgrades to fail unless a screenshot is actually attached (no silent false positives).
3. **Diagnostics**: `uap report diag --project <YourProject>` -- captures the editor's plugin
   version, open level, PIE state (env) AND frame timing / fps (perf) into the report (sourced
   via `exec`, so it reads the RIGHT editor even when another squats the RC port). Run it once
   after start, and again WHILE PIE is live (step 5) to record the game's frame rate rather than
   the idle editor's. The report nags at finish if env is empty.
4. **Scene**: `uap exec` to `load_level('/Game/...')` if the question implies a specific map;
   else use the open level and `uap report note "using level X"`.
5. **Start PIE**: `uap pie start`, then `uap pie wait 12` -- blocks until the game world is
   live (up to 12s) and fails if it never comes up. (These wrap the version-correct engine
   call; do NOT use the old `PlayWorldEditorSubsystem`, which does not exist on UE 5.7.) Give
   it a beat for a frame to render before capturing a screenshot (see step 8). Grab a log
   cursor before driving the condition.
6. **Set up + drive** the exact condition: spawn/possess/teleport via `uap exec`; inject input
   via `uap rc InjectKey KeyName=E bPressed=true` (or `InjectXRButton` for VR); flip a flag with `uap exec`.
7. **Read** via the channel chosen in Step 1. For motion proofs, `uap exec` to sample a
   bone/property, wait ~0.3s, sample again, compare. Then
   `uap report assert "<label>" pass|fail "<the values>"`.
8. **Evidence**: `uap screenshot shot.png --caption "..."` for human-facing evidence; `uap read-ui`
   for on-screen text. **`uap screenshot` requires a live, rendering game frame (PIE running)** --
   called against an idle editor viewport it writes no file and reports `exists:false`; treat
   that as a failure to capture, not a pass. On a fail, gather log lines and put the hypothesis
   in the summary.
9. **Stop PIE**: `uap pie stop`.
   Then `uap report finish pass|fail "<summary>"` -> prints the `index.html` path; renders +
   opens the report. (With `--require-screenshot`, a `pass` with no attached screenshot comes
   back as `verdict: fail, downgraded: true` -- that is the gate doing its job.)
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
