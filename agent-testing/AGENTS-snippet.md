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
  drops a latched key. VR locomotion is an analog AXIS, not a button. Key names are exact
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
- Run the CLI via this project's `uap.ps1` so commands target THIS editor; calling it with no
  `--project` while another editor is open cross-targets the wrong one.
