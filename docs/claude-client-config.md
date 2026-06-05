# Claude Code / Desktop configuration

## Claude Code (`~/.claude.json`)

```json
{
  "mcpServers": {
    "unreal-agent-player": {
      "command": "python",
      "args": ["-m", "unreal_agent_player"]
    }
  }
}
```

If you installed the MCP server in a venv, use the venv's Python directly:

```json
{
  "mcpServers": {
    "unreal-agent-player": {
      "command": "D:\\dev\\unreal-agent-player\\mcp-server\\.venv\\Scripts\\python.exe",
      "args": ["-m", "unreal_agent_player"]
    }
  }
}
```

## Claude Desktop

Same schema; path is `%APPDATA%\Claude\claude_desktop_config.json` on Windows.

## Verify

In a new Claude session, type: `bridge_status` — you should get a diagnostic JSON response.
