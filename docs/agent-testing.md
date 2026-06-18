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
  is live for the game's frame rate.
- `uap pie start` / `uap pie wait <sec>` / `uap pie stop` -- start / await / stop Play-In-Editor
  (wraps the version-correct engine call; agents never touch the raw subsystem).
- `uap exec "<python>"` -- arbitrary `import unreal; ...` in the editor.
- `uap rc <FunctionName> [key=value ...]` -- call a UAP_Preset UFUNCTION (use `uap exec` for nested args).
- `uap read-ui` -- dump viewport UMG text. `uap screenshot <file> [--caption ...]` -- capture
  (requires a live PIE frame; reports `ok:false` if no file lands), auto-attached.

## Writing a project-specific preset

`/AgentPlayerTest` is the general, question-driven form. For a system you test repeatedly,
bake a preset:

1. Copy `.claude/commands/agentplayertest.md` to a new command, e.g. `.claude/commands/mytest.md`.
2. Hard-code the scene (which level to load), the acceptance checklist (one `uap report assert`
   per check), and the test helpers it relies on.
3. Expose game-truth reads as C++ test helpers so the preset reads them with
   `uap rc CallTestHelper Name=... JsonArgs={}` (see `docs/writing-test-helpers.md`).

The preset stays in your project; the plugin only ships the generic command.
