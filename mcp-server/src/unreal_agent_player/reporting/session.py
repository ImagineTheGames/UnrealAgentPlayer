from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


class ReportSession:
    def __init__(self, *, task: str, run_dir: Path, quote: str,
                 project: str | None = None, requires_screenshot: bool = True,
                 agent: str | None = None):
        self.task = task
        self.project = project
        self.quote = quote
        self.requires_screenshot = bool(requires_screenshot)
        self.status = "running"
        self.started = datetime.now()
        self.finished: datetime | None = None
        self.duration_s: float | None = None
        self.summary = ""
        self.env: dict[str, Any] = {}
        self.perf: dict[str, Any] | None = None
        self.notes: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.screenshots: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        # Who started this report, and who (if anyone) took the machine-wide slot away from it.
        # The slot is ONE per machine; see start_session.
        self.agent: str | None = agent
        self.superseded_by: dict[str, Any] | None = None
        # Not persisted: what THIS start displaced, so `report start` can warn about it once.
        self.displaced: dict[str, Any] | None = None
        self.run_dir = Path(run_dir)
        (self.run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        self._persist()

    # --- time helper ---
    @staticmethod
    def _hms(dt: datetime) -> str:
        return dt.strftime("%H:%M:%S")

    # --- curated / captured appends ---
    def add_assertion(self, label: str, passed: bool, evidence: str = "") -> None:
        self.assertions.append({"label": label, "passed": bool(passed), "evidence": evidence})
        self._persist()

    def add_note(self, text: str, section: str | None = None) -> None:
        self.notes.append({"text": text, "section": section})
        self._persist()

    def add_screenshot(self, src_path: str, caption: str = "",
                       provenance: str | None = None) -> str | None:
        # provenance = the project of the editor the shot was captured FROM (stamped by
        # `uap screenshot`). Used to reject a pass whose only proof is a shot of another editor.
        idx = len(self.screenshots)
        src = Path(src_path)
        if not src.exists():
            self.screenshots.append({
                "file": None, "caption": caption, "provenance": provenance,
                "t": self._hms(datetime.now()), "missing": True,
            })
            self._persist()
            return None
        rel = f"screenshots/{idx:03d}.png"
        shutil.copyfile(src, self.run_dir / rel)
        self.screenshots.append({
            "file": rel, "caption": caption, "provenance": provenance,
            "t": self._hms(datetime.now()), "missing": False,
        })
        self._persist()
        return rel

    def set_caption(self, ref: Any | None, caption: str) -> bool:
        if not self.screenshots:
            return False
        if ref is None:
            self.screenshots[-1]["caption"] = caption
            self._persist()
            return True
        # ref may be an int index or a filename string
        for i, sh in enumerate(self.screenshots):
            if ref == i or sh.get("file") == ref or sh.get("file") == f"screenshots/{ref}":
                sh["caption"] = caption
                self._persist()
                return True
        return False

    def add_tool_call(self, tool: str, args: dict[str, Any], *, ok: bool,
                      ms: int, error: str | None = None) -> None:
        self.timeline.append({
            "t": self._hms(datetime.now()), "tool": tool, "args": args,
            "ok": bool(ok), "ms": int(ms), "error": error,
        })
        self._persist()

    def set_perf(self, perf: dict[str, Any]) -> None:
        self.perf = perf
        self._persist()

    def set_env(self, env: dict[str, Any]) -> None:
        self.env.update(env)
        self._persist()

    def add_logs(self, lines: list[dict[str, Any]]) -> None:
        self.logs.extend(lines)
        self._persist()

    def _has_real_screenshot(self) -> bool:
        return any((not s.get("missing")) and s.get("file") for s in self.screenshots)

    def _screenshot_proof_ok(self) -> tuple[bool, str]:
        """A passing report needs a real screenshot captured FROM the editor under test --
        not a shot of another editor, and not a manual attach with unknown origin. `uap
        screenshot` stamps each shot's provenance (its editor's project); we require one
        whose provenance matches this report's project."""
        real = [s for s in self.screenshots if (not s.get("missing")) and s.get("file")]
        if not real:
            return False, "no screenshot attached"
        rp = (self.project or "").lower()
        if not rp:
            return True, ""  # report has no project to verify against; accept a real shot
        for s in real:
            prov = (s.get("provenance") or "").lower()
            if prov and (prov in rp or rp in prov):
                return True, ""
        provs = [s.get("provenance") for s in real]
        return False, (f"screenshot(s) are not verified from the editor under test "
                       f"(report project={self.project!r}, shot provenance={provs}). Capture "
                       f"with `uap screenshot <abs.png>` via THIS project's uap.ps1 -- a shot of "
                       f"another editor (or a manual attach) is not proof.")

    def finish(self, status: str, summary: str) -> None:
        # A "pass" without a screenshot FROM THE EDITOR UNDER TEST is a false positive (agents
        # have passed on a shot of another editor / an unverified image). Require verified proof
        # by default -- auto-downgrade to fail. (Opt out: `report start --no-require-screenshot`.)
        if status == "pass" and self.requires_screenshot:
            ok, reason = self._screenshot_proof_ok()
            if not ok:
                status = "fail"
                self.notes.append({
                    "text": f"AUTO-FAIL: {reason} (Opt out only with "
                            "`report start --no-require-screenshot`.)",
                    "section": None,
                })
                summary = (summary + f" [auto-failed: {reason}]").strip()
        self.status = status
        self.summary = summary
        self.finished = datetime.now()
        self.duration_s = round((self.finished - self.started).total_seconds(), 1)
        self._persist()

    # --- serialization ---
    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "project": self.project,
            "agent": self.agent,
            "superseded_by": self.superseded_by,
            "status": self.status,
            "started": self.started.isoformat(timespec="seconds"),
            "finished": self.finished.isoformat(timespec="seconds") if self.finished else None,
            "duration_s": self.duration_s,
            "quote": self.quote,
            "requires_screenshot": self.requires_screenshot,
            "summary": self.summary,
            "env": self.env,
            "perf": self.perf,
            "notes": self.notes,
            "assertions": self.assertions,
            "timeline": self.timeline,
            "screenshots": self.screenshots,
            "logs": self.logs,
        }

    def _persist(self) -> None:
        (self.run_dir / "data.json").write_text(
            json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, run_dir) -> ReportSession:
        run_dir = Path(run_dir)
        data = json.loads((run_dir / "data.json").read_text(encoding="utf-8"))
        # Build without re-running __init__ (which would reset lists + recreate dirs).
        self = cls.__new__(cls)
        self.task = data.get("task", "")
        self.project = data.get("project")
        self.agent = data.get("agent")
        self.superseded_by = data.get("superseded_by")
        self.quote = data.get("quote", "")
        self.requires_screenshot = data.get("requires_screenshot", True)
        self.status = data.get("status", "running")
        self.started = datetime.fromisoformat(data["started"]) if data.get("started") else datetime.now()
        self.finished = datetime.fromisoformat(data["finished"]) if data.get("finished") else None
        self.duration_s = data.get("duration_s")
        self.summary = data.get("summary", "")
        self.env = data.get("env", {})
        self.perf = data.get("perf")
        self.notes = data.get("notes", [])
        self.assertions = data.get("assertions", [])
        self.timeline = data.get("timeline", [])
        self.screenshots = data.get("screenshots", [])
        self.logs = data.get("logs", [])
        self.run_dir = run_dir
        (self.run_dir / "screenshots").mkdir(parents=True, exist_ok=True)
        return self


# --- Active session registry ---

_active: ReportSession | None = None


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s[:maxlen] or "run")


def _reports_root() -> Path:
    root = os.environ.get("UAP_REPORTS_DIR")
    return Path(root) if root else (Path.home() / ".uap-reports")


def _active_pointer() -> Path:
    return _reports_root() / ".active"


def set_active_run(run_dir, agent: str | None = None) -> None:
    """Claim the machine-wide report slot, recording WHO claimed it.

    The pointer used to be a bare path, so nothing in it said whose report it was -- which is
    why a second `report start` could take the slot from a running one and neither agent could
    tell. It is JSON now; `get_active_run` still reads the old bare-path form.
    """
    p = _active_pointer()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_dir": str(run_dir), "agent": agent or None,
                             "pid": os.getpid(),
                             "claimed": datetime.now().isoformat(timespec="seconds")}),
                 encoding="utf-8")


def get_active_owner() -> dict[str, Any] | None:
    """The slot's claim record: {run_dir, agent, pid, claimed}, or None if unclaimed."""
    p = _active_pointer()
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return {"run_dir": raw, "agent": None, "pid": None, "claimed": None}  # legacy bare path
    if not isinstance(rec, dict) or not rec.get("run_dir"):
        return None
    return rec


def get_active_run() -> Path | None:
    rec = get_active_owner()
    return Path(rec["run_dir"]) if rec else None


def clear_active_run() -> None:
    p = _active_pointer()
    if p.exists():
        p.unlink()


class ReportSlotConflict(Exception):
    """A running report is held by a DIFFERENT agent, so taking the slot would erase it.

    The slot is one per machine. Before this, `report start` overwrote the pointer with no
    check: the first agent's later `report note` / `assert` / `finish` then resolved the
    pointer to the SECOND agent's run and, once that one finished and cleared it, got a bare
    `no active report`. Nothing announced it and the first agent's evidence was gone. Two
    agents verifying at once collided quietly instead of loudly.
    """

    def __init__(self, holder: dict[str, Any], task: str, status: str):
        self.holder = holder
        self.task = task
        self.status = status
        super().__init__(
            f"the report slot is already held by agent {holder.get('agent')!r} "
            f"(task {task!r}, status {status}, run_dir {holder.get('run_dir')}, claimed "
            f"{holder.get('claimed')}). There is ONE slot per machine, so starting a report "
            f"now would take it from that run and its later notes/asserts would be lost. "
            f"Wait for it to finish, or -- only if you are sure it is abandoned -- pass "
            f"--takeover, which closes it as `incomplete` and renders what it has so far."
        )


def _supersede(run_dir: Path, *, by_agent: str | None, by_task: str,
               live: ReportSession | None = None) -> bool:
    """Close a displaced report instead of abandoning it, and keep its evidence readable.

    The in-process path already did this (`finish("incomplete", ...)`), but every CLI call is a
    fresh process, so the in-process `_active` global was always None and the displaced run was
    simply left `status: running` forever with its pointer overwritten.

    data.json is ALWAYS the source read and written, never an in-memory copy: notes and
    assertions may have been appended by other processes since that copy was made, and closing
    the copy would drop them. `live` is only an in-memory handle for this same run (the
    long-lived MCP server's); it is updated from the closed record afterwards so the object the
    caller still holds does not report `running`, and so it cannot persist itself back over the
    stamp.
    """
    try:
        if not (Path(run_dir) / "data.json").exists():
            return False
        old = ReportSession.load(run_dir)
        if old.status != "running":
            return False
        old.superseded_by = {"agent": by_agent, "task": by_task,
                             "at": datetime.now().isoformat(timespec="seconds")}
        old.add_note(f"SUPERSEDED: the machine-wide report slot was taken by a new "
                     f"`report start` (agent {by_agent!r}, task {by_task!r}). Everything below "
                     f"this line was never recorded against this run.")
        old.finish("incomplete", f"superseded by a new report start from agent {by_agent!r}")
        try:
            from unreal_agent_player.reporting.render import render
            (Path(run_dir) / "index.html").write_text(render(old.to_dict()), encoding="utf-8")
        except Exception:
            pass    # the data.json record is what matters; a render failure must not block
        if live is not None and live is not old:
            # Bring the caller's handle in line with what is now on disk, without persisting --
            # otherwise it reports `running` for a run that is closed, and any later _persist()
            # from it would erase the stamp.
            for field in ("status", "summary", "finished", "duration_s", "superseded_by",
                          "notes", "assertions", "timeline", "screenshots", "logs"):
                setattr(live, field, getattr(old, field))
        return True
    except Exception:
        return False


def find_superseded_run(agent: str | None) -> dict[str, Any] | None:
    """The most recent run that LOST the slot, so a stranded call can say what happened.

    `report note` against a slot somebody else took used to answer a bare `no active report`,
    which reads as "you forgot to start one". Prefer a run whose agent matches the caller's
    token; with no token, the most recent superseded run at all is the best available answer
    and is reported as a possibility rather than a fact.
    """
    root = _reports_root()
    if not root.exists():
        return None
    best = None
    for d in sorted(root.glob("*__*"), reverse=True)[:50]:
        try:
            data = json.loads((d / "data.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sup = data.get("superseded_by")
        if not sup:
            continue
        rec = {"run_dir": str(d), "task": data.get("task"), "agent": data.get("agent"),
               "superseded_by": sup}
        if agent and data.get("agent") == agent:
            return rec
        best = best or rec
    return None if agent else best


def start_session(*, task: str, project: str | None = None,
                  requires_screenshot: bool = True, agent: str | None = None,
                  takeover: bool = False) -> ReportSession:
    """Claim the machine-wide report slot for a new run.

    Refuses when a RUNNING report is held by a different agent token, because taking the slot
    silently is how one agent's evidence disappeared mid-session. With no token on either side
    there is no identity to compare -- this harness has none -- so the take proceeds, but the
    displaced run is closed as `incomplete` and rendered rather than left dangling.
    """
    global _active
    from unreal_agent_player.reporting.quotes import pick_quote
    holder = get_active_owner()
    holder_dir = str(holder["run_dir"]) if holder else None
    # An in-process handle for a DIFFERENT run than the slot names is closed here; one for the
    # SAME run is handed to _supersede, so the object the caller still holds is the object that
    # gets closed. Two writers on one data.json is how the supersede stamp got erased. Only the
    # long-lived MCP server ever has such a handle; every CLI call is a fresh process.
    live_holder: ReportSession | None = None
    if _active is not None and _active.status == "running":
        if str(_active.run_dir) == holder_dir:
            live_holder = _active
        else:
            _active.finish("incomplete", "superseded by a new report_start")
    superseded: dict[str, Any] | None = None
    if holder:
        held_dir = Path(holder["run_dir"])
        held_status, held_task = "missing", ""
        try:
            data = json.loads((held_dir / "data.json").read_text(encoding="utf-8"))
            held_status, held_task = data.get("status", "running"), data.get("task", "")
        except (OSError, json.JSONDecodeError):
            pass
        if held_status == "running":
            hold_agent = (holder.get("agent") or "").strip()
            mine = (agent or "").strip()
            if hold_agent and mine and hold_agent != mine and not takeover:
                raise ReportSlotConflict(holder, held_task, held_status)
            if _supersede(held_dir, by_agent=agent, by_task=task, live=live_holder):
                superseded = {"run_dir": str(held_dir), "task": held_task,
                              "agent": holder.get("agent")}
    started = datetime.now()
    run_dir = _reports_root() / f"{started:%Y%m%d-%H%M%S}__{_slug(task)}"
    _active = ReportSession(task=task, project=project, run_dir=run_dir, agent=agent,
                            quote=pick_quote(), requires_screenshot=requires_screenshot)
    _active.displaced = superseded          # read by `report start` for its warning
    set_active_run(_active.run_dir, agent)
    return _active


def active() -> ReportSession | None:
    return _active


def clear_active() -> None:
    global _active
    _active = None


# --- Auto-capture routing ---

def _arg_summary(args: dict, limit: int = 200) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + "...(truncated)"
        else:
            out[k] = v
    return out


#: Both spellings of the log verbs. The MCP server names its tools `log_since` / `log_tail`;
#: the CLI records the same reads as `log:since` / `log:tail`, and only the MCP spellings were
#: matched here -- so every log line an agent read through `uap log` was dropped on the floor
#: and the report's "Log warnings/errors" panel stayed empty, while AGENTS.md told agents to
#: use `uap log` precisely SO the evidence would land in the report.
_LOG_TOOLS = ("log_tail", "log_since", "log:tail", "log:since")


def record_call(session: ReportSession, tool: str, args: dict,
                body: dict, ms: int) -> None:
    """Append a timeline entry and harvest known tool outputs. Never raises."""
    try:
        ok = bool(body.get("ok", True)) and "error" not in body
        err = None
        if isinstance(body.get("error"), dict):
            err = body["error"].get("message")
            ok = False
        session.add_tool_call(tool, _arg_summary(args), ok=ok, ms=ms, error=err)

        if tool == "screenshot_viewport" and body.get("path"):
            session.add_screenshot(body["path"])
        elif tool == "perf_stat" and isinstance(body.get("parsed"), dict):
            session.set_perf(body["parsed"])
        elif tool == "bridge_status":
            session.set_env({
                "plugin_version": body.get("plugin_version"),
                "bridge": {
                    "ue_running": body.get("ue_running"),
                    "rc_reachable": body.get("rc_reachable"),
                    "remote_exec_reachable": body.get("remote_exec_reachable"),
                },
            })
        elif tool in _LOG_TOOLS and isinstance(body.get("lines"), list):
            kept = [ln for ln in body["lines"]
                    if str(ln.get("verbosity")) in ("Warning", "Error", "Fatal")]
            if kept:
                session.add_logs(kept)
    except Exception:
        # Capture must never break the underlying tool result.
        pass
