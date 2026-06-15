from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from unreal_agent_player.reporting import session as sess
from unreal_agent_player.reporting.render import render


def _load_active() -> "sess.ReportSession | None":
    run = sess.get_active_run()
    if run is None or not (run / "data.json").exists():
        return None
    return sess.ReportSession.load(run)


def _emit(obj: dict) -> None:
    print(json.dumps(obj))


# --- report verbs ---

def _report_start(args) -> int:
    s = sess.start_session(task=args.task, project=args.project)
    _emit({"ok": True, "run_dir": str(s.run_dir)})
    return 0


def _report_assert(args) -> int:
    s = _load_active()
    if s is None:
        _emit({"ok": False, "error": "no active report; run `uap report start` first"})
        return 2
    s.add_assertion(args.label, args.verdict == "pass", args.evidence)
    _emit({"ok": True})
    return 0


def _report_note(args) -> int:
    s = _load_active()
    if s is None:
        _emit({"ok": False, "error": "no active report; run `uap report start` first"})
        return 2
    s.add_note(args.text)
    _emit({"ok": True})
    return 0


def _report_finish(args) -> int:
    s = _load_active()
    if s is None:
        _emit({"ok": False, "error": "no active report; run `uap report start` first"})
        return 2
    s.finish(args.verdict, args.summary)
    html_path = s.run_dir / "index.html"
    html_path.write_text(render(s.to_dict()), encoding="utf-8")
    sess.clear_active_run()
    try:
        webbrowser.open(html_path.as_uri())
    except Exception:
        pass
    _emit({"ok": True, "html": str(html_path)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uap")
    sub = p.add_subparsers(dest="cmd", required=True)

    rep = sub.add_parser("report").add_subparsers(dest="rcmd", required=True)
    rs = rep.add_parser("start"); rs.add_argument("task"); rs.add_argument("--project", default="SchoolsOutVR")
    rs.set_defaults(func=_report_start)
    ra = rep.add_parser("assert"); ra.add_argument("label"); ra.add_argument("verdict", choices=["pass", "fail"]); ra.add_argument("evidence", nargs="?", default="")
    ra.set_defaults(func=_report_assert)
    rn = rep.add_parser("note"); rn.add_argument("text"); rn.set_defaults(func=_report_note)
    rf = rep.add_parser("finish"); rf.add_argument("verdict", choices=["pass", "fail"]); rf.add_argument("summary")
    rf.set_defaults(func=_report_finish)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
