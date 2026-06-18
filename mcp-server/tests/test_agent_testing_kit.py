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
    # Pins the per-project target so the launcher never cross-targets another editor.
    assert "__UAP_PROJECT__" in text
    assert "UAP_PROJECT" in text


def test_command_template_is_project_agnostic():
    text = _read("agentplayertest.md")
    low = text.lower()
    # No project-specific leakage.
    assert "schoolsout" not in low
    assert "janitor" not in low
    assert "E:/ImagineGames" not in text
    # Core contract present.
    assert "uap report start" in text
    assert "uap report finish" in text
    assert "uap status" in text
    assert "$ARGUMENTS" in text
    # The report is mandatory and must be cited.
    assert "index.html" in text


AGENTS_MARKER = "## Verifying runtime game behavior"


def test_agents_snippet_has_marker_and_is_generic():
    text = _read("AGENTS-snippet.md")
    assert AGENTS_MARKER in text  # used by the installer for idempotent append
    low = text.lower()
    assert "janitor" not in low
    assert "/agentplayertest" in low
    assert "uap report finish" in text


def test_docs_no_stale_slate_input_claim():
    for rel in ("README.md", "docs/capabilities.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "ProcessKeyDownEvent" not in text, rel
        assert "in-process via Slate" not in text, rel
