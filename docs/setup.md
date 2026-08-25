# Setup

Follow these in order. Steps 1-2 are once per machine; steps 3-6 are once per project.

If you are joining a project that already uses UnrealAgentPlayer, the plugin and `uap.ps1`
are already in its source control -- you only need **step 1**, then jump to
[Verify](#7-verify). Nothing else is yours to install.

## Prerequisites

- Windows 10/11, UE 5.6, 5.7, 5.8, or the Meta 5.7 fork
- Python 3.11+ on `PATH` (`python --version` to check)
- Git
- An MCP client if you want the MCP tools (Claude Code CLI, Claude Desktop, Cursor). The
  `uap` CLI does **not** need one.

---

## 1. Clone this repo and build its venv

Once per machine. The CLI lives outside your game project and is never in your project's
depot, so every teammate does this themselves.

```powershell
git clone https://github.com/ImagineTheGames/UnrealAgentPlayer.git
cd UnrealAgentPlayer\mcp-server
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

Add the `windows` extra if you want editor-menu UIAutomation:
`.venv\Scripts\pip install -e ".[dev,windows]"`

### Tell the launcher where the clone is

`uap.ps1` looks for this repo in three places, in order: `$env:UAP_HOME`, a relative path
recorded when the launcher was generated, then a checkout sitting next to the game project.
Cloning it beside your project needs no configuration. Anywhere else, set `UAP_HOME` once:

```powershell
[Environment]::SetEnvironmentVariable('UAP_HOME','D:\dev\UnrealAgentPlayer','User')
```

Then **reopen your shell** -- an already-open terminal keeps the old environment.

## 2. Install the plugin into your UE project

```cmd
mklink /J C:\Path\To\YourProject\Plugins\UnrealAgentPlayer D:\dev\UnrealAgentPlayer\Plugin
```

For teams (Perforce, Git), **copy** `Plugin/` into `YourProject/Plugins/UnrealAgentPlayer`
as real files and commit them instead. That is what travels to other workstations; the
`mklink /J` junction above is a local-dev convenience only.

## 3. Enable the UE plugins

1. Open the project in UE.
2. Edit > Plugins. Ensure these are enabled:
   - Remote Control API
   - Python Editor Script Plugin
   - Unreal Agent Player
3. Restart the editor.

## 4. Enable Python Remote Execution

1. Edit > Project Settings > Python.
2. Check **Enable Remote Execution**.
3. Restart the editor.

## 5. Allow remote function calls (UE 5.8+ only)

**Skip this on UE 5.6/5.7 and the Meta 5.7 fork** -- the setting does not exist there.

UE 5.8 added `bAllowAnyRemoteFunctionCall`, defaulting to **false**, which blocks every
call this tool makes. Miss it and the very first command fails with:

```
Executing function 'UAPAgentSubsystem GetPluginVersion' is not allowed by remote control
settings. (see 'Custom Allowed Remote Function Calls' or 'Allow Any Remote Function Call')
```

Edit > Project Settings > Plugins > **Remote Control** > *Security*, and either:

- Tick **Allow Any Remote Function Call** -- simplest, and what most projects do. Or in
  `Config/DefaultRemoteControl.ini`:

  ```ini
  [/Script/RemoteControlCommon.RemoteControlSettings]
  bAllowAnyRemoteFunctionCall=True
  ```

- Or, to stay narrow, leave it off and add each `UAPAgentSubsystem` function you use to
  **Custom Allowed Remote Function Calls**. More setup, and it breaks whenever a new verb
  starts calling a function you did not list.

Scope note: the RC HTTP server binds localhost only and Remote Control is editor tooling,
not a shipping runtime. But "allow any" does mean any function call over that port, so do
not enable it on a machine exposing the port to an untrusted network.

## 6. Do NOT set the Remote Control port

You do not need to know the port, and setting it by hand is the most common way to get
stuck here.

- Each editor assigns itself a port at startup (`30011 + crc32(projectName) % 80`, probing
  upward for a free one), so two editors on one machine never collide on the stock 30010.
- `uap status` reports the live port as `rc_port`, and the CLI discovers it per editor. Set
  `UAP_RC_PORT` only if you must force one.
- A project may pin its own port in `Config/DefaultRemoteControl.ini` under
  `[/Script/RemoteControlCommon.RemoteControlSettings]`. That pin wins and the plugin
  leaves it alone. Do not "fix" a mismatch by editing that file.

If you do go looking in Project Settings anyway, two traps:

- The setting is **Remote Control HTTP Server Port**. *Remote Control Web Interface HTTP
  Port* is the separate web UI and is unrelated.
- `URemoteControlSettings` is `UCLASS(config = RemoteControl)` **without** `defaultconfig`,
  so the per-user file `Saved/Config/<Platform>/RemoteControl.ini` **overrides** the
  project's `Config/DefaultRemoteControl.ini`, and anything you touch in the Project
  Settings UI is written there permanently. That is why a hand-edit to the project ini
  appears to "reset" on every restart. Close the editor, delete that entry from the
  `Saved/` copy, reopen.

## 7. Verify

With the editor running, from your project root:

```powershell
powershell -NoProfile -File uap.ps1 status
```

Expected:

```json
{"ok": true, "rc_reachable": true, "plugin_version": "0.0.1", "rc_port": 30035}
```

`rc_port` will differ per project -- any number is fine, as long as `rc_reachable` is true.

If you also registered the MCP server (below), ask your client to call `bridge_status` and
expect `ue_running: true`, `rc_reachable: true`, `remote_exec_reachable: true`.

### If it does not work

| Symptom | Cause | Fix |
| --- | --- | --- |
| `could not find the UnrealAgentPlayer repo` | Clone missing, or not where the launcher looks | Step 1, including `UAP_HOME` + reopen the shell |
| `python venv not found` | Clone exists, venv never built | Step 1's `python -m venv` + `pip install` |
| `...is not allowed by remote control settings` | UE 5.8 default | Step 5 |
| `rc_reachable: false` | Editor not running, or Remote Control API not enabled | Start the editor; step 3 |
| `remote_exec_reachable: false` | Enable Remote Execution unchecked | Step 4 |
| Commands hit the *wrong* editor | Called the CLI directly with no `--project` | Run the project's own `uap.ps1`, which pins `UAP_PROJECT` |

## 8. Register MCP in your client (optional)

Only needed for the MCP tools. See [claude-client-config.md](claude-client-config.md). The
`uap` CLI works without it.

## 9. Agent test command (`/AgentPlayerTest`)

To give an agent a one-command, report-backed way to verify runtime behavior, install the
test kit into your project:

```powershell
powershell -NoProfile -File Install-AgentTest.ps1 -ProjectRoot C:\Path\To\YourProject
```

It drops `.claude/commands/agentplayertest.md` and a `uap.ps1` launcher at the project
root, and prints the AGENTS rule to paste. See [agent-testing.md](agent-testing.md).

**Commit both files to your project's source control.** They contain no machine-local
paths: `uap.ps1` resolves the project root from its own location, the engine from the
`.uproject` `EngineAssociation` at run time, and this repo as described in step 1. A
teammate who syncs the project to a different drive and keeps their engine somewhere else
needs no edits -- only step 1.

If they are missing from the depot, every agent that follows your `AGENTS.md` fails at its
first step, with an error that does not say why.
