"""A low frame rate must EXPLAIN itself at the point it is reported.

The editor throttles to exactly 3.0 fps when it is not the foreground window. Agents kept
reading that number as a finding about the game (one filed a bug ticket off timing taken in
a throttled editor), because at the moment they see the number they are not reading docs.
These tests pin the explanation to every surface that reports a frame rate.
"""

import json

import pytest
from pytest_httpx import HTTPXMock

from unreal_agent_player import cli, throttle
from unreal_agent_player.reporting.render import render
from unreal_agent_player.tools.baseline import perf_baseline_compare, perf_baseline_save
from unreal_agent_player.tools.perf import perf_stat
from unreal_agent_player.transport import RemoteControlClient


@pytest.fixture(autouse=True)
def _pin_port(monkeypatch):
    monkeypatch.setenv("UAP_RC_PORT", "30010")


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --- threshold ---------------------------------------------------------------------------

@pytest.mark.parametrize("fps", [0.5, 3.0, 3.3, 5.0])
def test_rates_at_or_below_five_fps_are_the_throttle(fps):
    assert throttle.is_throttled_rate(fps=fps) is True


@pytest.mark.parametrize("fps", [5.1, 12.0, 30.0, 72.0, 120.5])
def test_a_plausible_rate_is_never_flagged(fps):
    # Even a badly blown frame budget lands at 10-20 fps; nothing legitimate lives under 5.
    assert throttle.is_throttled_rate(fps=fps) is False


def test_a_missing_reading_is_not_a_throttle():
    # 0 / None / non-numeric mean "no measurement", not "throttled" -- a missing stat must
    # never fabricate a warning.
    assert throttle.is_throttled_rate(fps=None, frame_ms=None) is False
    assert throttle.is_throttled_rate(fps=0.0) is False
    assert throttle.is_throttled_rate(fps="fast") is False
    assert throttle.is_throttled(None) is False
    assert throttle.is_throttled({}) is False


def test_frame_ms_alone_detects_it():
    assert throttle.is_throttled({"frame_ms": 333.33}) is True     # 3.0 fps
    assert throttle.is_throttled({"frame_ms": 200.0}) is True      # exactly 5.0 fps
    assert throttle.is_throttled({"frame_ms": 11.2}) is False


def test_the_note_says_all_four_things_an_agent_needs():
    n = throttle.THROTTLE_NOTE
    assert "foreground" in n                        # what it actually is
    assert "PIE window" in n and "re-measure" in n  # what to do
    assert "void" in n                              # measurements already taken
    # ...and what to do when focus CANNOT be taken.
    assert "SetForegroundWindow" in n and "GetForegroundWindow" in n
    assert "Slate.bAllowThrottling 0" in n and "does NOT lift it" in n
    assert "unmeasurable" in n
    assert "uap sample" in n and "input hold" in n  # what still measures correctly
    assert "agentplayertest.md" in n                # links to trap #1, does not restate it
    assert n.isascii()


def test_the_sampler_note_covers_the_same_ground():
    n = throttle.SAMPLER_THROTTLE_NOTE
    assert "3 Hz" in n and "void" in n and "re-sample" in n
    assert "SetForegroundWindow" in n and "unmeasurable" in n
    assert "agentplayertest.md" in n
    assert n.isascii()


# --- perf_stat ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perf_stat_explains_a_throttled_reading(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "FPS: 3.0"})
    rc = RemoteControlClient()
    result = await perf_stat(rc=rc, py_exec=None, stat_group="fps")
    assert result["throttled"] is True
    assert "foreground" in result["warning"]
    # The parsed metrics stay a dict of pure numbers: baseline comparison divides by them.
    assert result["parsed"] == {"fps": 3.0}
    await rc.aclose()


@pytest.mark.asyncio
async def test_perf_stat_explains_a_throttled_frame_time(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Frame: 333.33 ms\nGPU: 11.81 ms"})
    rc = RemoteControlClient()
    result = await perf_stat(rc=rc, py_exec=None, stat_group="unit")
    assert result["throttled"] is True
    await rc.aclose()


@pytest.mark.asyncio
async def test_perf_stat_stays_quiet_at_a_healthy_rate(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Frame: 8.21 ms\nGPU: 4.80 ms"})
    rc = RemoteControlClient()
    result = await perf_stat(rc=rc, py_exec=None, stat_group="unit")
    assert "throttled" not in result and "warning" not in result
    await rc.aclose()


# --- baselines ---------------------------------------------------------------------------

class _Store:
    def __init__(self, seed=None):
        self.data = dict(seed or {})

    def save(self, name, metrics):
        self.data[name] = metrics

    def load(self, name):
        return self.data.get(name)


@pytest.mark.asyncio
async def test_saving_a_baseline_in_a_throttled_editor_warns(httpx_mock: HTTPXMock):
    # A baseline captured at 3 fps poisons every comparison made against it later.
    httpx_mock.add_response(json={"ReturnValue": "Frame: 333.33 ms"})
    rc = RemoteControlClient()
    res = await perf_baseline_save(rc=rc, store=_Store(), name="hub_idle")
    assert res["throttled"] is True and "foreground" in res["warning"]
    await rc.aclose()


@pytest.mark.asyncio
async def test_a_regression_measured_while_throttled_says_so(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"ReturnValue": "Frame: 333.33 ms"})
    rc = RemoteControlClient()
    res = await perf_baseline_compare(
        rc=rc, store=_Store({"hub_idle": {"frame_ms": 8.2}}), name="hub_idle")
    assert res["regressed"] is True          # numerically, yes...
    assert res["throttled"] is True          # ...but the number is not about the game
    await rc.aclose()


# --- report diag -------------------------------------------------------------------------

def _stub_diag(monkeypatch, unit, fps):
    payload = {"plugin_version": "0.0.1", "world": "Map_X", "is_in_pie": True,
               "unit": unit, "fps": fps}

    class _Client:
        def __init__(self, node_project_substr=None):
            pass

        def exec_python(self, code):
            return {"output": [{"output": "UAPDIAG:" + json.dumps(payload)}]}

    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Client)


def test_report_diag_explains_three_fps_where_the_agent_reads_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    _stub_diag(monkeypatch, "Frame: 333.33 ms\nDraw: 5.57 ms\nGPU: 11.81 ms", "FPS: 3.0")
    assert cli.main(["report", "start", "does the chase work"]) == 0
    capsys.readouterr()
    assert cli.main(["report", "diag"]) == 0
    body = _out(capsys)
    assert body["perf"]["fps"] == 3.0
    assert body["throttled"] is True
    assert "NOT the game" in body["warning"]
    assert "PIE window" in body["warning"]


def test_report_diag_is_silent_at_a_healthy_rate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    _stub_diag(monkeypatch, "Frame: 11.20 ms", "FPS: 89.3")
    assert cli.main(["report", "start", "t"]) == 0
    capsys.readouterr()
    assert cli.main(["report", "diag"]) == 0
    body = _out(capsys)
    assert "throttled" not in body and "warning" not in body


# --- sampler -----------------------------------------------------------------------------

def _stub_rc(monkeypatch, table):
    def fake(func, params, project=None):
        return table[func]
    monkeypatch.setattr(cli, "_rc_call", fake)


def _series(step):
    return {"ok": True, "count": 4, "samples": [
        {"t": i * step, "v": {"x": float(i), "y": 0.0, "z": 0.0}} for i in range(4)]}


def test_a_sampler_running_at_three_hz_is_itself_the_tell(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"StartPropertySample": json.dumps({"ok": True}),
                           "ReadPropertySample": json.dumps(_series(0.3333))})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["sample", "start", "PlayerCameraManager", "WorldLocation",
                     "--seconds", "2"]) == 0
    body = _out(capsys)
    assert body["stats"]["hz"] == 3.0
    assert body["throttled"] is True
    assert "re-sample" in body["warning"]


def test_a_sampler_at_frame_rate_says_nothing(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"StartPropertySample": json.dumps({"ok": True}),
                           "ReadPropertySample": json.dumps(_series(0.011))})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["sample", "start", "PlayerPawn", "WorldLocation"]) == 0
    body = _out(capsys)
    assert "throttled" not in body


def test_sample_read_annotates_too(monkeypatch, capsys):
    _stub_rc(monkeypatch, {"ReadPropertySample": json.dumps(_series(0.3333))})
    assert cli.main(["sample", "read", "--summary"]) == 0
    assert _out(capsys)["throttled"] is True


# --- HTML report -------------------------------------------------------------------------

def _report(perf):
    return {"task": "t", "project": "P", "status": "pass", "started": "s", "finished": "f",
            "duration_s": 1.0, "quote": "q", "summary": "chase took 9s", "env": {},
            "perf": perf, "notes": [], "assertions": [], "timeline": [], "screenshots": [],
            "logs": []}


def test_html_report_explains_a_low_rate_to_the_human_reading_it_later():
    html = render(_report({"frame_ms": 333.33, "fps": 3.0}))
    assert "editor throttle, not the game" in html
    assert "SetForegroundWindow" in html
    # In the Overview panel, where a human lands -- not buried behind the Diagnostics tab.
    overview = html.split('data-panel="Overview"')[1].split('data-panel="Screenshots"')[0]
    assert "class='warn'" in overview


def test_html_report_says_nothing_at_a_healthy_rate():
    html = render(_report({"frame_ms": 11.2, "fps": 89.3}))
    assert "class='warn'" not in html


# --- the pointer at the perf surface, in the docs an agent actually meets ----------------

import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[2]


def test_uap_help_explains_the_low_number_where_the_verb_is_listed():
    h = cli._HELP_CATALOG
    assert "5 fps" in h and "foreground" in h.lower()
    assert "SetForegroundWindow" in h and "unmeasurable" in h
    assert "uap sample" in h


def test_the_agent_testing_kit_points_at_it_from_the_diag_step():
    md = (_REPO / "agent-testing" / "agentplayertest.md").read_text(encoding="utf-8")
    diag_step = md.split("**Diagnostics**")[1].split("**Scene**")[0]
    assert "throttled: true" in diag_step and "trap #1" in diag_step
    # Trap #1 itself gains the "cannot take focus" answer, and keeps its own headline.
    assert "GetForegroundWindow" in md and "unmeasurable" in md


def test_the_agents_snippet_says_the_tool_now_answers():
    md = (_REPO / "agent-testing" / "AGENTS-snippet.md").read_text(encoding="utf-8")
    assert "throttled:true" in md and "UNMEASURABLE" in md
