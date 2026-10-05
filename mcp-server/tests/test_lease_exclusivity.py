"""Is the exclusive lease actually exclusive? (ClickUp 17tm466ftzp)

Measured 2026-09-25: agent `skylight` held exclusive with ttl 600, and `ladderproof` was
granted exclusive 147s later while that lease was live and unexpired. `skylight`'s PIE died
under it and it only found out because a later `pie stop` said `was_playing: false`.

These tests use REAL PROCESSES, not threads. The defect lives in a file-based read-modify-write
between processes, so a thread test with a shared interpreter would not exercise it. They run
against an isolated $UAP_REPORTS_DIR -- never the machine's real lease directory, which has live
agents in it.

The assertion is instant occupancy, not timestamp comparison: on being granted, a worker takes
an O_CREAT|O_EXCL token file. If that token already exists, somebody else is inside the critical
section AT THAT MOMENT, which is the definition of the lease not being exclusive. No clock
arithmetic and no way to argue with the result.

And it contends REPEATEDLY. An intermittent exclusivity failure declared fixed on one clean run
is worthless.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

WORKER = textwrap.dedent('''
    import json, os, random, sys, time
    from unreal_agent_player import coordination as c

    project, agent, rounds, occ, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
    violations = []
    for r in range(rounds):
        try:
            res = c.acquire(project, "exclusive", reason="pie", agent=agent,
                            pid=0, wait=90, ttl=30)
        except Exception as exc:
            # A refusal is acceptable; a silent double-grant is not. Record it so a run that
            # only ever errors cannot masquerade as a clean run.
            violations.append({"kind": "error", "agent": agent, "round": r, "detail": repr(exc)})
            continue
        if not res.get("granted"):
            violations.append({"kind": "not_granted", "agent": agent, "round": r,
                               "detail": json.dumps(res)[:300]})
            continue
        held = False
        try:
            fd = os.open(occ, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                who = open(occ, encoding="utf-8").read()
            except OSError:
                who = "?"
            violations.append({"kind": "concurrent_holders", "agent": agent, "round": r,
                               "detail": "granted exclusive while " + repr(who) +
                                         " was already inside the critical section"})
        else:
            held = True
            os.write(fd, agent.encode())
            os.close(fd)
            time.sleep(random.uniform(0.01, 0.05))
            st = c.status(project)
            holder = (st.get("exclusive") or {}).get("agent")
            if holder != agent:
                violations.append({"kind": "lease_lost", "agent": agent, "round": r,
                                   "detail": "we hold it but the lease names " + repr(holder)})
        if held:
            try:
                os.unlink(occ)
            except OSError:
                pass
        try:
            c.release(project, agent=agent)
        except Exception as exc:
            violations.append({"kind": "release_error", "agent": agent, "round": r,
                               "detail": repr(exc)})
    open(out, "w", encoding="utf-8").write(json.dumps(violations))
''')


def _contend(tmp_path, workers: int, rounds: int, label: str, extra_env=None) -> list[dict]:
    """Run `workers` real processes contending for the same exclusive lease."""
    reports = tmp_path / f"reports-{label}"
    reports.mkdir(parents=True, exist_ok=True)
    occ = tmp_path / f"occupied-{label}.token"
    env = dict(os.environ)
    env["UAP_REPORTS_DIR"] = str(reports)     # never the machine's real .leases
    env.pop("UAP_AGENT_ID", None)
    if extra_env:
        env.update(extra_env)

    procs = []
    outs = []
    for i in range(workers):
        out = tmp_path / f"{label}-w{i}.json"
        outs.append(out)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", WORKER, "proj-" + label, f"w{i}", str(rounds),
             str(occ), str(out)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    violations: list[dict] = []
    for p in procs:
        _o, e = p.communicate(timeout=600)
        if p.returncode != 0:
            violations.append({"kind": "worker_crashed", "detail": (e or "")[-600:]})
    for out in outs:
        if out.exists():
            violations.extend(json.loads(out.read_text(encoding="utf-8")))
    return violations


def test_exclusive_lease_is_exclusive_under_real_process_contention(tmp_path):
    """Six processes, eight rounds each. At most one may be inside at any instant."""
    violations = _contend(tmp_path, workers=6, rounds=8, label="basic")
    bad = [v for v in violations if v["kind"] in ("concurrent_holders", "lease_lost")]
    assert not bad, "the exclusive lease was not exclusive:\n" + "\n".join(
        f"  {v['kind']}: {v['agent']} round {v['round']}: {v['detail']}" for v in bad)
    # A run where nobody was ever granted would pass the check above while proving nothing.
    assert len(violations) < 6 * 8, f"no worker ever got the lease: {violations[:3]}"


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_exclusivity_holds_across_repeated_runs(tmp_path, attempt):
    """Repeated, because the failure is intermittent and one clean run means little."""
    violations = _contend(tmp_path, workers=4, rounds=6, label=f"rep{attempt}")
    bad = [v for v in violations if v["kind"] in ("concurrent_holders", "lease_lost")]
    assert not bad, "\n".join(
        f"  {v['kind']}: {v['agent']} round {v['round']}: {v['detail']}" for v in bad)


def test_a_torn_lease_file_is_never_read_as_free(tmp_path):
    """The mechanism, isolated: an unreadable lease state must not read as "nobody holds it".

    `_save` truncated-then-wrote and `_load` turned any parse error into a blank state whose
    `exclusive` is None. So a reader that caught the file mid-write concluded the lease was
    free, granted itself exclusive, and wrote that over the real holder's record -- which is
    how a live unexpired lease disappeared. "I could not read the answer" is not the same fact
    as "the answer is no", and at this exact spot the difference is the whole guarantee.
    """
    from unreal_agent_player import coordination as c

    os.environ["UAP_REPORTS_DIR"] = str(tmp_path)
    try:
        got = c.acquire("tornproj", "exclusive", reason="pie", agent="holder", pid=0, ttl=300)
        assert got["granted"]
        # Exactly what a reader sees mid-`write_text`: the file exists and is truncated.
        path = c._lease_path("tornproj")
        path.write_text("", encoding="utf-8")
        with pytest.raises(c.LeaseStateUnreadable):
            c.acquire("tornproj", "exclusive", reason="pie", agent="intruder", pid=0,
                      wait=0, ttl=300)
        # And a half-written object, not just an empty file.
        path.write_text('{"generation": 0, "exclusive": {"agent": "hol', encoding="utf-8")
        with pytest.raises(c.LeaseStateUnreadable):
            c.acquire("tornproj", "exclusive", reason="pie", agent="intruder", pid=0,
                      wait=0, ttl=300)
    finally:
        os.environ.pop("UAP_REPORTS_DIR", None)


def test_acquire_refuses_rather_than_proceeding_without_the_mutex(tmp_path, monkeypatch):
    """A lock it cannot take must stop it, not be shrugged off.

    `_acquire_filelock` used to give the mutex up after a timeout and carry on, on the grounds
    that a wedged lockfile must not brick coordination. But the grant decision is a
    read-modify-write, so two callers proceeding unsynchronised lose one of the two updates --
    and for an exclusive lease that means two holders. The wedged-lock case is handled by
    stale-breaking instead.
    """
    from unreal_agent_player import coordination as c

    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(c, "_FILELOCK_TIMEOUT", 0.3)
    monkeypatch.setattr(c, "_FILELOCK_STALE", 9999.0)   # never breakable, so it really is stuck
    lp = c._lock_path("stuckproj")
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("someone-else", encoding="utf-8")
    with pytest.raises(c.LeaseUnavailable):
        c.acquire("stuckproj", "exclusive", reason="pie", agent="a", pid=0, wait=0, ttl=300)


def test_a_wedged_lock_is_still_broken_so_coordination_cannot_brick(tmp_path, monkeypatch):
    """The other side of the trade: refusing must not mean hanging forever on a dead lock."""
    from unreal_agent_player import coordination as c

    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(c, "_FILELOCK_STALE", 0.0)      # anything already there is stale
    lp = c._lock_path("wedgedproj")
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("dead-process", encoding="utf-8")
    assert c.acquire("wedgedproj", "exclusive", reason="pie", agent="a",
                     pid=0, wait=0, ttl=300)["granted"] is True


def test_releasing_does_not_remove_a_lock_somebody_else_now_owns(tmp_path, monkeypatch):
    """One lost lock must not cascade into every pair of callers after it.

    Release used to unlink whatever lock file was there. So once a lock had been broken as
    stale and replaced, the original owner's release deleted the NEW owner's lock on the way
    out, leaving the next two callers unsynchronised as well.
    """
    from unreal_agent_player import coordination as c

    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    c._acquire_filelock("cascadeproj")
    lp = c._lock_path("cascadeproj")
    lp.write_text("99999:someone-else-took-it", encoding="utf-8")
    c._release_filelock("cascadeproj")
    assert lp.exists(), "released a lock owned by another process"
    assert lp.read_text(encoding="utf-8") == "99999:someone-else-took-it"


def test_a_reader_never_sees_a_partial_state_while_a_holder_exists(tmp_path):
    """Atomicity of `_save`, measured against a reader hammering the same file.

    This is the window the defect came through, so it is asserted directly rather than trusted
    to `os.replace`'s documentation.
    """
    from unreal_agent_player import coordination as c

    os.environ["UAP_REPORTS_DIR"] = str(tmp_path)
    try:
        c.acquire("atomicproj", "exclusive", reason="pie", agent="holder", pid=0, ttl=300)
        path = c._lease_path("atomicproj")
        big = {"generation": 1, "exclusive": {"agent": "holder", "pid": 0, "ttl": 300,
                                              "heartbeat_at": 0, "filler": "x" * 60000},
               "shared": [], "waiters": []}
        seen_partial = 0
        for _ in range(300):
            c._save("atomicproj", big)
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                seen_partial += 1
        assert seen_partial == 0, f"{seen_partial} torn reads of the lease file"
    finally:
        os.environ.pop("UAP_REPORTS_DIR", None)


def test_a_genuinely_absent_lease_file_is_still_free(tmp_path):
    """The other half: absent is not corrupt. A first-ever acquire must still work."""
    from unreal_agent_player import coordination as c

    os.environ["UAP_REPORTS_DIR"] = str(tmp_path)
    try:
        got = c.acquire("freshproj", "exclusive", reason="pie", agent="first", pid=0, ttl=300)
        assert got["granted"] is True
    finally:
        os.environ.pop("UAP_REPORTS_DIR", None)
