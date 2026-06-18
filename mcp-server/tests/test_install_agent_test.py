import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
INSTALLER = REPO / "Install-AgentTest.ps1"


def _powershell():
    """Return a usable PowerShell executable name, or None to skip."""
    for exe in ("pwsh", "powershell"):
        if shutil.which(exe):
            return exe
    return None


def _run(tmp_project, *extra, python="C:/fake/python.exe", venv_base=None):
    ps = _powershell()
    if ps is None:
        pytest.skip("no PowerShell executable available")
    cmd = [
        ps, "-NoProfile", "-File", str(INSTALLER),
        "-ProjectRoot", str(tmp_project),
    ]
    if python is not None:           # explicit -Python is trusted verbatim
        cmd += ["-Python", python]
    if venv_base is not None:        # else default resolution under -VenvBase
        cmd += ["-VenvBase", venv_base]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture
def fake_project(tmp_path):
    (tmp_path / "FakeGame.uproject").write_text("{}", encoding="utf-8")
    return tmp_path


def test_installs_command_and_launcher_with_substituted_python(fake_project):
    res = _run(fake_project, python="C:/baked/python.exe")
    assert res.returncode == 0, res.stderr
    cmd_file = fake_project / ".claude" / "commands" / "agentplayertest.md"
    launcher = fake_project / "uap.ps1"
    assert cmd_file.is_file()
    assert launcher.is_file()
    launcher_text = launcher.read_text(encoding="utf-8")
    assert "C:/baked/python.exe" in launcher_text
    assert "__UAP_PYTHON__" not in launcher_text
    # Project identity baked from the .uproject (fixture creates FakeGame.uproject).
    assert "FakeGame" in launcher_text
    assert "__UAP_PROJECT__" not in launcher_text


def test_rerun_without_force_is_noop(fake_project):
    _run(fake_project)
    launcher = fake_project / "uap.ps1"
    launcher.write_text("SENTINEL", encoding="utf-8")
    res = _run(fake_project)
    assert res.returncode == 0
    assert launcher.read_text(encoding="utf-8") == "SENTINEL"  # not clobbered


def test_force_overwrites(fake_project):
    _run(fake_project)
    launcher = fake_project / "uap.ps1"
    launcher.write_text("SENTINEL", encoding="utf-8")
    res = _run(fake_project, "-Force")
    assert res.returncode == 0
    assert "SENTINEL" not in launcher.read_text(encoding="utf-8")


def test_append_agents_is_idempotent(fake_project):
    agents = fake_project / "AGENTS.md"
    agents.write_text("# Existing rules\n", encoding="utf-8")
    _run(fake_project, "-AppendAgents")
    once = agents.read_text(encoding="utf-8")
    assert "## Verifying runtime game behavior" in once
    _run(fake_project, "-AppendAgents")
    twice = agents.read_text(encoding="utf-8")
    assert twice.count("## Verifying runtime game behavior") == 1


def test_missing_venv_errors_on_default_resolution(fake_project):
    # No -Python -> installer resolves <VenvBase>/.venv/Scripts/python.exe; point VenvBase
    # at a dir with no venv so resolution fails (exit 4) and nothing is written.
    res = _run(fake_project, python=None, venv_base=str(fake_project / "nope"))
    assert res.returncode != 0
    assert not (fake_project / "uap.ps1").exists()
