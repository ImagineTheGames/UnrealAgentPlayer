## Verifying runtime game behavior

To verify any runtime game behavior (does X animate / open / trigger / show the right
text), run `/AgentPlayerTest <question>`. It drives the `uap` CLI (`uap.ps1` at the
project root), which talks to the running editor over Remote Control + Python remote-exec
and renders an HTML report.

- A verification is NOT complete until `uap report finish` emits a report at
  `~/.uap-reports/<ts>/index.html`. Cite the path.
- A passing report REQUIRES a screenshot FROM THE EDITOR UNDER TEST -- capture with
  `uap screenshot <abs.png>` via this project's `uap.ps1` (it stamps the source editor). A shot of
  another editor (or a manual attach of unknown origin) auto-FAILS the pass. Pixels are not proof
  unless you read what they show (`uap read-ui`/state) and assert on it. (Opt out for a genuinely
  headless check: `report start --no-require-screenshot`.)
- Never conclude a behavior "works" from a screenshot alone. Read concrete state -- a
  project test helper (`uap rc CallTestHelper`), an actor/anim property, an anim-bone
  delta across two samples, or a log line. A non-zero speed with a frozen bone is a FAIL.
- To HOLD input (walking, a stick push), use `uap input hold <Key> --seconds N` /
  `uap input axis <AxisKey> <v> --seconds N`, never repeated `rc InjectKey`: one injected
  event is a single frame, the CLI round-trip is ~1s, and any FlushPressedKeys silently
  drops a latched key. VR locomotion is an analog AXIS, not a button. **For a GAMEPAD BUTTON
  (trigger, face, DPad) use `uap input hold <GamepadKey>`** -- it takes the viewport route and
  reaches Enhanced Input. `rc InjectGamepad` routes buttons through Slate BY DESIGN (a face/DPad
  press is also how UMG focus nav is driven), so it reaches gameplay input only while the PIE
  viewport holds Slate focus, and otherwise looks exactly like a dead feature. Sticks and
  keyboard keys already take the viewport route. Key names are exact
  FKeys (`W`, `C`, `LeftControl`, `SpaceBar`, `OculusTouch_Left_Thumbstick_Y`).
- If input starts behaving oddly mid-session (a pawn stuck crouched, movement that will not
  stop), run `uap input release` -- it clears every hold AND flushes any key the engine still
  has down. `uap input status` shows what is held and whether it is really `down`.
- For anything SUB-SECOND (judder, a short wind-up, a one-frame pop), use
  `uap sample start <object> <property> --seconds N` -- it records per frame in-engine.
  Two `uap exec` reads a second apart cannot see it.
- Read logs with `uap log cursor` before the action and `uap log since <c> --grep RE`
  after, so the evidence lands in the report and targets the right editor. Do not shell-tail
  `Saved/Logs/*.log`.
- If the behavior is HMD-only (OpenXR input, an `IsHeadMountedDisplayEnabled()` branch such as
  a world-space VR screen), start with `uap pie start --mode vr` -- flat PIE takes neither path,
  so the bug will look absent.
- The `uap` CLI needs no MCP tools; it works in any session as long as the editor is up
  (`uap status` to check; launch the editor if it is down). Run `uap help` for the verb
  catalog + recipes -- don't reverse-engineer by dumping `dir()` on the subsystem.
- For a UI screenshot use `uap screenshot <abs.png>` (composites 3D + UMG + CommonUI + Slate).
  Do NOT use HighResShot or the MCP screenshot tool -- those capture the 3D scene only, no UI.
- **Five tooling traps that silently give a WRONG answer.** They are not flaky tools, they are
  LAYER MISMATCHES -- the test runs at a different layer than the thing being tested (below Slate,
  aimed at the wrong Slate user, outside the focused window, outside the game window, or before the
  work has happened). All five look like product bugs. **THREE OF THE FIVE ARE THE SAME HEADLINE
  RISK: input you injected silently never arrives** -- wrong layer, right layer/wrong user, or a
  throttled unfocused editor. **State queries are unaffected by all but the queued-ack one** --
  prefer reading engine state over simulating input and inferring from what you see. Full
  write-up: the "Layer mismatches" section of `.claude/commands/agentplayertest.md`.
  - **An unfocused editor throttles to exactly 3.0 fps**, so any timing measured then is void (a
    chase watched at 3 fps looked like broken AI and produced a ticket that was not real). Force
    PIE window focus, then CONFIRM the rate in-engine (`WorldDeltaSeconds`, or the `hz` a sampler
    reports) BEFORE recording a number. `Slate.bAllowThrottling 0` alone is NOT enough -- foreground
    focus is the gate. `uap sample` / `uap input hold` are immune: no CLI calls during the window.
    uap now says this AT the number: any rate <= 5 fps comes back `throttled:true` + a `warning`
    (report diag, perf stats, baselines, and `sample` via its own `hz`), and the HTML report
    renders it too. If focus cannot be taken at all (SetForegroundWindow False /
    GetForegroundWindow 0), report the timing as UNMEASURABLE rather than using it.
  - **`InjectKey` never reaches Slate input pre-processors or focus handlers.** It enters below the
    pre-processor chain, so anything registered via `RegisterInputPreProcessor` (e.g. an analog or
    virtual cursor) and anything routed by Slate focus never sees it -- silently, which looks
    exactly like a broken feature. Not a blanket limitation: it works fine for gameplay input
    routed through Enhanced Input (menus, bound actions). It fails at ONE layer. For cursor, focus
    or pre-processor work use real OS input (`keybd_event`) to the PIE window, or query state
    instead (`GetDesiredFocusTarget()`, `HasFocusedDescendants`).
  - **Injected input aimed at the WRONG SLATE USER is discarded -- right layer, wrong user.**
    Slate stamps a USER index on every event and handlers filter on it
    (`FAnalogCursor::IsRelevantInput` is `GetOwnerUserIndex() == InputEvent.GetUserIndex()`,
    engine `AnalogCursor.cpp:192`), so a stick drive against an analog/virtual cursor moved it
    zero pixels while reporting success -- and nearly became a bug filed against a working fix.
    This is NOT the trap above: that one is the wrong LAYER, this is the right layer with the
    wrong USER. Use `uap input axis <Key> <v> --user <N>` to drive the SLATE route as that user;
    without `--user` the sample goes to the game viewport, BELOW the pre-processor chain, so
    dropping the flag and retrying gets you `ok` and no movement. Results carry
    `route: slate|viewport` (+ `user_index`); a `--user` nobody is registered on is refused with
    the valid indices listed, not discarded.
  - **`uap screenshot` captures only the EDITOR viewport.** A separate Standalone PIE window is not
    captured -- you get a plausible-looking image of the wrong thing. Use an OS `PrintWindow`
    capture of that window, which also works while the game is PAUSED.
  - **An operation that acks a QUEUED request, not a completed one.** The tell: *a call that
    returns success faster than the work could plausibly have finished acked a queue, not a
    result.* `uap pie stop` used to answer ok:true while PIE kept running -- the stop landed
    before the queued start had created the play world, so it did nothing and PIE came up ~4s
    later. Poll the authoritative completion signal (and check it can actually SEE the state you
    care about), bounded, failing loudly on timeout. `pie stop` now waits and confirms; `pie
    start` still returns at once by design and says so (`queued:true, confirmed:false`) -- follow
    it with `uap pie wait <sec>`.
- **"this editor's plugin has no `<Verb>` ... sync and rebuild `<project>`" is a TOOLING version
  gap**, not a broken editor and not a product bug: the `uap` CLI is shared by every project while
  each project vendors its own plugin copy. Flat `uap pie start` degrades to the legacy verb on its
  own; `pie start --mode vr`, `uap input hold/axis`, `uap sample` and helper NAMES need the plugin
  rebuilt for this project. Do not retry it as if it were flaky.
- **A check that does not run the same thing as the gate it stands in for is not a check.**
  Matching command TEXT is not enough: a pre-push hook ran the identical `ruff check` line CI runs
  and reported "clean" while CI failed, because `ruff>=0.5` let the two resolve different versions.
  Pin the tool version in any local gate, and ask what quantity your confirmation signal measures --
  not just what it returns when things go well.
- Run the CLI via this project's `uap.ps1` so commands target THIS editor; calling it with no
  `--project` while another editor is open cross-targets the wrong one.
