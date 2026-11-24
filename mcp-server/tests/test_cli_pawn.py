"""`uap pawn teleport|place` and the Grip View allowlist on the exec PIE-write guard.

Verify job 44985d55 (2026-11-24): the guard (CRASH-0BD72FF1) blocked Grip View's rig and left no way
to put the VR pawn beside a test object.
"""

import json
import os
import warnings
from typing import ClassVar

import pytest

from unreal_agent_player import cli, pawn_move, xr_reach


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("UAP_RC_PORT", "30010")
    monkeypatch.setenv("UAP_MACHINE_LOCK", "0")
    monkeypatch.delenv("UAP_PIE_WRITE_ALLOW", raising=False)


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# --- probe source ---------------------------------------------------------------------------

def test_probe_code_compiles_and_writes_no_property():
    codes = [pawn_move.teleport_code(1, 2, 3), pawn_move.teleport_code(1, 2, 3, 90.0, 1),
             pawn_move.place_code('Cap"x', 295, 180)]
    for code in codes:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # no "is not with a literal" etc.
            compile(code, "probe", "exec")
        # The whole point: nothing the exec PIE-write guard refuses, and no python-file-extension text.
        assert cli._EXEC_PIE_WRITE_RE.search(code.replace("set_control_rotation", "")) is None
        assert ".py" not in code
        assert "teleport" in code and "UEDPIE" in code


def test_probe_refuses_non_pie_world_and_logs():
    code = pawn_move.teleport_code(0, 0, 0)
    assert "is not a PIE world" in code and "_u.log(" in code


# --- verbs ----------------------------------------------------------------------------------

def _wire(monkeypatch, result):
    seen = []

    def fake(code, args):
        seen.append(code)
        return result

    monkeypatch.setattr(cli, "_exec_probe", fake)
    return seen


def test_teleport_runs_probe_and_reports(monkeypatch, capsys):
    res = {"ok": True, "pawn": "BP_Pawn_0", "from": [0, 0, 0], "to": [10, 20, 30], "yaw": 90.0}
    seen = _wire(monkeypatch, res)
    assert cli.main(["pawn", "teleport", "10", "20", "30", "--yaw", "90"]) == 0
    out = _out(capsys)
    assert out["ok"] and out["to"] == [10, 20, 30] and out["action"] == "pawn teleport"
    assert "(10.0, 20.0, 30.0)" in seen[0] and "[90.0]" in seen[0]


def test_place_passes_actor_dist_bearing(monkeypatch, capsys):
    seen = _wire(monkeypatch, {"ok": True, "to": [1, 2, 3]})
    assert cli.main(["pawn", "place", "Loop", "--dist", "295", "--bearing", "90"]) == 0
    assert '"Loop"' in seen[0] and "295.0" in seen[0] and "90.0" in seen[0]


def test_probe_failure_is_nonzero(monkeypatch, capsys):
    _wire(monkeypatch, {"ok": False, "error": "refusing: world X is not a PIE world"})
    assert cli.main(["pawn", "teleport", "0", "0", "0"]) == 1
    assert "PIE" in _out(capsys)["error"]


def test_pawn_is_a_guarded_instance_verb():
    assert "pawn" in cli._MACHINE_GUARDED and "pawn" in cli._LEASE_GUARDED
    assert "pawn" in cli._INSTANCE_RC_CMDS


def test_probe_shares_reach_marker():
    line = xr_reach.MARK + json.dumps({"ok": True})
    assert xr_reach.parse_probe([line]) == {"ok": True}


# --- Grip View allowlist ----------------------------------------------------------------------

RIG = "w = 'UEDPIE_0_X'\npawn.set_actor_location_and_rotation(v, r, False, True)\n"
GV = os.path.join("proj", "Scripts", "GripView", "gv_rig.py")


def test_allowlisted_path_matching(monkeypatch):
    assert cli._pie_write_allowlisted(GV)
    assert cli._pie_write_allowlisted(GV.replace("/", "\\").upper())
    assert not cli._pie_write_allowlisted(os.path.join("proj", "Scripts", "other.py"))
    assert not cli._pie_write_allowlisted(os.path.join("evil", "gv_rig.py"))
    assert not cli._pie_write_allowlisted(None)
    monkeypatch.setenv("UAP_PIE_WRITE_ALLOW", "/scripts/mine/rig.py")
    assert cli._pie_write_allowlisted(os.path.join("p", "Scripts", "Mine", "rig.py"))


def test_allowlist_relaxes_writes_but_not_attach():
    assert cli._exec_pie_write_refusal(RIG, "") is not None
    assert cli._exec_pie_write_refusal(RIG, "", allowlisted=True) is None
    assert cli._exec_pie_write_refusal("w='UEDPIE_0'\nc.set_editor_property('a',1)", "", allowlisted=True) is None
    for bad in ("w='UEDPIE_0'\na.attach_to_actor(o)", "w='UEDPIE_0'\na.rerun_construction_scripts()",
                "w='UEDPIE_0'\nc.k2_attach_to_component(p)"):
        assert "NOT run" in cli._exec_pie_write_refusal(bad, "", allowlisted=True)


class _Fake:
    ran: ClassVar[list] = []

    def __init__(self, *a, **k):
        pass

    def exec_python(self, code):
        _Fake.ran.append(code)
        return {"success": True, "result": "None", "output": []}


def test_exec_file_allowlisted_runs_and_others_refused(monkeypatch, tmp_path, capsys):
    _Fake.ran = []
    monkeypatch.setattr(cli, "PythonRemoteExecClient", _Fake)
    monkeypatch.setattr(cli, "_exec_client_is_noop", lambda *a, **k: True)
    good = tmp_path / "Scripts" / "GripView" / "gv_rig.py"
    good.parent.mkdir(parents=True)
    good.write_text(RIG, encoding="utf-8")
    other = tmp_path / "Scripts" / "other_rig.py"
    other.write_text(RIG, encoding="utf-8")
    assert cli.main(["exec-file", str(other)]) == 1
    assert "NOT run" in _out(capsys)["error"] and not _Fake.ran
    assert cli.main(["exec-file", str(good)]) == 0
    assert _Fake.ran
