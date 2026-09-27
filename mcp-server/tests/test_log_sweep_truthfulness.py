"""The broad log sweep must not be able to answer "clean" when it did not look.

AGENTS.md in School's Out VR makes a broad Error/Warning sweep MANDATORY before an agent
finishes a task, and says silence reads as "did not look". Both doors into that sweep were
shut, for two unrelated reasons, and both answered `ok: true, count: 0`:

  * `--verbosity Error` / `--verbosity Warning` searched only what survived the plugin's
    fixed-size ring buffer. A PIE session logs far more records than the ring holds, so a
    sweep from a cursor taken before the action searched a window that had already been
    overwritten -- and said nothing was there.
  * `--grep "Error|Warning"` matched only the `message` field, so a record whose VERBOSITY
    is Error but whose text does not contain the word "Error" was invisible to it. Measured
    live against one editor: 19 Error records by verbosity, of which grep found 4.

The ring is modelled here rather than mocked away, because the defect IS the ring: the
filtering code is correct and still returns nothing. `_FakeRing` mirrors
Plugin/Source/UnrealAgentPlayerRuntime/Private/AgentLogCapture.cpp -- contiguous cursors from
a single NextCursor++, oldest-first scan, MaxLines counted against MATCHES, and
`OutCursor = last match`.
"""

import json

import pytest

from unreal_agent_player import cli
from unreal_agent_player.reporting import session as report_session

# EAgentLogVerbosity, UAPAgentTypes.h. PassesFilters keeps a record when its value is <= the
# requested minimum, so Error (2) is admitted by a Warning (3) sweep but not the reverse.
_V = {"NoLogging": 0, "Fatal": 1, "Error": 2, "Warning": 3,
      "Display": 4, "Log": 5, "Verbose": 6, "VeryVerbose": 7}


class _FakeRing:
    """FAgentLogCapture, in Python. Same eviction, same filters, same cursor arithmetic."""

    def __init__(self, capacity: int = 4096):
        self.capacity = capacity
        self.entries: list[dict] = []
        self.next_cursor = 1

    def serialize(self, verbosity: str, category: str, message: str) -> int:
        cursor = self.next_cursor
        self.next_cursor += 1
        self.entries.append({"cursor": cursor, "timestamp": 0.0, "category": category,
                             "verbosity": verbosity, "message": message})
        if len(self.entries) > self.capacity:       # Head wraps; the oldest record is gone.
            self.entries = self.entries[-self.capacity:]
        return cursor

    def get_log_cursor(self) -> int:
        return self.next_cursor - 1

    def read_since(self, params: dict) -> str:
        after = int(params["AfterCursor"])
        max_lines = int(params["MaxLines"])
        cat = params.get("CategoryFilter") or ""
        floor = _V[params.get("MinVerbosity") or "Log"]
        out = []
        for e in self.entries:                      # oldest -> newest, as ReadSince scans
            if len(out) >= max_lines:
                break
            if e["cursor"] <= after:
                continue
            if cat and e["category"] != cat:
                continue
            if _V[e["verbosity"]] <= floor:
                out.append(e)
        return json.dumps({"cursor": out[-1]["cursor"] if out else after, "lines": out})


def _wire(monkeypatch, ring: _FakeRing):
    """Route the CLI's RC calls at the ring and record which functions were called."""
    seen = []

    def fake(func, params, project=None):
        seen.append(func)
        if func == "GetLogCursor":
            return ring.get_log_cursor()
        if func == "GetLogsSince":
            return ring.read_since(params)
        raise AssertionError(f"unexpected RC call {func}")

    monkeypatch.setattr(cli, "_rc_call", fake)
    return seen


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


def _session_ring() -> tuple[_FakeRing, int]:
    """A PIE session shaped like the one that produced the false clean sweep.

    Cursor 2779 is taken before the action; the session then logs out to 27740. The startup
    errors the sweep is supposed to find sit near the beginning -- M_JanitorSkin failing to
    compile and the LogClass uninitialised-property errors -- and one Error lands late, inside
    the surviving tail, as the positive control.
    """
    ring = _FakeRing(capacity=4096)
    for i in range(1, 27741):
        if i == 1284:
            ring.serialize("Warning", "LogMaterial",
                           "[AssetLog] M_JanitorSkin.uasset: Failed to compile Material for "
                           "platform PCD3D_SM5, Default Material will be used in game.")
        elif 1392 <= i <= 1397:
            ring.serialize("Error", "LogClass",
                           "EnumProperty FMRUKEnvironmentRaycastHit::status is not "
                           "initialized properly. Module:MRUtilityKit")
        elif i == 27000:
            ring.serialize("Error", "LogJanitorAI",
                           "DELIVERY UNREACHABLE: carrying 1 to the detention cell")
        else:
            ring.serialize("Log", "LogTemp", f"routine line {i}")
    assert ring.get_log_cursor() == 27740
    return ring, 2779


def test_positive_control_the_sweep_can_see_an_error_inside_the_retained_tail(monkeypatch, capsys):
    """Before trusting a zero, show the instrument returns a hit. The late Error is retained,
    and the same command that answers 0 for the evicted ones finds this one."""
    ring, cursor = _session_ring()
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", str(cursor), "--lines", "4000", "--verbosity", "Error"]) == 0
    body = _out(capsys)
    assert body["count"] == 1
    assert body["lines"][0]["cursor"] == 27000
    assert "DELIVERY UNREACHABLE" in body["lines"][0]["message"]


def test_named_negative_the_evicted_errors_are_missed_but_no_longer_pass_as_clean(monkeypatch, capsys):
    """The seven early records (one Warning, six Errors) are gone from the ring, so the sweep
    cannot report them -- but it must no longer report their absence as an absence. 20865 of
    the 24961 records asked for were evicted, and the answer says so."""
    ring, cursor = _session_ring()
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", str(cursor), "--lines", "4000", "--verbosity", "Warning"]) == 0
    body = _out(capsys)
    assert body["truncated"] is True
    assert body["dropped"] == 20865            # 23645 - 2779 - 1, exact: cursors are contiguous
    assert body["oldest_cursor"] == 23645
    assert "TRUNCATED" in body["warning"] and "UNPROVEN" in body["warning"]
    # And the Warning it could not see is genuinely not in the returned lines -- the warning
    # is the only thing standing between that and a false "no errors".
    assert not [ln for ln in body["lines"] if "M_JanitorSkin" in ln["message"]]


def test_an_intact_window_is_not_flagged_and_costs_no_extra_call(monkeypatch, capsys):
    """A cursor taken recently enough is fully retained. Contiguity is provable from the main
    read alone, so nothing is probed and nothing is warned about."""
    ring = _FakeRing(capacity=4096)
    for i in range(1, 51):
        ring.serialize("Log", "LogTemp", f"line {i}")
    seen = _wire(monkeypatch, ring)
    assert cli.main(["log", "since", "10", "--lines", "100"]) == 0
    body = _out(capsys)
    assert body["count"] == 40 and body["dropped"] == 0
    assert "truncated" not in body and "warning" not in body
    assert seen.count("GetLogsSince") == 1


def test_nothing_logged_since_the_cursor_is_not_reported_as_truncation(monkeypatch, capsys):
    """An empty read at the head of the log is a real zero, not a blind spot: eviction only
    ever takes the OLDEST records, so anything logged past the cursor would still be there."""
    ring = _FakeRing(capacity=4096)
    for i in range(1, 51):
        ring.serialize("Log", "LogTemp", f"line {i}")
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", "50", "--lines", "100"]) == 0
    body = _out(capsys)
    assert body["count"] == 0 and body["dropped"] == 0
    assert "truncated" not in body
    assert body["oldest_cursor"] is None


def test_grep_finds_records_classified_error_whose_text_lacks_the_word(monkeypatch, capsys):
    """The second shut door. `--grep "Error|Warning"` is the documented broad sweep; matching
    only `message` hid every record whose classification, not whose prose, was the evidence."""
    ring = _FakeRing(capacity=4096)
    ring.serialize("Error", "LogClass",
                   "StructProperty FOculusXRBodyJoint::OrientationQuat is not initialized "
                   "properly. Module:OculusXRMovement")
    ring.serialize("Warning", "LogStreaming", "Failed to read file 'ButtonHoverHint.png'.")
    ring.serialize("Log", "LogTemp", "everything is fine here")
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", "0", "--grep", "Error|Warning"]) == 0
    body = _out(capsys)
    assert body["count"] == 2
    assert sorted(ln["verbosity"] for ln in body["lines"]) == ["Error", "Warning"]


def test_grep_still_matches_message_text_and_now_also_category(monkeypatch, capsys):
    ring = _FakeRing(capacity=4096)
    ring.serialize("Log", "LogJanitorAI", "chase started")
    ring.serialize("Log", "LogTemp", "Catch windup")
    ring.serialize("Log", "LogTemp", "unrelated")
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", "0", "--grep", "catch"]) == 0
    assert [ln["message"] for ln in _out(capsys)["lines"]] == ["Catch windup"]
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", "0", "--grep", "LogJanitorAI"]) == 0
    assert [ln["message"] for ln in _out(capsys)["lines"]] == ["chase started"]


def test_cli_log_reads_reach_the_report(monkeypatch, tmp_path):
    """AGENTS.md says to read logs through `uap log` SO the evidence lands in the report. The
    harvester matched only the MCP tool names (`log_since`), while the CLI records the same
    read as `log:since`, so nothing an agent read through the CLI was ever kept."""
    s = report_session.ReportSession(task="q", run_dir=tmp_path, quote="")
    body = {"ok": True, "count": 2, "lines": [
        {"cursor": 1, "category": "LogClass", "verbosity": "Error", "message": "boom"},
        {"cursor": 2, "category": "LogTemp", "verbosity": "Log", "message": "fine"},
    ]}
    report_session.record_call(s, "log:since", {}, body, 5)
    assert [ln["message"] for ln in s.logs] == ["boom"]


def test_a_cursor_from_before_an_editor_restart_is_not_reported_as_a_quiet_log(monkeypatch, capsys):
    """Third route to the same false "clean" answer. The ring is rebuilt on every editor start,
    the editor is shared, and any agent may bounce it with Restart-Editor.ps1 mid-task -- so a
    cursor taken before that points past the head of the current capture and `log since` used to
    answer `count: 0, ok: true` for a window it never looked at. Observed live: the capture's
    cursor went 3413 -> 1798 across a restart another agent performed during this very task."""
    ring = _FakeRing(capacity=4096)
    for i in range(1, 1799):
        ring.serialize("Log", "LogTemp", f"line {i}")
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", "3413", "--lines", "500", "--verbosity", "Warning"]) == 0
    body = _out(capsys)
    assert body["count"] == 0
    assert body["stale_cursor"] is True
    assert body["head_cursor"] == 1798
    assert "STALE CURSOR" in body["warning"]
    assert "truncated" not in body      # not eviction; a different failure, named differently


def test_a_cursor_at_the_head_is_a_real_zero_not_a_stale_one(monkeypatch, capsys):
    """The named negative for the check above: caught up is not the same as looking at the wrong
    process, and only one of the two deserves a warning."""
    ring = _FakeRing(capacity=4096)
    for i in range(1, 51):
        ring.serialize("Log", "LogTemp", f"line {i}")
    _wire(monkeypatch, ring)
    assert cli.main(["log", "since", "50", "--lines", "100"]) == 0
    body = _out(capsys)
    assert body["count"] == 0 and body["head_cursor"] == 50
    assert "stale_cursor" not in body and "warning" not in body
