"""Token + speed fixes: harness-safe lease waits, brief output, help split, guard fold, timing log.

Every lease test here runs against the isolated $UAP_REPORTS_DIR the conftest sets up, on a fake
project, so nothing touches a real editor's lease state.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from tests.test_log_sweep_truthfulness import _FakeRing, _wire
from unreal_agent_player import cli
from unreal_agent_player import coordination as co

PROJ = "tokenspeedfake"


@pytest.fixture
def fast_queue(monkeypatch):
    """Short poll + waiter TTL, so parking is what keeps a waiter alive, not a lingering TTL."""
    monkeypatch.setattr(co, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(co, "_WAITER_TTL", 0.2)
    monkeypatch.setenv("UAP_WAITER_PARK_TTL", "30")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")


def _out(capsys) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def _queue() -> list[str]:
    return [w["agent"] for w in co.status(PROJ)["waiters"]]


# --------------------------------------------------------------------------- max-block / resume
def test_max_block_returns_still_waiting_and_keeps_the_place(fast_queue):
    assert co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)["granted"]
    t0 = time.monotonic()
    res = co.acquire(PROJ, "exclusive", reason="pie", agent="B", pid=0, wait=60, max_block=0.3)
    took = time.monotonic() - t0
    assert took < 2.0
    assert res["ok"] is False and res["held"] is False and res["still_waiting"] is True
    assert res["position"] == 1
    assert res["holder"]["agent"] == "A"
    assert res["resume"] == "uap lease wait --agent B"
    # Well past the waiter TTL: the PARK is what keeps it queued between calls.
    time.sleep(0.6)
    assert _queue() == ["B"]


def test_queue_order_is_kept_across_timeouts_and_resumes(fast_queue):
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    for a in ("B", "C", "D"):
        r = co.acquire(PROJ, "exclusive", reason="pie", agent=a, pid=0, wait=60, max_block=0.1)
        assert r["still_waiting"]
    assert _queue() == ["B", "C", "D"]
    time.sleep(0.5)                       # all three are "between calls" now
    co.release(PROJ, agent="A")
    # C comes back first, but B is still ahead of it: C keeps waiting, place unchanged.
    rc = co.wait_turn(PROJ, agent="C", seconds=0.2)
    assert rc["still_waiting"] and rc["position"] == 2
    rb = co.wait_turn(PROJ, agent="B", seconds=0.2)
    assert rb["granted"] and rb["held"] and rb["mode"] == "exclusive"
    assert _queue() == ["C", "D"]
    co.release(PROJ, agent="B")
    rd = co.wait_turn(PROJ, agent="D", seconds=0.2)
    assert rd["still_waiting"] and rd["position"] == 2
    assert co.wait_turn(PROJ, agent="C", seconds=0.2)["granted"]
    co.release(PROJ, agent="C")
    assert co.wait_turn(PROJ, agent="D", seconds=0.2)["granted"]


def test_resume_reuses_the_parked_request(fast_queue):
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    co.acquire(PROJ, "exclusive", reason="level", agent="B", pid=0, wait=60, ttl=321,
               task="my task", max_block=0.1)
    co.release(PROJ, agent="A")
    r = co.wait_turn(PROJ, agent="B", seconds=0.5)
    assert r["granted"]
    ex = co.status(PROJ)["exclusive"]
    assert (ex["agent"], ex["reason"], ex["ttl"], ex["task"], ex["pid"]) == \
        ("B", "level", 321, "my task", 0)


def test_wait_with_nothing_to_resume_says_not_queued(fast_queue):
    r = co.wait_turn(PROJ, agent="nobody", seconds=0.1)
    assert r["not_queued"] is True and r["ok"] is False and r["held"] is False
    assert _queue() == []


def test_wait_when_already_holding_answers_held(fast_queue):
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    r = co.wait_turn(PROJ, agent="A", seconds=0.1)
    assert r["held"] and r["already_held"]


def test_an_expired_park_loses_the_place_and_says_so(fast_queue, monkeypatch):
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    monkeypatch.setattr(co, "_park_ttl", lambda: 0.2)
    co.acquire(PROJ, "exclusive", reason="pie", agent="B", pid=0, wait=60, max_block=0.05)
    co.acquire(PROJ, "exclusive", reason="pie", agent="C", pid=0, wait=60, max_block=0.05)
    time.sleep(0.6)                        # both parks expire
    assert _queue() == []                  # a dead agent drops out on its own
    r = co.wait_turn(PROJ, agent="B", seconds=0.1)
    assert r["still_waiting"] and r["requeued"] is True
    assert _queue() == ["B"]


def test_explicit_lease_wait_blocks_even_after_the_original_budget_is_gone(fast_queue):
    # Was `test_total_wait_budget_spans_resumes`: it asserted that a spent --wait budget made
    # `lease wait --seconds 5` return busy at once and drop the slot. That instant answer is the
    # spin bug (agents re-queued at the back each time). An explicit `lease wait --seconds S`
    # now blocks S seconds and keeps the place; the original --wait only bounds the acquire call.
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    r = co.acquire(PROJ, "exclusive", reason="pie", agent="B", pid=0, wait=0.4, max_block=0.1)
    assert r["still_waiting"]
    time.sleep(0.5)                        # the 0.4s original budget is gone
    t0 = time.time()
    r = co.wait_turn(PROJ, agent="B", seconds=0.5)
    assert time.time() - t0 >= 0.45
    assert r["busy"] and r["still_waiting"] and not r["held"]
    assert _queue() == ["B"]               # place kept


def test_a_fresh_acquire_while_parked_keeps_the_place(fast_queue):
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    co.acquire(PROJ, "exclusive", reason="pie", agent="B", pid=0, wait=60, max_block=0.05)
    co.acquire(PROJ, "exclusive", reason="pie", agent="C", pid=0, wait=60, max_block=0.05)
    time.sleep(0.4)
    # An agent that does not know about `lease wait` re-runs its acquire: still 2nd, not last.
    r = co.acquire(PROJ, "exclusive", reason="pie", agent="C", pid=0, wait=60, max_block=0.05)
    assert r["position"] == 2


def test_without_max_block_the_old_behaviour_is_unchanged(fast_queue):
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    r = co.acquire(PROJ, "exclusive", reason="pie", agent="B", pid=0, wait=0.2)
    assert r["busy"] and "still_waiting" not in r and "held" not in r
    assert _queue() == []                   # a capped plain wait leaves the queue, as before


_WORKER = r"""
import json, sys, time
from unreal_agent_player import coordination as co
co.POLL_SECONDS = 0.05
agent, out = sys.argv[1], sys.argv[2]
calls = 0
while True:
    calls += 1
    r = co.wait_turn("tokenspeedfake", agent=agent, seconds=0.3)
    if r.get("granted"):
        break
    assert r.get("still_waiting"), r
    time.sleep(0.15)                     # "thinking" between calls: the harness gap
with open(out, "a") as f:
    f.write(json.dumps({"agent": agent, "at": time.time(), "calls": calls}) + "\n")
time.sleep(0.3)
co.release("tokenspeedfake", agent=agent)
"""


def test_three_real_processes_resume_and_are_granted_in_arrival_order(fast_queue, tmp_path):
    co.acquire(PROJ, "exclusive", reason="pie", agent="A", pid=0, wait=0)
    for a in ("B", "C", "D"):
        co.acquire(PROJ, "exclusive", reason="pie", agent=a, pid=0, wait=120, max_block=0.05)
    out = tmp_path / "order.jsonl"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(pathlib.Path(cli.__file__).resolve().parents[1])
    # Started in REVERSE arrival order, so arrival -- not who polls first -- has to decide.
    procs = [subprocess.Popen([sys.executable, "-c", _WORKER, a, str(out)], env=env)
             for a in ("D", "C", "B")]
    time.sleep(0.8)
    co.release(PROJ, agent="A")
    for p in procs:
        assert p.wait(timeout=60) == 0
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert [r["agent"] for r in rows] == ["B", "C", "D"]
    assert all(r["calls"] >= 2 for r in rows)     # each timed out at least once and resumed


# --------------------------------------------------------------------------- CLI lease verbs
def test_cli_acquire_max_block_and_wait_brief(fast_queue, capsys):
    base = ["--project", PROJ]
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "A",
                     "--wait", "0"] + base) == 0
    capsys.readouterr()
    assert cli.main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "B",
                     "--wait", "60", "--max-block", "0.2", "--brief"] + base) == 1
    raw = capsys.readouterr().out.strip()
    body = json.loads(raw)
    assert body == {"ok": False, "held": False, "still_waiting": True, "position": 1,
                    "holder": "A", "resume": "uap lease wait --agent B", "mode": "exclusive",
                    "busy": True}
    assert len(raw) < 300 and "\n" not in raw
    assert cli.main(["lease", "release", "--agent", "A", "--force", "--brief"] + base) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "released": True}
    assert cli.main(["lease", "wait", "--agent", "B", "--seconds", "1", "--brief"] + base) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["held"] is True and body["ok"] is True


def test_env_max_block_applies_to_acquire(fast_queue, capsys, monkeypatch):
    monkeypatch.setenv("UAP_MAX_BLOCK", "0.2")
    base = ["--project", PROJ]
    cli.main(["lease", "acquire", "exclusive", "--agent", "A", "--wait", "0"] + base)
    capsys.readouterr()
    t0 = time.monotonic()
    assert cli.main(["lease", "acquire", "exclusive", "--agent", "B"] + base) == 1
    assert time.monotonic() - t0 < 3
    body = json.loads(capsys.readouterr().out)
    # Default (full) shape: every old key is still there, the new ones are added.
    for k in ("ok", "granted", "busy", "agent", "mode", "holder", "queue_position",
              "queue_length", "queue_ahead", "hint", "still_waiting", "resume"):
        assert k in body


def test_default_lease_status_shape_is_unchanged_and_brief_is_small(fast_queue, capsys):
    base = ["--project", PROJ]
    cli.main(["lease", "acquire", "exclusive", "--reason", "pie", "--agent", "A",
              "--task", "doing a thing", "--wait", "0"] + base)
    co.acquire(PROJ, "exclusive", reason="pie", agent="B", pid=0, wait=60, max_block=0.05)
    capsys.readouterr()
    cli.main(["lease", "status"] + base)
    full = json.loads(capsys.readouterr().out)
    for k in ("ok", "project", "queue", "queue_detail", "generation", "exclusive", "shared",
              "waiters", "evicted", "last_eviction", "activity", "pie_session", "pending_reap",
              "last_reap", "unowned_pie", "inflight", "machine"):
        assert k in full
    cli.main(["lease", "status", "--brief", "--agent", "B"] + base)
    raw = capsys.readouterr().out.strip()
    brief = json.loads(raw)
    assert set(brief) == {"ok", "holder", "queue", "my_position", "machine_holder"}
    assert brief["holder"]["agent"] == "A" and brief["holder"]["task"] == "doing a thing"
    assert isinstance(brief["holder"]["age_s"], int)
    assert brief["queue"] == ["B"] and brief["my_position"] == 1
    assert len(raw) < 300
    # --full beats $UAP_OUTPUT=brief
    os.environ["UAP_OUTPUT"] = "brief"
    try:
        cli.main(["lease", "status", "--full"] + base)
    finally:
        del os.environ["UAP_OUTPUT"]
    assert "queue_detail" in json.loads(capsys.readouterr().out)


def test_stale_diagnostics_are_pruned(fast_queue):
    old = time.time() - 2 * co._RECORD_MAX_AGE
    co._acquire_filelock(PROJ)
    try:
        st = co._load(PROJ)
        st["last_reap"] = {"why": "x", "reaped_at": old, "evicted_at": old}
        st["last_eviction"] = {"why": "y", "evicted_at": old}
        st["evicted"] = {"gone": {"evicted_at": old}, "fresh": {"evicted_at": time.time()}}
        co._save(PROJ, st)
    finally:
        co._release_filelock(PROJ)
    s = co.status(PROJ)
    assert s["last_reap"] is None and s["last_eviction"] is None
    assert list(s["evicted"]) == ["fresh"]


# --------------------------------------------------------------------------- status / log brief
def _fake_status(monkeypatch, *, contract_calls: list):
    monkeypatch.setattr(cli, "_rc_call", lambda func, params, project=None: "1.2.3")
    monkeypatch.setattr(cli, "_rc_port_for", lambda project=None: 30123)

    def contract(project=None):
        contract_calls.append(project)
        return {"state": "current", "checked_verbs": 3}
    monkeypatch.setattr(cli, "_contract_report", contract)


def test_status_default_keys_unchanged(monkeypatch, capsys):
    calls: list = []
    _fake_status(monkeypatch, contract_calls=calls)
    assert cli.main(["status"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert list(body) == ["ok", "rc_reachable", "plugin_version", "rc_port", "contract"]
    assert calls


def test_status_brief_skips_the_contract_unless_asked(monkeypatch, capsys):
    calls: list = []
    _fake_status(monkeypatch, contract_calls=calls)
    assert cli.main(["status", "--brief"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body == {"ok": True, "rc_port": 30123, "contract_ok": None,
                    "message": "contract not checked (pass --contract)"}
    assert calls == []
    assert cli.main(["status", "--brief", "--contract"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["contract_ok"] is True and calls


def test_status_probe_uses_a_short_timeout_and_retries_once(monkeypatch, capsys):
    from unreal_agent_player.errors import AgentError, ErrorCode
    seen: list = []

    def rc(func, params, project=None):
        seen.append(cli._RC_PROBE_TIMEOUT[0])
        if len(seen) == 1:
            raise AgentError(ErrorCode.UE_UNREACHABLE, "HTTP error: timed out")
        return "1.0"
    monkeypatch.setattr(cli, "_rc_call", rc)
    monkeypatch.setattr(cli, "_rc_port_for", lambda project=None: 1)
    monkeypatch.setattr(cli, "_contract_report", lambda project=None: {"state": "current"})
    assert cli.main(["status"]) == 0
    assert seen == [3.0, 3.0]
    assert cli._RC_PROBE_TIMEOUT[0] is None        # never leaks into later RC calls


def test_log_tail_brief_with_no_records_keeps_the_correctness_flags(monkeypatch, capsys):
    ring = _FakeRing()
    _wire(monkeypatch, ring)
    monkeypatch.setattr(cli, "_rc_port_for", lambda project=None: 1)
    assert cli.main(["log", "tail", "--grep", "nothing-matches-this", "--brief"]) == 0
    raw = capsys.readouterr().out.strip()
    body = json.loads(raw)
    assert body["count"] == 0 and "lines" not in body
    assert set(body) <= set(cli._LOG_BRIEF_KEYS)
    assert len(raw) < 300
    assert cli.main(["log", "tail", "--grep", "nothing-matches-this"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert "lines" in full and "scanned" in full


# --------------------------------------------------------------------------- help
def test_help_is_a_short_index_with_per_verb_detail(capsys):
    assert cli.main(["help"]) == 0
    index = capsys.readouterr().out
    assert len(index.encode()) < 3200
    for verb in ("status", "lease", "pie", "batch", "timing report", "--max-block",
                 "lease wait", "UAP_OUTPUT"):
        assert verb in index
    assert cli.main(["help", "--all"]) == 0
    full = capsys.readouterr().out
    assert cli._HELP_CATALOG.strip() in full and len(full) > 20000
    assert cli.main(["help", "lease"]) == 0
    lease = capsys.readouterr().out
    assert "MULTI-AGENT COORDINATION" in lease and "lease wait" in lease
    assert len(lease) < 8000
    assert cli.main(["help", "batch"]) == 0
    assert "uap batch" in capsys.readouterr().out
    assert cli.main(["help", "no-such-verb"]) == 2


def test_help_does_not_import_the_network_stack():
    code = ("import sys; from unreal_agent_player import cli; cli.main(['help']); "
            "print('HEAVY', [m for m in ('httpx', 'asyncio', 'unreal_agent_player.contract') "
            "if m in sys.modules])")
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(cli.__file__).resolve().parents[1]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         check=True).stdout
    assert "HEAVY []" in out


# --------------------------------------------------------------------------- guard fold
def test_uncontended_editor_call_takes_one_lock_pass(monkeypatch, capsys):
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    calls: list = []
    real = co._acquire_filelock

    def counting(project):
        calls.append(str(project))
        return real(project)
    monkeypatch.setattr(co, "_acquire_filelock", counting)
    _fake_status(monkeypatch, contract_calls=[])
    co.acquire(PROJ, "exclusive", reason="pie", agent="me", pid=0, wait=0)
    before = co.status(PROJ)["exclusive"]["heartbeat_at"]
    calls.clear()
    time.sleep(0.02)
    ring = _FakeRing()
    _wire(monkeypatch, ring)
    assert cli.main(["log", "tail", "--project", PROJ, "--agent", "me", "--task", "t"]) == 0
    # guard_pass + end_call. The old chain took 7.
    assert len(calls) == 2, calls
    st = co.status(PROJ)
    assert st["activity"]["me"]["task"] == "t"
    assert st["exclusive"]["heartbeat_at"] > before        # own lease refreshed
    assert st["inflight"] == {}                             # begin/end paired


def test_another_agents_lease_still_blocks_through_the_slow_path(monkeypatch, capsys):
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    co.acquire(PROJ, "exclusive", reason="pie", agent="other", pid=0, wait=0)
    ring = _FakeRing()
    _wire(monkeypatch, ring)
    assert cli.main(["log", "tail", "--project", PROJ, "--agent", "me"]) == 1
    body = _out(capsys)[-1]
    assert body["busy"] and body["blocked_by"] == "other" and "still_waiting" not in body


def test_max_block_caps_the_automatic_lease_wait(monkeypatch, capsys):
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.setenv("UAP_LEASE_WAIT", "900")
    monkeypatch.setattr(co, "POLL_SECONDS", 0.05)
    co.acquire(PROJ, "exclusive", reason="pie", agent="other", pid=0, wait=0)
    _wire(monkeypatch, _FakeRing())
    t0 = time.monotonic()
    assert cli.main(["log", "tail", "--project", PROJ, "--agent", "me",
                     "--max-block", "0.3"]) == 1
    assert time.monotonic() - t0 < 3
    body = _out(capsys)[-1]
    assert body["busy"] and body["still_waiting"] and body["resume"]


def test_a_rebuild_with_max_block_returns_busy_instead_of_running(monkeypatch, capsys):
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.setattr(co, "POLL_SECONDS", 0.05)
    co.acquire(PROJ, "exclusive", reason="rebuild", agent="rb", pid=0, wait=0)
    ran: list = []
    monkeypatch.setattr(cli, "_status", lambda args: ran.append(1) or 0)
    sp = cli.build_parser()
    args = sp.parse_args(["status", "--project", PROJ, "--max-block", "0.2"])
    args.func = cli._status
    assert cli._run_parsed(args) == 1
    body = _out(capsys)[-1]
    assert body["rebuilding"] and body["still_waiting"] and not ran


def test_eviction_notice_still_reaches_the_evicted_agent(monkeypatch, capsys):
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    co._acquire_filelock(PROJ)
    try:
        st = co._load(PROJ)
        st["evicted"] = {"me": {"why": "editor_not_running", "message": "gone",
                                "evicted_at": time.time(), "held_reason": "pie"}}
        co._save(PROJ, st)
    finally:
        co._release_filelock(PROJ)
    _wire(monkeypatch, _FakeRing())
    assert cli.main(["log", "tail", "--project", PROJ, "--agent", "me"]) == 1
    body = _out(capsys)[-1]
    assert body["lease_evicted"] and body["why"] == "editor_not_running"
    assert cli.main(["log", "tail", "--project", PROJ, "--agent", "me"]) == 0   # once only


# --------------------------------------------------------------------------- timing log
def _timing_rows(project=PROJ) -> list[dict]:
    p = cli._timing_path(project)
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()]


def test_timing_log_one_line_per_call_with_gap_and_no_args(capsys):
    assert cli.main(["lease", "status", "--project", PROJ, "--agent", "tim"]) == 0
    time.sleep(0.05)
    cli.main(["report", "note", "C:/secret/path/script text", "--project", PROJ,
              "--agent", "tim"])
    rows = _timing_rows()
    assert [r["verb"] for r in rows] == ["lease status", "report note"]
    for r in rows:
        assert set(r) == {"ts_start", "ts_end", "agent", "verb", "project", "duration_ms",
                          "guard_ms", "exit", "out_bytes", "gap_ms"}
        assert r["agent"] == "tim" and r["out_bytes"] > 0
    assert rows[0]["gap_ms"] is None and rows[1]["gap_ms"] >= 40
    assert "secret" not in cli._timing_path(PROJ).read_text(encoding="utf-8")


def test_timing_log_can_be_turned_off_and_never_raises(monkeypatch, capsys):
    monkeypatch.setenv("UAP_TIMING_LOG", "0")
    cli.main(["lease", "status", "--project", PROJ])
    assert not cli._timing_path(PROJ).exists()
    monkeypatch.delenv("UAP_TIMING_LOG")
    monkeypatch.setattr(cli, "_timing_path", lambda project: 1 / 0)
    assert cli.main(["lease", "status", "--project", PROJ]) == 0


def test_timing_log_rotates_past_the_cap(monkeypatch):
    p = cli._timing_path(PROJ)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 100)
    monkeypatch.setattr(cli, "_TIMING_MAX_BYTES", 10)
    monkeypatch.setattr(time, "monotonic_ns", lambda: 0)     # the 1-in-50 size check fires
    args = cli.build_parser().parse_args(["lease", "status", "--project", PROJ])
    cli._timing_record(args, time.time(), time.monotonic(), 0, 1)
    assert p.with_name(p.name + ".1").exists()


def test_timing_record_overhead_is_under_a_millisecond():
    args = cli.build_parser().parse_args(["lease", "status", "--project", PROJ,
                                          "--agent", "perf"])
    for _ in range(20):
        cli._timing_record(args, time.time(), time.monotonic(), 0, 10)
    n = 300
    t0 = time.perf_counter()
    for _ in range(n):
        cli._timing_record(args, time.time(), time.monotonic(), 0, 10)
    per_ms = (time.perf_counter() - t0) / n * 1000
    print(f"timing_record overhead: {per_ms:.3f} ms/call")
    assert per_ms < 3.0       # measured ~0.8-1.5 ms on a busy workstation; slack for CI


def test_timing_report_summarises_per_agent(capsys):
    p = cli._timing_path(PROJ)
    p.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rows = []
    t = now - 100
    for i, (verb, gap) in enumerate([("lease acquire", None), ("pie start", 5000),
                                     ("exec", 30000), ("log tail", 1000), ("pie stop", 2000)]):
        rows.append({"ts_start": t, "ts_end": t + 1, "agent": "x", "verb": verb,
                     "project": PROJ, "duration_ms": 1000, "guard_ms": 250 if i == 0 else 0,
                     "exit": 0, "out_bytes": 10, "gap_ms": gap})
        t += 10
    rows.append({"ts_start": now - 99999, "ts_end": now - 99998, "agent": "x", "verb": "old",
                 "project": PROJ, "duration_ms": 1, "guard_ms": 0, "exit": 0, "out_bytes": 1,
                 "gap_ms": None})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n{torn line\n", encoding="utf-8")
    assert cli.main(["timing", "report", "--project", PROJ, "--since", "1h"]) == 0
    raw = capsys.readouterr().out.strip()
    body = json.loads(raw)
    (a,) = body["agents"]
    assert a["agent"] == "x" and a["calls"] == 5 and a["uap_s"] == 5.0 and a["wait_s"] == 0.2
    assert (a["gap_med_s"], a["gap_p90_s"], a["gap_max_s"]) == (5.0, 30.0, 30.0)
    assert a["top_gaps"][0] == "30.0s pie start -> exec"
    assert len(raw) < 600
    assert cli.main(["timing", "report", "--project", PROJ, "--agent", "nobody"]) == 0
    assert json.loads(capsys.readouterr().out)["agents"] == []
    assert cli.main(["timing", "report", "--since", "soon"]) == 2


def test_brief_guard_busy_drops_the_boilerplate_hint(monkeypatch, capsys):
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    co.acquire(PROJ, "exclusive", reason="pie", agent="other", pid=0, wait=0, task="x" * 60)
    _wire(monkeypatch, _FakeRing())
    assert cli.main(["log", "tail", "--project", PROJ, "--agent", "me", "--brief"]) == 1
    raw = capsys.readouterr().out.strip()
    body = json.loads(raw)
    assert body["busy"] and body["blocked_by"] == "other" and "hint" not in body
    assert len(raw) < 200
