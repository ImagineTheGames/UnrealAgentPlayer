# Agent testing with the `uap` CLI

`/AgentPlayerTest` lets an agent answer "does this actually work in play?" by driving the
running editor and rendering a self-contained HTML report. It is MCP-free: a thin
PowerShell launcher (`uap.ps1`) calls the `uap` Python CLI, which talks to the editor over
Remote Control HTTP + Python Remote Execution.

## Install into a project

```
powershell -NoProfile -File Install-AgentTest.ps1 -ProjectRoot C:\Path\To\YourProject
```

This writes `.claude/commands/agentplayertest.md` and `uap.ps1` (with the venv python baked
in), and prints the AGENTS rule to paste (add `-AppendAgents` to append it automatically,
`-Force` to overwrite existing files). Prereq: the mcp-server venv is built
(`cd mcp-server; python -m venv .venv; .venv\Scripts\pip install -e ".[dev]"`). If you move
the repo, set `$env:UAP_HOME` to its root and the launcher will prefer that.

## The contract

A verification is not done until `uap report finish` emits `~/.uap-reports/<ts>/index.html`
and the agent cites the path. Reads of concrete state (test helper, actor/anim property,
bone delta, log line) settle a question -- never a screenshot alone.

## `uap` verbs

- `uap status` -- preflight; `{ok, rc_reachable, plugin_version}`.
- `uap report start "<task>" [--project X] [--require-screenshot]` -- `--require-screenshot`
  makes `finish pass` auto-downgrade to fail unless a screenshot is attached.
- `uap report assert "<label>" pass|fail "<evidence>"` / `note "<text>"` / `finish pass|fail "<summary>"`.
- `uap report diag --project <X>` -- capture editor diagnostics (env: plugin version, level, PIE
  state) AND perf (frame_ms / draw_ms / gpu_ms / fps, from the plugin's GetStatGroupText) into the
  report via `exec` (reads the right editor even if another squats the RC port). Run it while PIE
  is live for the game's frame rate. **A rate at or below 5 fps is answered on the spot:** the
  result gains `throttled: true` + a `warning` saying it is the editor's not-foreground throttle
  (it pins ~3.0 fps), not the game, and the same explanation is rendered into the HTML report.
  Same annotation on `perf_stat`, on both perf baselines, and on `uap sample` when the sampler's
  own `hz` comes back at that rate. Do not report a sub-5-fps number as a performance finding --
  see trap #1 in `agent-testing/agentplayertest.md`.
- `uap pie start [--mode flat|vr]` / `uap pie wait <sec>` / `uap pie stop [--timeout 30]` --
  start / await / stop Play-In-Editor (wraps the version-correct engine call; agents never touch
  the raw subsystem). **`start` and `stop` are deliberately asymmetric.** `start` only QUEUES the
  session and returns at once (`queued:true, confirmed:false`) -- the play world does not exist
  yet, so `pie wait <sec>` is the confirmation half and is not optional. `stop` BLOCKS until the
  teardown is confirmed (it polls `IsPIEInProgress`, i.e. live **or** queued) and returns
  `ok:false` on timeout saying the editor is not free, because `stop` is the handover point: the
  lease, the next agent and `report finish` all act on "the editor is free". `ok:true` +
  `stopped:true` is the only "PIE is gone". See known-issues #25.
  `--mode vr` starts the editor's **VR Preview** instead: the HMD code path (OpenXR input, every
  `IsHeadMountedDisplayEnabled()` branch) that flat PIE never takes. Requires a connected headset
  and fails with a reason if there is none -- it never silently falls back to flat. It also needs a
  plugin copy new enough to export `StartPIEMode`, and refuses (rather than downgrading to flat) on
  an older one; **flat** `pie start` falls back to the legacy `StartPIE` by itself. Every project
  vendors its own plugin copy while sharing this one CLI, so that skew is normal -- see
  "CLI/plugin version skew" in `capabilities.md` and known-issues #23 before adding a new verb.
- `uap exec "<python>"` -- arbitrary `import unreal; ...` in the editor.
- `uap rc <FunctionName> [key=value ...]` -- call a UAP_Preset UFUNCTION (use `uap exec` for nested args).
- `uap read-ui` -- dump viewport UMG text. `uap screenshot <file> [--caption ...]` -- capture
  (requires a live PIE frame; reports `ok:false` if no file lands), auto-attached.
- `uap input hold <Key> --seconds N` / `uap input axis <AxisKey> <v> --seconds N` /
  `uap input release [<Key>]` / `uap input status` -- **sustained** input. `rc InjectKey` is ONE
  event: the round-trip is ~1s so re-injecting per poll cannot cover a sub-second window, and any
  `FlushPressedKeys` silently drops a latched key. These re-assert the input every frame inside the
  engine and then release. `axis` is the VR locomotion verb -- thumbsticks are analog axis FKeys,
  not buttons. Both return immediately (the hold continues in-engine) so you can read state WHILE
  it is held; `--wait` blocks instead. All four return JSON, so a refusal says why -- and a
  refused call presses nothing. Bare `uap input release` is the RECOVERY hatch: it clears every
  hold AND flushes any key the engine still has down, so a stuck key never needs a PIE restart.
- `uap sample start <object> <property> --seconds N` / `uap sample read` -- record a property
  once per frame in-engine and get the series plus `delta_mean`/`delta_max`/`delta_p95`/`hz`.
  This is how you measure anything sub-second (judder, a 0.6s wind-up, a one-frame pop);
  `object` accepts `PlayerPawn`/`PlayerController`/`PlayerCameraManager`, an actor name, or an
  object path, and `property` accepts a dot path or a computed leaf like `WorldLocation`.
- `uap log cursor` / `uap log since <cursor> [--grep RE]` / `uap log tail [--lines N]
  [--grep RE] [--category C] [--verbosity V]` -- read the editor log through the plugin, so log
  evidence lands in the report and targets the SAME editor as every other verb. Grab a cursor
  BEFORE driving the condition, then read `since` it.
- `uap helpers [--grep RE] [--names]` -- list the project's test helpers with their arg schemas.
- `--agent <token>` and `--project <name>` are accepted by EVERY verb (ignored by the ones that
  do not touch the editor), so you can pass the same flags on every call in a run without
  special-casing which verb takes what.

## Writing a project-specific preset

`/AgentPlayerTest` is the general, question-driven form. For a system you test repeatedly,
bake a preset:

1. Copy `.claude/commands/agentplayertest.md` to a new command, e.g. `.claude/commands/mytest.md`.
2. Hard-code the scene (which level to load), the acceptance checklist (one `uap report assert`
   per check), and the test helpers it relies on.
3. Expose game-truth reads as C++ test helpers so the preset reads them with
   `uap rc CallTestHelper Name=... JsonArgs={}` (see `docs/writing-test-helpers.md`).

The preset stays in your project; the plugin only ships the generic command.

## Verifying the PIE-stop fix (known-issues #25)

`uap pie stop` used to answer `ok:true` while PIE kept running. The fix has two halves and they
are verified separately, because the plugin half needs a rebuild and the CLI half does not.

**Prerequisite for the full check:** the project's vendored plugin copy must be synced and rebuilt
(`Restart-Editor.ps1`) so the editor exports `StopPIEEx` and `IsPIEInProgress`. Without it the CLI
still confirms the stop, but at the reduced guarantee it labels `degraded: true`.

1. **Preflight.** `uap status` -> `rc_reachable:true`. Confirm the plugin is new enough:
   `uap rc IsPIEInProgress` must return `{"ok": true, "result": false}` on an idle editor -- a 404
   / "this editor's plugin has no ..." means the rebuild has not landed.
2. **The original race, on purpose.** Start PIE and stop it IMMEDIATELY, with no wait in between:
   `uap pie start` then, as the very next command, `uap pie stop`.
   * Expected now: the stop reports `cancelled_queued_start: true` (it consumed the queued start
     rather than racing it) and `stopped: true`.
   * The bug: `{"ok": true, "result": true}` returned in well under a second, followed ~4s later by
     `Creating play world package` in the log and a live PIE window.
3. **Check the editor, not the JSON.** Two independent reads, because the whole point is that the
   verb's own answer was the thing lying:
   * the OS window title on the editor's PID -- `<Project> Preview [NetMode: ...]` means PIE is
     LIVE; `<Project> - Unreal Editor` means it is gone;
   * `uap log tail --grep "Shutting down PIE online subsystems"` (and `--grep "Creating play world
     package"` for anything that came up after your stop).
4. **The normal path.** `uap pie start` -> `uap pie wait 12` -> `uap pie stop`. The stop should
   report `was_playing: true`, `stopped: true`, and a `waited_seconds` that is non-zero: teardown
   takes at least a tick, so an instant return is itself the symptom.
5. **The timeout path** (optional, needs a wedged teardown; skip if you cannot produce one). With
   `--timeout 2` against a session that will not tear down, the verb must exit non-zero with
   `stopped: false` and an error saying the editor is NOT free -- never `ok:true`.
6. **The lease guard.** While PIE is live:
   `uap lease acquire exclusive --reason pie --agent verify` then
   `uap lease release --agent verify` -> must REFUSE with `pie_live: true` and exit 1, and
   `uap lease status` must still show `verify` holding it. Stop PIE, release again -> succeeds.
   (`--force` releases anyway; use it only to hand over a live session deliberately.)
