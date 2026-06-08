import json
from pathlib import Path

import pytest

from unreal_agent_player.errors import ErrorCode
from unreal_agent_player.reporting import session as sess_mod
from unreal_agent_player.tools.report import (
    report_assert, report_caption, report_finish, report_note, report_start,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: True)
    sess_mod.clear_active()
    yield
    sess_mod.clear_active()


@pytest.mark.asyncio
async def test_start_opens_session():
    r = await report_start(rc=None, py_exec=None, task="T")
    assert r["ok"] is True
    assert Path(r["run_dir"]).exists()
    assert sess_mod.active() is not None


@pytest.mark.asyncio
async def test_assert_without_session_errors():
    r = await report_assert(rc=None, py_exec=None, label="x", passed=True)
    assert r["ok"] is False
    assert r["error"]["code"] == ErrorCode.REPORT_NO_SESSION.value


@pytest.mark.asyncio
async def test_finish_writes_html_and_clears():
    await report_start(rc=None, py_exec=None, task="T")
    await report_assert(rc=None, py_exec=None, label="moved", passed=True, evidence="710u")
    r = await report_finish(rc=None, py_exec=None, verdict="pass", summary="done")
    assert r["ok"] is True
    html_path = Path(r["html"])
    assert html_path.name == "index.html" and html_path.exists()
    assert "moved" in html_path.read_text(encoding="utf-8")
    data = json.loads((html_path.parent / "data.json").read_text(encoding="utf-8"))
    assert data["status"] == "pass"
    assert sess_mod.active() is None


@pytest.mark.asyncio
async def test_finish_bad_verdict_errors():
    await report_start(rc=None, py_exec=None, task="T")
    r = await report_finish(rc=None, py_exec=None, verdict="maybe", summary="")
    assert r["ok"] is False
    assert r["error"]["code"] == ErrorCode.SCHEMA_VALIDATION.value
