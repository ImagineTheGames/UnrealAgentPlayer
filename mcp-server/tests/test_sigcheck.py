"""The pre-push guard that makes "say so before someone rebuilds" enforced rather than hoped.

A commit that describes the fix but not the break is a silent trap with a delayed fuse, and
unlike every other trap in docs/known-issues.md the victim is a teammate weeks later with no
context and no reason to suspect anything. So the check is on the push, where the commit
message can still be edited -- not on the sync, where it is already too late.
"""

import pathlib

import pytest

from unreal_agent_player import sigcheck

REPO = pathlib.Path(__file__).resolve().parents[2]

_OLD = """
UCLASS()
class UUAPAgentSubsystem : public UEditorSubsystem
{
    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectAxis(FString AxisName, float Value);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    FString HoldKey(FString KeyName, float Seconds);

    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    bool StopPIE();
};
"""

_NEW = """
UCLASS()
class UUAPAgentSubsystem : public UEditorSubsystem
{
    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectAxis(FString AxisName, float Value, FString SlateUser);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    FString HoldKey(FString KeyName, float Seconds);

    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    FString StopPIEEx();
};
"""


def test_a_new_required_parameter_is_a_breaking_change():
    """The real case: InjectAxis/InjectGamepad/HoldAxis gained a required FString SlateUser.
    They are BlueprintCallable, so every caller on the old arity breaks -- C++ at compile
    time, Blueprint whenever that asset is next compiled."""
    d = sigcheck.diff_signatures(sigcheck.signatures(_OLD), sigcheck.signatures(_NEW))
    assert d["changed"] == ["InjectAxis(FString, float) -> (FString, float, FString)"]
    assert d["removed"] == ["StopPIE"]
    assert d["added"] == ["StopPIEEx"]


def test_an_added_verb_is_not_a_break():
    """The other skew direction, and it is already handled: a project that has not rebuilt
    404s, and `_rc_require` names that as skew instead of letting it leak (#23). Blocking a
    push for it would be noise, and noise is how a real block gets ignored."""
    old = sigcheck.signatures(_OLD)
    new = dict(old, BrandNewVerb=[])
    d = sigcheck.diff_signatures(old, new)
    assert d["changed"] == [] and d["removed"] == []
    assert d["added"] == ["BrandNewVerb"]


def test_an_unchanged_header_is_clean():
    d = sigcheck.diff_signatures(sigcheck.signatures(_OLD), sigcheck.signatures(_OLD))
    assert d == {"changed": [], "removed": [], "added": []}


def test_report_names_the_break_says_who_it_hits_and_how_to_declare_it():
    d = {"changed": ["InjectAxis(FString, float) -> (FString, float, FString)"],
         "removed": ["StopPIE"], "added": []}
    text = sigcheck.report(d)
    assert "changed: InjectAxis(FString, float) -> (FString, float, FString)" in text
    assert "removed: StopPIE" in text
    assert "BlueprintCallable" in text                 # who else it silently hits
    assert "BREAKING(plugin):" in text                 # the exact thing to write
    assert "Restart-Editor.ps1" in text                # the remedy the reader will need
    assert "UAP_SKIP_SIGCHECK=1" in text               # and the way out


def test_main_passes_when_a_commit_message_declares_it(monkeypatch, capsys):
    monkeypatch.delenv("UAP_SKIP_SIGCHECK", raising=False)
    monkeypatch.setattr(sigcheck, "collect",
                        lambda base: {"changed": ["X() -> (int32)"], "removed": [], "added": []})
    monkeypatch.setattr(sigcheck, "declared", lambda base: True)
    assert sigcheck.main(["origin/main"]) == 0
    assert capsys.readouterr().out == ""


def test_main_blocks_when_nothing_declares_it(monkeypatch, capsys):
    monkeypatch.delenv("UAP_SKIP_SIGCHECK", raising=False)
    monkeypatch.setattr(sigcheck, "collect",
                        lambda base: {"changed": ["X() -> (int32)"], "removed": [], "added": []})
    monkeypatch.setattr(sigcheck, "declared", lambda base: False)
    assert sigcheck.main(["origin/main"]) == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_main_is_silent_when_nothing_broke(monkeypatch, capsys):
    monkeypatch.delenv("UAP_SKIP_SIGCHECK", raising=False)
    monkeypatch.setattr(sigcheck, "collect",
                        lambda base: {"changed": [], "removed": [], "added": ["NewVerb"]})
    assert sigcheck.main(["origin/main"]) == 0


def test_the_override_exists_and_works(monkeypatch):
    """A guard with no escape hatch gets disabled wholesale the first time it is wrong."""
    monkeypatch.setenv("UAP_SKIP_SIGCHECK", "1")
    monkeypatch.setattr(sigcheck, "collect", lambda base: pytest.fail("must not even look"))
    assert sigcheck.main(["origin/main"]) == 0


def test_declared_accepts_the_documented_markers(monkeypatch):
    for message in ("BREAKING(plugin): HoldAxis gained SlateUser",
                    "fix: whatever\n\nsignature change: HoldAxis"):
        monkeypatch.setattr(sigcheck, "_git", lambda *a, _m=message: _m)
        assert sigcheck.declared("origin/main") is True
    monkeypatch.setattr(sigcheck, "_git", lambda *a: "fix(input): resolve the Slate user")
    assert sigcheck.declared("origin/main") is False


def test_the_same_break_in_both_subsystems_is_reported_once(monkeypatch):
    """The editor and runtime subsystems mirror most verbs, so a real break is found twice. It
    is one problem; listing it twice reads as two and makes the block harder to act on."""
    monkeypatch.setattr(sigcheck, "_header_at",
                        lambda ref, path: _OLD if ref != "HEAD" else _NEW)
    assert sigcheck.collect("origin/main")["changed"] == [
        "InjectAxis(FString, float) -> (FString, float, FString)"]


def test_missing_base_revision_never_blocks_a_push(monkeypatch):
    """`git show` on a ref that is not there returns nothing. A first push, a shallow clone or
    a detached state must not be read as "every verb was removed"."""
    monkeypatch.setattr(sigcheck, "_git", lambda *a: "")
    assert sigcheck.collect("nope") == {"changed": [], "removed": [], "added": []}


def test_the_guidance_the_check_enforces_is_written_down():
    """A block whose reasoning lives only in a hook teaches nobody. CONTRIBUTING.md is where a
    contributor meets it, and it must carry the rule, the reasoning and the real example."""
    raw = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    text = " ".join(raw.split())      # the prose is hard-wrapped; assert on the words
    assert "If you change a signature, say so where someone will read it BEFORE they rebuild" \
        in text
    assert "silent trap with a delayed fuse" in text
    assert "a teammate weeks later" in text
    assert "SlateUser" in text and "BlueprintCallable" in text
    assert "BREAKING(plugin):" in text
    assert "UAP_SKIP_SIGCHECK=1" in text


def test_the_hook_actually_runs_the_check():
    hook = (REPO / ".githooks/pre-push").read_text(encoding="utf-8")
    assert "unreal_agent_player.sigcheck" in hook
    assert "UAP_SKIP_SIGCHECK" in hook
