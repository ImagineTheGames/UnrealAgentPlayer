# Agent Studio — game ideas

The slate of games we want to build with this toolchain, and what each one would cost to build.

An idea earns a place here by clearing two bars at once: it is a game worth making, **and** it puts a
load on the agent loop that we can name. The second bar is the useful one. A game that only exercises
what already works teaches us nothing about the toolchain; a game whose every loop is blocked on
unbuilt tooling is a research project wearing a design doc. The ideas below are ordered by how much
game you get per unit of missing tooling.

Each entry says what UnrealAgentPlayer covers **today** (verbs that exist, in
[capabilities.md](capabilities.md)) and what the game would **force us to build**. Nothing here is
committed work; the "forces" lines are the honest price tag, not a roadmap promise.

---

## 1. Multiplayer side-scrolling jet shooter

**Pitch.** Two-to-four player co-op side-scroller with a roster of real jets — F-16, F-14, A-10 —
bought and upgraded between missions. The reference is Capcom's SNES *U.N. Squadron* (*Area 88* in
Japan): fixed-scroll stages, a shop between sorties, and a plane you get attached to. What it never
had was another player on the screen. That is the whole pitch: the co-op version of that game.

**Why it is the right first title.** A side-scrolling stage is a **timeline**, and a timeline is the
one thing an agent can assert against without ambiguity. Scroll speed × elapsed time is position; the
mid-boss spawns at a fixed distance; the same input sequence produces the same run. `input_sequence`
is already a timeline of inputs — the genre and the tool have the same shape. The state surface is
small enough to read exhaustively (position, velocity, HP, weapon, score) and there is no navmesh, no
physics soup, and no open world to get lost in.

**Covered today**

| Need | Verb |
| --- | --- |
| Two clients + a host, driven independently | `game_launch` / `game_attach` / `game_list` / `game_stop`, each on its own RC port; every play tool takes `target=<instance_id>` |
| The scripted run | `input_sequence`, plus `HoldKey` / `HoldAxis` for sustained thrust and fire |
| Bullet-hell frame cost | `perf_stat`, `perf_baseline_save` / `perf_baseline_compare` — the genre's real risk is hundreds of live projectiles, and this is exactly the guard for it |
| Per-stage evidence | `report_*` → tabbed HTML run report with the screenshot gallery |

Two standalone instances plus the editor on distinct RC ports is runtime-verified, so the hard part
of a multiplayer test rig — addressing each client unambiguously — is already done.

**What it forces us to build**

- **A cross-instance assertion.** Today the agent reads instance A, reads instance B, and compares
  the two in its own head. Desync is the bug class this game generates, and there is no verb aimed at
  it. The missing one samples the *same* helper on N targets at one moment and reports divergence —
  one call, one answer, no agent bookkeeping.
- **Sampling against a shared clock.** `StartPropertySample` / `ReadPropertySample` are per-instance.
  Two instances sampled against a common time origin is not currently a thing, and "both clients
  agree" is meaningless without it.
- **Nothing for a shipped build.** `UnrealAgentPlayerRuntime` is `DeveloperTool`, `Win64`, and
  excluded from Shipping. Fine for the dev loop; it does not test what players install.

**First slice an agent can close the loop on.** One stage, one jet, two clients. Scripted input
timeline on both. Assert both clients report the same boss HP at the same scroll distance — and that
neither dropped below the frame budget while doing it.

---

## 2. Top-down action-adventure

**Pitch.** Single-player top-down Zelda-like: an overworld, dungeons, keys and locked doors, items
that unlock traversal, a boss per dungeon.

**Why it belongs here.** The genre is a **state machine with a map**, and every edge in that machine
is a named boolean. Door locked or open, item held or not, boss dead or alive, room entered. That is
precisely the `AgentTestHelper` shape — `IsDoorOpen(DoorTag)` is already the worked example in
[`examples/schoolsout`](../examples/schoolsout/). No genre converts more cleanly into a regression
suite: every dungeon is a repeatable input sequence with a known end state, and the sequence-break
bugs it generates (item B obtained before item A) are exactly the reproduce-and-verify loop in
[use-cases.md](use-cases.md) #2.

**Covered today**

| Need | Verb |
| --- | --- |
| Progression flags | `helper_list` + `CallTestHelper` |
| Skip the first four rooms and test the fifth | `actor_set_property` / `exec_python` to put the world in a known state, then act — [use-cases.md](use-cases.md) #6 |
| The deterministic room clear | `input_sequence` |
| Evidence a human will actually look at | `screenshot_viewport` (composited game + UMG by default) into a `report_*` gallery |

**What it forces us to build**

- **Nothing, for the HUD — and that is the point.** Hearts, rupees, and item slots are *images*.
  `read_viewport_ui` reads UMG text and focus, so there is nothing there for it to read. The
  temptation is an image-region assertion; the right answer is the one this repo already argues for —
  perceive with the cheapest reliable channel, so the project exposes HUD state as helpers and the
  agent asserts on `GetHearts()`, not on pixels. This game is the forcing function for that
  discipline, not for a new tool.
- **Navigation, which is an agent problem and not a plugin problem.** A scripted timeline works in a
  side-scroller because the stage moves the player. In a dungeon the agent has to *find* the door.
  That is pathing — reading nav data through `exec_python`, or teleporting to a waypoint and testing
  from there — and it is the first place the studio hits "the agent needs a brain" rather than "the
  toolchain needs a verb". Worth hitting early, on a small map.
- **Save-game round-trip.** Save corruption is a real bug class for this genre and there is no verb
  for it. Probably helpers again (`SaveAndReload()`, then re-assert progression), which is a
  satisfying answer.

---

## 3. Real-time strategy, phone-first

**Pitch.** StarCraft on a phone. Base building, unit production, drag-select and command, a 1v1
ladder.

**Why it is interesting for a studio built on agents.** In the other two titles the agent is a
*tester*. Here the same state-read-and-act loop is also **an opponent and a balance harness**. Run a
few hundred matches overnight, read win rates by faction and by build order, tune costs, run them
again. No other genre on this list turns the testing infrastructure into a design instrument, and an
RTS's state is unusually readable — unit counts fall straight out of `actor_find` by class, economy
out of helpers.

**What it forces us to build.** This is the honest part: today the toolchain cannot drive this game
at all on its target platform.

- **Touch input does not exist.** The injection surface is keyboard, mouse, gamepad, and XR
  buttons/poses. There is no tap, no drag, no pinch — and a touch RTS is *nothing but* drag-select
  and pinch-zoom. This is a plugin change (a new UFUNCTION routing through the viewport client's
  touch path), not something the Python layer can add over existing verbs.
- **Nothing runs off Win64.** `UnrealAgentPlayerRuntime` carries `PlatformAllowList: [Win64]` and is
  explicitly excluded from Android. Driving a build on a device or emulator is a port: Remote Control
  over an ADB port-forward, no editor, no UIAutomation.
- **Batch runs assume a viewport.** Balance testing wants N matches at max speed with rendering off.
  Every loop we have assumes something is on screen — `stat unit`, screenshots, viewport-routed
  input. A `-nullrhi` server-only mode injecting at the gameplay layer instead of the viewport layer
  is a different execution model, not a flag.

**So build it PC-first.** Mouse and keyboard, on Windows, in the editor. Every gap above disappears,
the design work (which is the actual hard part of an RTS) proceeds in full, and touch becomes a port
undertaken once the game is worth porting. Committing to phone-first on day one buys a control scheme
we cannot test.

---

## Order, and why

1. **Jet shooter.** Smallest game, largest toolchain payoff: multi-instance driving is already built
   and verified, so one missing verb stands between us and a self-testing multiplayer game.
2. **Top-down adventure.** The best regression-suite shape we have, and the cheapest place to hit the
   agent-navigation problem on a map small enough to reason about.
3. **RTS, on PC.** Largest design lift and three real pieces of platform work before a phone build is
   drivable. Worth doing, worth not doing first.

## Adding an idea here

Say what the game is, then answer the two questions that make the entry useful: which verbs in
[capabilities.md](capabilities.md) already cover its loop, and what it would force us to build. An
entry with no second answer is a game we can already test — good news, and it belongs in
[use-cases.md](use-cases.md) instead.
