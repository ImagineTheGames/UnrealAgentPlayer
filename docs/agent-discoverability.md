# Agent discoverability -- making UAP easy to use

Field feedback (2026-06-18): an agent adding a feature had to discover input tools by dumping
`dir()` on the subsystem and reverse-engineering the `read-ui -> inject_mouse` chain, instead of
reading them. UAP is meant to be the *easy* path; the discovery surface was too thin.

## Diagnosis

1. **The skill (`agentplayertest.md`) is the only thing an agent loads** -- the rich docs
   (`capabilities.md`, `known-issues.md`, `agent-testing.md`) live in this repo, which an agent
   working inside a *consuming* project never opens. So the skill must carry the recipes and
   point at the docs.
2. **No game-UMG interaction verb.** Clicking an in-game UMG button meant composing
   `read-ui` (for coords) + `InjectMouseMove` + `InjectMouseButton` by hand. The chain was
   implicit. (The only `ui_menu_click` that exists drives editor *Slate* menus via Windows
   UIAutomation -- useless for in-game UMG.)
3. **No catalog.** Discovery = read source. `uap <verb> --help` existed but no overview.
4. **`read-ui` gives `text`+`x`+`y` but no width/height (no center) and no widget id/class**, so
   agents can't target by widget name and click coords are top-left, not center.
5. **Side friction:** trimmed Python bindings (no `WidgetBlueprintLibrary`/subsystem getters ->
   widget-walk dead-ends); screenshots landing in a read-denied `Saved/`.

## Done (2026-06-18)

- **`uap help` / `uap tools`** -- full verb catalog + copy-paste recipes (click-by-label, key,
  XR button, test helpers, tab-via-exec). The catalog leads with recipes, not philosophy.
- **`agentplayertest.md` now opens with a "Quick recipes" block** + a pointer to `uap help` and
  to `docs/agent-testing.md` / `capabilities.md` / `known-issues.md`. Agents stop spelunking.
- **`uap click "<label>"`** -- one-call UMG button click (read-ui -> match text -> inject mouse).
  CLI-only (composes existing verbs). Live-verify pending; clicks the element's reported
  position (top-left) until read-ui exposes a center coord (see D).

## Planned (needs live-editor verification; do when editors are free)

- **A. Remaining UI verbs** (need plugin C++ + a rebuild):
  - `uap tab "<TabId>"` -- CommonUI `CommonTabListWidgetBase::SelectTabByID` (menus are
    tab-driven; highest-value single addition).
  - `uap nav up|down|left|right|accept|back` -- CommonUI focus navigation.
  - These need new plugin UFUNCTIONs (UMG hit-test + CommonUI tab/nav) + CLI verbs + a rebuild.
- **D. Richer `read-ui` + `uap ui-tree`** -- per-element width/height (so click targets center),
  a stable id/widget-name, and a hierarchy dump (names/classes/visibility) so agents target by
  widget name without Python.
- **E. Screenshot fix** -- correct `exists`/`path` reporting, and write (or auto-copy) to an
  agent-readable dir instead of `Saved/`.
- **F. Binding gap** -- document the trimmed Python bindings, or add `uap call <widget> <method>`
  so agents don't dead-end on `dir()`.

## Principle

Agents discover UAP by reading the skill. So: the skill carries the recipes, `uap help` carries
the catalog, and the common UI actions are first-class verbs -- never "compose these primitives,
which you'll find by dumping `dir()`."
