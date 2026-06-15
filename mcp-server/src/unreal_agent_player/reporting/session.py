from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class ReportSession:
    def __init__(self, *, task: str, run_dir: Path, quote: str,
                 project: Optional[str] = None):
        self.task = task
        self.project = project
        self.quote = quote
        self.status = "running"
        self.started = datetime.now()
        self.finished: Optional[datetime] = None
        self.duration_s: Optional[float] = None
        self.summary = ""
        self.env: dict[str, Any] = {}
        self.perf: Optional[dict[str, Any]] = None
        self.notes: list[dict[str, Any]] = []
        self.assertions: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.screenshots: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
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

    def add_note(self, text: str, section: Optional[str] = None) -> None:
        self.notes.append({"text": text, "section": section})
        self._persist()

    def add_screenshot(self, src_path: str, caption: str = "") -> Optional[str]:
        idx = len(self.screenshots)
        src = Path(src_path)
        if not src.exists():
            self.screenshots.append({
                "file": None, "caption": caption,
                "t": self._hms(datetime.now()), "missing": True,
            })
            self._persist()
            return None
        rel = f"screenshots/{idx:03d}.png"
        shutil.copyfile(src, self.run_dir / rel)
        self.screenshots.append({
            "file": rel, "caption": caption,
            "t": self._hms(datetime.now()), "missing": False,
        })
        self._persist()
        return rel

    def set_caption(self, ref: Optional[Any], caption: str) -> bool:
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
                      ms: int, error: Optional[str] = None) -> None:
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

    def finish(self, status: str, summary: str) -> None:
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
            "status": self.status,
            "started": self.started.isoformat(timespec="seconds"),
            "finished": self.finished.isoformat(timespec="seconds") if self.finished else None,
            "duration_s": self.duration_s,
            "quote": self.quote,
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
    def load(cls, run_dir) -> "ReportSession":
        run_dir = Path(run_dir)
        data = json.loads((run_dir / "data.json").read_text(encoding="utf-8"))
        # Build without re-running __init__ (which would reset lists + recreate dirs).
        self = cls.__new__(cls)
        self.task = data.get("task", "")
        self.project = data.get("project")
        self.quote = data.get("quote", "")
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

_active: Optional[ReportSession] = None


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (s[:maxlen] or "run")


def _reports_root() -> Path:
    root = os.environ.get("UAP_REPORTS_DIR")
    return Path(root) if root else (Path.home() / ".uap-reports")


def _active_pointer() -> Path:
    return _reports_root() / ".active"


def set_active_run(run_dir) -> None:
    p = _active_pointer()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(run_dir), encoding="utf-8")


def get_active_run() -> Optional[Path]:
    p = _active_pointer()
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8").strip()
    return Path(raw) if raw else None


def clear_active_run() -> None:
    p = _active_pointer()
    if p.exists():
        p.unlink()


def start_session(*, task: str, project: Optional[str] = None) -> ReportSession:
    global _active
    from unreal_agent_player.reporting.quotes import pick_quote
    if _active is not None and _active.status == "running":
        _active.finish("incomplete", "superseded by a new report_start")
    started = datetime.now()
    run_dir = _reports_root() / f"{started:%Y%m%d-%H%M%S}__{_slug(task)}"
    _active = ReportSession(task=task, project=project, run_dir=run_dir, quote=pick_quote())
    set_active_run(_active.run_dir)
    return _active


def active() -> Optional[ReportSession]:
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


def record_call(session: "ReportSession", tool: str, args: dict,
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
        elif tool in ("log_tail", "log_since") and isinstance(body.get("lines"), list):
            kept = [ln for ln in body["lines"]
                    if str(ln.get("verbosity")) in ("Warning", "Error", "Fatal")]
            if kept:
                session.add_logs(kept)
    except Exception:
        # Capture must never break the underlying tool result.
        pass
