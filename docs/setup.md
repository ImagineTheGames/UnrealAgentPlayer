# Setup

## Prerequisites

- Windows 10/11, UE 5.6, 5.7, 5.8, or Meta 5.7 fork
- Python 3.11+
- MCP client (Claude Code CLI, Claude Desktop, Cursor)

## Install plugin into a UE project

```cmd
mklink /J C:\Path\To\YourProject\Plugins\UnrealAgentPlayer D:\dev\unreal-agent-player\Plugin
```

For teams (e.g. Perforce), prefer copying the `Plugin/` folder into
`YourProject/Plugins/UnrealAgentPlayer` as real files and adding them to source control --
that is what travels to other workstations. The `mklink /J` junction above is a local-dev
convenience only.

## Install the MCP server

```bash
cd D:\dev\unreal-agent-player\mcp-server
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Enable UE plugins in your project

1. Open the project in UE.
2. Edit > Plugins. Ensure these are enabled:
   - Remote Control API
   - Python Editor Script Plugin
   - Unreal Agent Player
3. Restart editor.

## Enable Remote Control + Python Remote Exec

1. Edit > Project Settings > Python.
2. Check Enable Remote Execution.
3. Restart editor.

**Do not set the Remote Control port by hand.** You do not need to know it, and picking one
by hand is the single most common way to get stuck here.

- The plugin assigns each editor its own RC HTTP port at startup
  (`30011 + crc32(projectName) % 80`, probing upward for a free one), so two editors on one
  machine never collide on the default 30010.
- `uap status` reports the live port as `rc_port`, and the CLI discovers it per editor. Set
  `UAP_RC_PORT` only if you must force a specific one.
- A project may pin its own port in `Config/DefaultRemoteControl.ini` under
  `[/Script/RemoteControlCommon.RemoteControlSettings]`. If it does, that pin wins and the
  plugin leaves it alone. Do not "fix" a mismatch by editing that file.

If you do go looking in Project Settings, two gotchas:

- The setting is **Remote Control HTTP Server Port**. *Remote Control Web Interface HTTP
  Port* is the separate web UI and has nothing to do with this.
- `URemoteControlSettings` is `UCLASS(config = RemoteControl)` **without** `defaultconfig`, so
  the per-user file `Saved/Config/<Platform>/RemoteControl.ini` **overrides** the project's
  `Config/DefaultRemoteControl.ini`, and anything you touch in the Project Settings UI is
  written there permanently. That is why a hand-edit to the project ini "resets" on restart.
  Close the editor, delete the `Saved/Config/<Platform>/RemoteControl.ini` entry, reopen.

## Register MCP in your client

See `claude-client-config.md`.

## Verify

In Claude Code, after the editor is running:

> Call the bridge_status tool.

Expected: `ue_running: true`, `rc_reachable: true`, `remote_exec_reachable: true`, `plugin_version: 0.0.1`.

## Agent test command (`/AgentPlayerTest`)

To give an agent a one-command, report-backed way to verify runtime behavior, install the
test kit into your project:

```
powershell -NoProfile -File Install-AgentTest.ps1 -ProjectRoot C:\Path\To\YourProject
```

It drops `.claude/commands/agentplayertest.md` and a `uap.ps1` launcher at the project root,
and prints the AGENTS rule to paste. See `docs/agent-testing.md`.

**Commit both files to your project's source control.** They contain no machine-local paths:
`uap.ps1` resolves the project root from its own location, the engine from the `.uproject`
`EngineAssociation` at run time, and this repo from `$env:UAP_HOME`, a relative path recorded
at install time, or a sibling checkout. A teammate who syncs the project to a different drive
and keeps their engine somewhere else needs no edits -- only the one-time clone + venv below.

If they are missing from the depot, every agent that follows your `AGENTS.md` fails at the
first step, which is not obvious from the error.
