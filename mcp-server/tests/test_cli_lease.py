from unreal_agent_player.cli import main
import unreal_agent_player.coordination as co


def test_cli_lease_persists_across_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    base = ["--project", "demo", "--wait", "0"]

    # A standalone `lease acquire` must default to a TTL-only hold (pid 0) so it survives the
    # command's process exit -- otherwise PID-death would evict it immediately (regression guard).
    assert main(["lease", "acquire", "exclusive", "--reason", "rebuild", "--agent", "A"] + base) == 0
    state = co._load("demo")
    assert state["exclusive"]["agent"] == "A"
    assert state["exclusive"]["pid"] == 0

    # A different agent is blocked (non-zero exit so a script's `if` can branch on it).
    assert main(["lease", "acquire", "exclusive", "--agent", "B"] + base) == 1
    # Release frees it.
    assert main(["lease", "release", "--agent", "A", "--project", "demo"]) == 0
    assert main(["lease", "acquire", "exclusive", "--agent", "B"] + base) == 0


def test_cli_lease_explicit_pid_is_reclaimed_on_death(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(co, "_pid_alive", lambda pid: False)  # the anchored process is "dead"
    # Anchored to a (now dead) pid -> next acquire reclaims it despite a long TTL.
    assert main(["lease", "acquire", "exclusive", "--agent", "R", "--pid", "999999",
                 "--ttl", "9999", "--project", "demo", "--wait", "0"]) == 0
    assert main(["lease", "acquire", "exclusive", "--agent", "S",
                 "--project", "demo", "--wait", "0"]) == 0  # reclaimed
