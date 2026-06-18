import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
KIT = REPO / "agent-testing"


def _read(name):
    return (KIT / name).read_text(encoding="utf-8")


def test_launcher_template_has_token_and_home_fallback():
    text = _read("uap.ps1.template")
    assert "__UAP_PYTHON__" in text
    assert "UAP_HOME" in text
    assert "unreal_agent_player.cli" in text
    assert "$LASTEXITCODE" in text
