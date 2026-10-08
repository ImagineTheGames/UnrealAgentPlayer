"""`uap screenshot --window`: capture + stamp a standalone game client's window.

Two-client tests (launch_2p_standalone.ps1 starts `UnrealEditor.exe <uproject> -game` per EOS
Dev Auth context) could not put proof in a report: `uap screenshot` only reaches the editor
viewport, and a manual attach of unknown origin auto-fails the pass. One report passed with
`--no-require-screenshot` while its frames lived only in a doc.

These pin the parts that decide whether a picture is PROOF: which window a selector picks (and
that an ambiguous one is refused, not guessed), what the stamp says, that a blank frame is
never attached, and that the report gate accepts a stamped client frame for its project.
Win32 is stubbed: list_candidates / capture_bgra are the only OS calls.
"""
from __future__ import annotations

import json
import struct
import zlib

import pytest

from unreal_agent_player import cli
from unreal_agent_player import window_capture as wc
from unreal_agent_player.reporting import session as sess
from unreal_agent_player.reporting.render import render

UPROJ = r"E:\ImagineGames\SchoolsOutVR\SchoolsOut.uproject"
EDITOR_EXE = r"E:\ImagineGames\IG_MetaEngine\Engine\Binaries\Win64\UnrealEditor.exe"


def _client_cmdline(ctx: str, extra: str = "") -> str:
    # The exact shape launch_2p_standalone.ps1 produces.
    return (f'"{EDITOR_EXE}" "{UPROJ}" -game -windowed -ResX=1280 -ResY=720 -messaging '
            f'-ExecCmds="vrmenu.skiponboarding 1" -DevAuthToolName={ctx} -WinX=0 -WinY=0 '
            f'-ABSLOG="E:\\ImagineGames\\SchoolsOutVR\\Saved\\Logs\\Standalone_{ctx}.log" {extra}')


def _cand(pid, title, cmdline=None, exe=EDITOR_EXE, w=1280, h=720, hwnd=None):
    c = wc.Candidate(hwnd=hwnd or pid * 10, pid=pid, title=title, width=w, height=h,
                     exe=exe, cmdline=cmdline)
    c.info = wc.parse_cmdline(cmdline, exe)
    return c


def _desktop():
    return [
        _cand(100, "SchoolsOut (64-bit, PCD3D_SM6) ", _client_cmdline("Context_1")),
        _cand(200, "SchoolsOut (64-bit, PCD3D_SM6) ", _client_cmdline("Context_2", "-nohmd -NOSTEAM")),
        _cand(300, "SchoolsOut - Unreal Editor", f'"{EDITOR_EXE}" "{UPROJ}"', w=2560, h=1400),
        _cand(400, "Notepad", "notepad.exe", exe=r"C:\Windows\notepad.exe"),
    ]


def _bgra(w, h, fn):
    out = bytearray(w * h * 4)
    for y in range(h):
        for x in range(w):
            r, g, b = fn(x, y)
            o = (y * w + x) * 4
            out[o:o + 4] = bytes((b, g, r, 255))
    return bytes(out)


def _scene(w=64, h=36, shift=0):
    return _bgra(w, h, lambda x, y: ((x * 4 + shift) % 256, (y * 7) % 256, 90))


# --- command line -----------------------------------------------------------------------------

def test_parse_cmdline_reads_context_project_and_game_flag():
    info = wc.parse_cmdline(_client_cmdline("Context_2"), EDITOR_EXE)
    assert info["context"] == "Context_2"
    assert info["project"] == "SchoolsOut"
    assert info["uproject"] == UPROJ
    assert info["game_client"] is True
    assert info["log"].endswith("Standalone_Context_2.log")


def test_parse_cmdline_editor_is_not_a_game_client():
    info = wc.parse_cmdline(f'"{EDITOR_EXE}" "{UPROJ}" -GAMEUSERSETTINGSINI=PIEGameUserSettings1',
                            EDITOR_EXE)
    assert info["game_client"] is False          # -gameusersettingsini is not -game
    assert info["context"] is None
    assert info["project"] == "SchoolsOut"


def test_parse_cmdline_packaged_build_takes_project_from_exe():
    exe = r"D:\Builds\Windows\SchoolsOut\Binaries\Win64\SchoolsOut-Win64-Shipping.exe"
    info = wc.parse_cmdline(f'"{exe}" -windowed', exe)
    assert info["project"] == "SchoolsOut"
    # ...but an arbitrary program is never given a project.
    assert wc.parse_cmdline("notepad.exe", r"C:\Windows\notepad.exe")["project"] is None


def test_parse_cmdline_quoted_context():
    assert wc.parse_cmdline('x.exe -DevAuthToolName="Context 3"')["context"] == "Context 3"


# --- selection --------------------------------------------------------------------------------

def test_select_by_context_is_exact_and_case_insensitive():
    c, how = wc.select(_desktop(), "context_2")
    assert (c.pid, how) == (200, "context")


def test_select_by_pid_both_spellings():
    assert wc.select(_desktop(), "pid:100")[0].pid == 100
    assert wc.select(_desktop(), "400")[1] == "pid"


def test_select_by_unique_title_substring():
    c, how = wc.select(_desktop(), "notepad")
    assert (c.pid, how) == (400, "title")


def test_select_refuses_ambiguous_title_and_lists_candidates():
    with pytest.raises(wc.SelectError) as ei:
        wc.select(_desktop(), "SchoolsOut")
    assert "ambiguous" in str(ei.value)
    assert {c.pid for c in ei.value.candidates} == {100, 200, 300}


def test_select_refuses_two_clients_on_the_same_context():
    desk = _desktop() + [_cand(500, "SchoolsOut", _client_cmdline("Context_2"))]
    with pytest.raises(wc.SelectError) as ei:
        wc.select(desk, "Context_2")
    assert {c.pid for c in ei.value.candidates} == {200, 500}
    assert "pid:" in str(ei.value)


def test_select_no_match_lists_unreal_windows_only():
    with pytest.raises(wc.SelectError) as ei:
        wc.select(_desktop(), "Context_9")
    assert {c.pid for c in ei.value.candidates} == {100, 200, 300}


def test_select_unknown_pid_is_refused():
    with pytest.raises(wc.SelectError):
        wc.select(_desktop(), "pid:999")


def test_select_keeps_the_largest_window_of_a_process():
    desk = [_cand(100, "splash", _client_cmdline("Context_1"), w=300, h=200, hwnd=1),
            _cand(100, "game", _client_cmdline("Context_1"), w=1280, h=720, hwnd=2)]
    c, _ = wc.select(desk, "Context_1")
    assert c.hwnd == 2


# --- pixels -----------------------------------------------------------------------------------

def test_black_frame_is_blank():
    st = wc.image_stats(64, 36, _bgra(64, 36, lambda x, y: (0, 0, 0)))
    assert st["blank"] and "black" in st["reason"]


def test_flat_colour_frame_is_blank():
    st = wc.image_stats(64, 36, _bgra(64, 36, lambda x, y: (40, 120, 200)))
    assert st["blank"] and "flat colour" in st["reason"]


def test_real_scene_is_not_blank_even_if_partly_dark():
    st = wc.image_stats(64, 36, _bgra(64, 36, lambda x, y: (0, 0, 0) if x < 40 else (x * 3, y, 50)))
    assert st["blank"] is False


def test_png_round_trips_pixels_in_rgb_order():
    w, h = 3, 2
    px = _bgra(w, h, lambda x, y: (10 * x, 20 * y, 7))
    png = wc.bgra_to_png(w, h, px)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    ihdr_w, ihdr_h = struct.unpack(">II", png[16:24])
    assert (ihdr_w, ihdr_h) == (w, h)
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41:41 + idat_len])
    row1 = raw[(1 + w * 3) * 1:(1 + w * 3) * 2]
    assert row1[0] == 0                              # filter byte
    assert tuple(row1[1 + 2 * 3:1 + 3 * 3]) == (20, 20, 7)   # pixel (2,1) = r20 g20 b7


def test_frame_difference_zero_for_identical_and_positive_for_motion():
    a = _scene()
    assert wc.frame_difference(64, 36, a, a) == 0.0
    assert wc.frame_difference(64, 36, a, _scene(shift=40)) > 0.5


# --- stamp ------------------------------------------------------------------------------------

def test_stamp_and_description_name_the_client():
    c = _desktop()[1]
    st = wc.make_stamp(c, matched_by="context", selector="Context_2", width=1280, height=720,
                       frame=2, frames=4, offset_ms=500)
    assert st["kind"] == "window" and st["pid"] == 200 and st["context"] == "Context_2"
    assert st["project"] == "SchoolsOut" and st["game_client"] is True
    line = wc.describe_source(st)
    assert "client Context_2" in line and "pid 200" in line and "SchoolsOut" in line
    assert "-game" in line and "frame 2/4" in line


# --- the verb, end to end through the CLI (Win32 stubbed) -------------------------------------

@pytest.fixture
def stub_os(monkeypatch):
    frames = {"n": 0, "fn": lambda n: _scene(shift=n * 30)}

    def capture(hwnd):
        frames["n"] += 1
        return 64, 36, frames["fn"](frames["n"])

    monkeypatch.setattr(wc, "list_candidates", _desktop)
    monkeypatch.setattr(wc, "capture_bgra", capture)
    return frames


def _run(capsys, argv):
    rc = cli.main(argv)
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return rc, json.loads(out)


def _start(capsys, agent="shot-agent"):
    rc, _ = _run(capsys, ["report", "start", "two-client mouths", "--project", "SchoolsOut",
                          "--agent", agent])
    assert rc == 0


def _finish(capsys, verdict="pass"):
    return _run(capsys, ["report", "finish", verdict, "summary", "--keep-pie", "--no-open",
                         "--agent", "shot-agent"])


def test_window_shot_is_stamped_attached_and_passes_the_gate(capsys, tmp_path, stub_os):
    _start(capsys)
    f = str(tmp_path / "b.png")
    rc, body = _run(capsys, ["screenshot", f, "--window", "Context_2", "--caption", "mouth open",
                             "--agent", "shot-agent"])
    assert rc == 0 and body["ok"], body
    assert body["provenance"] == "SchoolsOut"
    assert body["target"]["pid"] == 200 and body["matched_by"] == "context"
    assert body["frames"][0]["source"]["context"] == "Context_2"
    assert (tmp_path / "b.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert body["attached"] == ["screenshots/000.png"]

    run = sess.get_active_run()
    data = json.loads((run / "data.json").read_text(encoding="utf-8"))
    shot = data["screenshots"][0]
    assert shot["provenance"] == "SchoolsOut"
    assert shot["source"]["pid"] == 200

    rc, fin = _finish(capsys)
    assert fin["verdict"] == "pass" and fin["downgraded"] is False
    html = (run / "index.html").read_text(encoding="utf-8")
    assert "client Context_2" in html and "pid 200" in html


def test_window_burst_saves_n_stamped_frames_and_reports_motion(capsys, tmp_path, stub_os):
    _start(capsys)
    f = str(tmp_path / "talk.png")
    rc, body = _run(capsys, ["screenshot", f, "--window", "pid:100", "--frames", "3",
                             "--interval-ms", "0", "--agent", "shot-agent"])
    assert rc == 0 and body["ok"], body
    paths = [fr["path"] for fr in body["frames"]]
    assert [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in paths] == [
        "talk_f01.png", "talk_f02.png", "talk_f03.png"]
    assert [fr["source"]["frame"] for fr in body["frames"]] == [1, 2, 3]
    assert all(fr["source"]["frames"] == 3 for fr in body["frames"])
    assert body["motion"]["max_changed"] > 0 and "warning" not in body
    assert len(body["attached"]) == 3


def test_identical_burst_warns_no_motion(capsys, tmp_path, stub_os):
    stub_os["fn"] = lambda n: _scene()
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "s.png"), "--window", "Context_1",
                             "--frames", "2", "--interval-ms", "0"])
    assert rc == 0
    assert body["motion"]["identical_pairs"] == 1 and "IDENTICAL" in body["warning"]


def test_blank_frame_fails_loudly_is_not_attached_and_cannot_pass(capsys, tmp_path, stub_os):
    stub_os["fn"] = lambda n: _bgra(64, 36, lambda x, y: (0, 0, 0))
    _start(capsys)
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "k.png"), "--window", "Context_2",
                             "--agent", "shot-agent"])
    assert rc == 1 and body["ok"] is False
    assert body["blank_frames"] == [1] and "BLANK" in body["error"]
    assert body["attached"] == []
    rc, fin = _finish(capsys)
    assert fin["verdict"] == "fail" and fin["downgraded"] is True


def test_ambiguous_selector_is_refused_with_candidates(capsys, tmp_path, stub_os):
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "x.png"), "--window", "SchoolsOut"])
    assert rc == 1 and "ambiguous" in body["error"]
    assert {c["pid"] for c in body["candidates"]} == {100, 200, 300}
    assert stub_os["n"] == 0                     # nothing was captured
    assert not (tmp_path / "x.png").exists()


def test_window_without_project_is_attached_but_not_proof(capsys, tmp_path, stub_os):
    _start(capsys)
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "n.png"), "--window", "Notepad",
                             "--agent", "shot-agent"])
    assert rc == 0 and body["provenance"] is None and "warning_provenance" in body
    rc, fin = _finish(capsys)
    assert fin["verdict"] == "fail"


def test_relative_path_and_bad_frames_are_refused(capsys, tmp_path, stub_os):
    rc, body = _run(capsys, ["screenshot", "rel.png", "--window", "Context_2"])
    assert rc == 1 and "ABSOLUTE" in body["error"]
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "a.png"), "--window", "Context_2",
                             "--frames", "0"])
    assert rc == 1 and "--frames" in body["error"]


def test_window_shot_does_not_attach_to_another_agents_report(capsys, tmp_path, stub_os):
    _start(capsys, agent="someone-else")
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "o.png"), "--window", "Context_2",
                             "--agent", "shot-agent"])
    assert rc == 0 and "someone-else" in body["report"]
    assert "attached" not in body


def test_window_shot_skips_editor_lease_and_machine_lock(capsys, tmp_path, stub_os, monkeypatch):
    """It touches no editor, so it must not queue behind a rebuild, a lease or the machine lock."""
    def boom(*a, **k):
        raise AssertionError("window capture consulted editor coordination")
    for name in ("wait_while_rebuild", "wait_if_blocked", "heartbeat", "acquire_machine"):
        monkeypatch.setattr(cli._coord, name, boom)
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "c.png"), "--window", "Context_2"])
    assert rc == 0 and body["ok"]


def test_render_marks_unstamped_attach_as_unknown_source():
    html = render({"task": "t", "status": "fail", "screenshots": [
        {"file": "screenshots/000.png", "caption": "manual", "t": "12:00:00"}]})
    assert "source unknown" in html


def test_parser_routes_window_flags():
    a = cli.build_parser().parse_args(["screenshot", "C:\\a.png", "--window", "Context_2",
                                       "--frames", "4", "--interval-ms", "250"])
    assert a.func is cli._screenshot
    assert (a.window, a.frames, a.interval_ms) == ("Context_2", 4, 250)


def test_tokenless_capture_does_not_attach_to_an_owned_report(capsys, tmp_path, stub_os,
                                                              monkeypatch):
    monkeypatch.delenv("UAP_AGENT_ID", raising=False)
    _start(capsys, agent="someone-else")
    rc, body = _run(capsys, ["screenshot", str(tmp_path / "t.png"), "--window", "Context_2"])
    assert rc == 0 and "no --agent token" in body["report"]
    assert (tmp_path / "t.png").exists()
