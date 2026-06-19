## Verifying runtime game behavior

To verify any runtime game behavior (does X animate / open / trigger / show the right
text), run `/AgentPlayerTest <question>`. It drives the `uap` CLI (`uap.ps1` at the
project root), which talks to the running editor over Remote Control + Python remote-exec
and renders an HTML report.

- A verification is NOT complete until `uap report finish` emits a report at
  `~/.uap-reports/<ts>/index.html`. Cite the path.
- Never conclude a behavior "works" from a screenshot alone. Read concrete state -- a
  project test helper (`uap rc CallTestHelper`), an actor/anim property, an anim-bone
  delta across two samples, or a log line. A non-zero speed with a frozen bone is a FAIL.
- The `uap` CLI needs no MCP tools; it works in any session as long as the editor is up
  (`uap status` to check; launch the editor if it is down). Run `uap help` for the verb
  catalog + recipes -- don't reverse-engineer by dumping `dir()` on the subsystem.
- For a UI screenshot use `uap screenshot <abs.png>` (composites 3D + UMG + CommonUI + Slate).
  Do NOT use HighResShot or the MCP screenshot tool -- those capture the 3D scene only, no UI.
- Run the CLI via this project's `uap.ps1` so commands target THIS editor; calling it with no
  `--project` while another editor is open cross-targets the wrong one.
