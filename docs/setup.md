# Setup

## Prerequisites

- Windows 10/11, UE 5.6, 5.7, or Meta 5.7 fork
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
3. Edit > Project Settings > Plugins > Remote Control HTTP.
4. Keep default port 30010.
5. Restart editor.

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

It drops `.claude/commands/agentplayertest.md` and a `uap.ps1` launcher (venv python baked
in) at the project root, and prints the AGENTS rule to paste. See `docs/agent-testing.md`.
