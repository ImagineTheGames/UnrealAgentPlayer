"""Task labels: who is driving the editor, and what for (--task / $UAP_TASK).

The in-editor overlay (AgentOverlay.cpp) and `uap lease status` read the lease state file, so
every assertion here is on what lands in that file / that JSON. No editor is involved.
"""
import json

import pytest

import unreal_agent_player.coordination as co
from unreal_agent_player import cli as cli_mod
from unreal_agent_player.cli import main


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("UAP_RC_PORT", "1")          # never reach a real editor
    monkeypatch.setenv("UAP_PROJECT", "")
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    monkeypatch.delenv("UAP_TASK", raising=False)
    monkeypatch.setattr(co, "_pid_alive", lambda pid: True)


def _status(capsys, *extra):
    capsys.readouterr()
    assert main(["lease", "status", "--project", "demo", *extra]) == 0
    return json.loads(capsys.readouterr().out)


# ---- sanitising -------------------------------------------------------------------------------

def test_sanitize_folds_control_chars_and_whitespace():
    assert co.sanitize_label("  PCG\tprofiling\r\npass \x07 now ") == "PCG profiling pass now"


def test_sanitize_truncates_to_80_with_ellipsis():
    out = co.sanitize_label("x" * 500)
    assert len(out) == co.TASK_MAX == 80
    assert out.endswith("...")


def test_sanitize_leaves_a_short_label_alone_and_tolerates_none():
    assert co.sanitize_label("fix hangar lights") == "fix hangar lights"
    assert co.sanitize_label(None) == ""
    assert co.sanitize_label(1234) == "1234"


def test_default_task_reads_and_sanitises_the_env(monkeypatch):
    monkeypatch.setenv("UAP_TASK", "line one\nline two")
    assert co.default_task() == "line one line two"


# ---- the lease record -------------------------------------------------------------------------

def test_holder_and_waiter_carry_their_task(tmp_path):
    assert co.acquire("demo", "exclusive", reason="pie", agent="pcgprof", wait=0,
                      task="PCG profiling")["granted"]
    busy = co.acquire("demo", "exclusive", reason="pie", agent="hangar", wait=0, task="Hangar lights")
    assert busy["busy"]
    # wait=0 gives the slot back, so re-queue a live waiter the way a blocked acquire leaves it.
    state = co._load("demo")
    co._enqueue(state, "hangar", "exclusive", "pie", "Hangar lights")
    co._save("demo", state)
    st = co.status("demo")
    assert st["exclusive"]["task"] == "PCG profiling"
    assert [w["task"] for w in st["waiters"]] == ["Hangar lights"]
    assert st["queue_detail"] == [{"agent": "hangar", "task": "Hangar lights", "reason": "pie",
                                   "requested_by": ""}]


def test_status_adds_empty_task_to_records_written_before_labels_existed():
    co.acquire("demo", "exclusive", reason="pie", agent="old", wait=0)
    state = co._load("demo")
    del state["exclusive"]["task"]
    del state["exclusive"]["requested_by"]
    co._save("demo", state)
    st = co.status("demo")
    assert st["exclusive"]["task"] == "" and st["exclusive"]["requested_by"] == ""


def test_reacquire_without_a_label_keeps_the_existing_one():
    """Restart-Editor's rebuild -> relaunch flip re-acquires as the same token."""
    co.acquire("demo", "exclusive", reason="rebuild", agent="rebuild-7", wait=0,
               task="PCG profiling", requested_by="pcgprof")
    co.acquire("demo", "exclusive", reason="relaunch", agent="rebuild-7", wait=0)
    ex = co.status("demo")["exclusive"]
    assert ex["reason"] == "relaunch"
    assert ex["task"] == "PCG profiling" and ex["requested_by"] == "pcgprof"


def test_heartbeat_with_a_task_relabels_the_hold():
    co.acquire("demo", "exclusive", reason="pie", agent="A", wait=0, task="old work")
    co.heartbeat("demo", agent="A", task="new work")
    assert co.status("demo")["exclusive"]["task"] == "new work"
    co.heartbeat("demo", agent="A")                       # no label: leaves it
    assert co.status("demo")["exclusive"]["task"] == "new work"


def test_labels_do_not_change_who_is_granted_what():
    co.acquire("demo", "exclusive", reason="pie", agent="A", wait=0, task="same")
    assert co.acquire("demo", "exclusive", agent="B", wait=0, task="same").get("busy")


def test_note_activity_records_agent_verb_and_task_and_expires(monkeypatch):
    co.note_activity("demo", agent="pcgprof", verb="exec", task="PCG profiling")
    act = co.status("demo")["activity"]
    assert act["pcgprof"]["verb"] == "exec" and act["pcgprof"]["task"] == "PCG profiling"
    # A later call without a label keeps the one already known for that agent.
    co.note_activity("demo", agent="pcgprof", verb="rc")
    assert co.status("demo")["activity"]["pcgprof"]["task"] == "PCG profiling"
    # ...and the entry ages out of the record.
    state = co._load("demo")
    state["activity"]["pcgprof"]["at"] -= co._ACTIVITY_TTL + 1
    co._save("demo", state)
    co.note_activity("demo", agent="other", verb="exec")
    assert "pcgprof" not in co.status("demo")["activity"]


def test_machine_lock_holder_and_queue_carry_task():
    r = co.acquire_machine("projA", reason="pie:start", agent="A", wait=0, task="Flight test")
    assert r["granted"] and r["holder"]["task"] == "Flight test"
    busy = co.acquire_machine("projB", reason="pie:start", agent="B", wait=0, task="Menu pass")
    assert busy["busy"] and busy["task"] == "Flight test"
    state = co._load(co.MACHINE_SENTINEL)
    co._enqueue(state, "B", "exclusive", "pie:start", "Menu pass")
    co._save(co.MACHINE_SENTINEL, state)
    ms = co.machine_status()
    assert ms["holder"]["task"] == "Flight test"
    assert ms["queue_detail"][0]["agent"] == "B" and ms["queue_detail"][0]["task"] == "Menu pass"


# ---- the CLI ----------------------------------------------------------------------------------

def test_cli_task_flag_lands_in_status_for_holder_and_machine(capsys):
    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "pcgprof",
                 "--task", "PCG profiling pass", "--project", "demo", "--wait", "0"]) == 0
    st = _status(capsys)
    assert st["exclusive"]["agent"] == "pcgprof"
    assert st["exclusive"]["task"] == "PCG profiling pass"
    assert st["machine"]["holder"]["task"] == "PCG profiling pass"      # pie is a foreground hold


def test_cli_task_comes_from_env_and_the_flag_wins(monkeypatch, capsys):
    monkeypatch.setenv("UAP_TASK", "from env")
    assert main(["lease", "acquire", "exclusive", "--agent", "A", "--project", "demo",
                 "--wait", "0"]) == 0
    assert _status(capsys)["exclusive"]["task"] == "from env"
    assert main(["lease", "acquire", "exclusive", "--agent", "A", "--task", "from flag",
                 "--project", "demo", "--wait", "0"]) == 0
    assert _status(capsys)["exclusive"]["task"] == "from flag"


def test_cli_task_is_sanitised_and_truncated(capsys):
    assert main(["lease", "acquire", "exclusive", "--agent", "A", "--project", "demo", "--wait", "0",
                 "--task", "bad\nlabel " + "y" * 200]) == 0
    t = _status(capsys)["exclusive"]["task"]
    assert "\n" not in t and len(t) <= 80 and t.startswith("bad label y")


def test_report_start_positional_task_is_not_confused_with_the_flag(capsys):
    """`report start <task>` has a positional called task; the new flag must not collide."""
    args = cli_mod.build_parser().parse_args(
        ["report", "start", "verify chairs", "--task", "label", "--agent", "A"])
    assert args.task == "verify chairs" and args.task_label == "label"


def test_rebuild_lease_names_its_requester_from_the_env(monkeypatch, capsys):
    """Restart-Editor.ps1 runs `lease acquire ... --agent rebuild-<pid>`; the invoking agent's own
    token / task reach it through the environment, with no change to the script."""
    monkeypatch.setenv("UAP_AGENT_ID", "pcgprof")
    monkeypatch.setenv("UAP_TASK", "PCG profiling")
    assert main(["lease", "acquire", "exclusive", "--reason", "rebuild", "--agent", "rebuild-4242",
                 "--pid", "4242", "--project", "demo", "--wait", "0"]) == 0
    ex = _status(capsys, "--agent", "rebuild-4242")["exclusive"]
    assert ex["agent"] == "rebuild-4242"
    assert ex["requested_by"] == "pcgprof" and ex["task"] == "PCG profiling"
    # The relaunch re-acquire in the same script keeps both even if the env were not inherited.
    monkeypatch.delenv("UAP_AGENT_ID")
    monkeypatch.delenv("UAP_TASK")
    assert main(["lease", "acquire", "exclusive", "--reason", "relaunch", "--agent", "rebuild-4242",
                 "--pid", "4242", "--project", "demo", "--wait", "0"]) == 0
    ex = _status(capsys, "--agent", "rebuild-4242")["exclusive"]
    assert ex["reason"] == "relaunch" and ex["requested_by"] == "pcgprof"


def test_requested_by_flag_wins_and_same_agent_is_not_its_own_requester(monkeypatch, capsys):
    monkeypatch.setenv("UAP_AGENT_ID", "pcgprof")
    assert main(["lease", "acquire", "exclusive", "--agent", "pcgprof", "--project", "demo",
                 "--wait", "0"]) == 0
    assert _status(capsys)["exclusive"]["requested_by"] == ""
    # `--reason rebuild`, as Restart-Editor.ps1 takes it: an exclusive lease for any other reason
    # now also takes the machine-wide lock, which project `demo` already holds.
    assert main(["lease", "acquire", "exclusive", "--reason", "rebuild", "--agent", "rebuild-9",
                 "--requested-by", "bob", "--project", "demo2", "--wait", "0"]) == 0
    capsys.readouterr()
    assert main(["lease", "status", "--project", "demo2"]) == 0
    assert json.loads(capsys.readouterr().out)["exclusive"]["requested_by"] == "bob"


def test_editor_verb_records_activity_with_its_task(monkeypatch, capsys):
    class _FakeExec:
        def __init__(self, **kw):
            pass

        def exec_python(self, code):
            return {"success": True, "result": "ok", "output": []}

    monkeypatch.setattr(cli_mod, "PythonRemoteExecClient", _FakeExec)
    main(["exec", "print(1)", "--project", "demo", "--agent", "pcgprof", "--task", "PCG profiling"])
    capsys.readouterr()
    act = co.status("demo")["activity"]
    assert act["pcgprof"]["task"] == "PCG profiling" and act["pcgprof"]["verb"] == "exec"


def test_busy_refusal_names_the_blockers_task(monkeypatch, capsys):
    assert main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "HOLDER",
                 "--task", "Flight test", "--project", "demo", "--wait", "0"]) == 0
    capsys.readouterr()
    rc = main(["exec", "print(1)", "--project", "demo", "--agent", "other"])
    out = json.loads(capsys.readouterr().out)
    assert rc != 0 and out["blocked_by"] == "HOLDER" and out["blocked_task"] == "Flight test"


def test_batch_inherits_the_task(monkeypatch, tmp_path):
    seen = []
    real = cli_mod._batch_run_step

    def spy(a):
        seen.append(getattr(a, "task_label", None))
        return 0
    monkeypatch.setattr(cli_mod, "_batch_run_step", spy)
    steps = tmp_path / "steps.txt"
    steps.write_text("status\n", encoding="utf-8")
    main(["batch", "--file", str(steps), "--task", "batch work", "--project", "demo"])
    monkeypatch.setattr(cli_mod, "_batch_run_step", real)
    assert seen == ["batch work"]
