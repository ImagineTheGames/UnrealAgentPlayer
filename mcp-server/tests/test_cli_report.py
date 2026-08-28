from unreal_agent_player.cli import main


def test_report_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    # `report finish` now best-effort stops PIE (an RC call). Pin an unreachable RC port so that
    # call fails fast and deterministically instead of touching a live editor on this machine.
    monkeypatch.setenv("UAP_RC_PORT", "1")
    assert main(["report", "start", "does the door open"]) == 0
    assert main(["report", "assert", "door opens", "pass", "bIsOpen=true"]) == 0
    assert main(["report", "finish", "pass", "verified"]) == 0
    # Only count run dirs -- infra like the .rcports port cache also lives under UAP_REPORTS_DIR.
    runs = [p for p in tmp_path.iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert len(runs) == 1
    html = (runs[0] / "index.html").read_text(encoding="utf-8")
    assert "door opens" in html
    assert not (tmp_path / ".active").exists()  # cleared on finish


def test_report_finish_autostops_pie(tmp_path, monkeypatch, capsys):
    """finish must CONFIRM the teardown, not merely fire a stop. `pie_stopped` is a claim the
    next agent acts on, so it may only be true once PIE is observed gone."""
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    import json

    from unreal_agent_player import cli
    calls = []
    polls = {"left": 1}      # still in progress for one poll, then gone

    def fake_rc(func, params, project=None):
        calls.append(func)
        if func == "StopPIEEx":
            return json.dumps({"ok": True, "was_playing": True,
                               "cancelled_queued_start": False, "in_progress": True})
        if func == "IsPIEInProgress":
            polls["left"] -= 1
            return polls["left"] >= 0
        return None

    monkeypatch.setattr(cli, "_rc_call", fake_rc)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert main(["report", "start", "t"]) == 0
    assert main(["report", "finish", "pass", "done"]) == 0
    assert "StopPIEEx" in calls and calls.count("IsPIEInProgress") == 2
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["pie_stopped"] is True and "pie_stop_error" not in body


def test_report_finish_reports_a_stop_that_did_not_take(tmp_path, monkeypatch, capsys):
    """A stop that never completed must not be reported as a clean editor -- that is exactly how
    the next agent ends up driving a live PIE session."""
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_PIE_STOP_TIMEOUT", "2")
    import json

    from unreal_agent_player import cli

    def fake_rc(func, params, project=None):
        if func == "StopPIEEx":
            return json.dumps({"ok": True, "was_playing": True,
                               "cancelled_queued_start": False, "in_progress": True})
        return True if func == "IsPIEInProgress" else None    # never goes away

    monkeypatch.setattr(cli, "_rc_call", fake_rc)
    # Drive the stop loop's wall clock instead of sleeping for real.
    clock = {"now": 0.0}
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cli.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))
    assert main(["report", "start", "t"]) == 0
    assert main(["report", "finish", "pass", "done"]) == 0     # the report still renders
    body = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert body["pie_stopped"] is False
    assert "NOT free" in body["pie_stop_error"]


def test_report_finish_keep_pie_skips_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    from unreal_agent_player import cli
    calls = []
    monkeypatch.setattr(cli, "_rc_call", lambda f, p, project=None: calls.append(f))
    assert main(["report", "start", "t"]) == 0
    assert main(["report", "finish", "pass", "done", "--keep-pie"]) == 0
    assert calls == []  # --keep-pie makes finish touch the editor not at all


def test_assert_without_start_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    assert main(["report", "assert", "x", "pass", "y"]) == 2
