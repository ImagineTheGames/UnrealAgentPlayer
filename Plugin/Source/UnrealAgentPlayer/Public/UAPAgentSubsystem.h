#pragma once

#include "CoreMinimal.h"
#include "EditorSubsystem.h"
#include "UAPAgentTypes.h"
#include "UAPAgentSubsystem.generated.h"

UCLASS()
class UNREALAGENTPLAYER_API UUAPAgentSubsystem : public UEditorSubsystem
{
    GENERATED_BODY()

public:
    virtual void Initialize(FSubsystemCollectionBase& Collection) override;
    virtual void Deinitialize() override;

    UFUNCTION(BlueprintCallable, Category="Agent|Status")
    FString GetPluginVersion() const;

    UFUNCTION(BlueprintCallable, Category="Agent|Status")
    FString ExecuteConsoleCommand(FString Command);

    // The RemoteControl HTTP port this editor's RC server actually bound. With two editors
    // open, the second auto-moves off the default 30010 to a free port; an agent CLI calls
    // this (over Python remote-exec, which is addressed per-editor) to learn the right port
    // to use for RemoteControl, instead of guessing 30010.
    UFUNCTION(BlueprintCallable, Category="Agent|Status")
    int32 GetRemoteControlPort() const;

    // Brings the editor's main window to the foreground. Works from inside the editor
    // process (no cross-process foreground lock), so an external agent can make the
    // editor frontmost before capturing screenshots or injecting clicks. Windows only.
    UFUNCTION(BlueprintCallable, Category="Agent|Editor")
    bool FocusEditorWindow();

    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    EAgentPIEPhase GetPIEPhase() const;

    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    double GetPIEElapsedSeconds() const;

    // Starts Play-In-Editor. Wraps ULevelEditorSubsystem::EditorRequestBeginPlay so agents
    // never touch the version-fragile raw engine API (the old PlayWorldEditorSubsystem path
    // does not exist on UE 5.7). Returns false if no editor world is available. PIE comes up
    // asynchronously -- poll IsInPIE() before reading game state or capturing a frame.
    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    bool StartPIE();

    // Stops Play-In-Editor. Wraps ULevelEditorSubsystem::EditorRequestEndPlay. Returns false
    // if the editor subsystem is unavailable.
    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    bool StopPIE();

    // True while a PIE/game world is live. Wraps ULevelEditorSubsystem::IsInPlayInEditor;
    // use it to wait for StartPIE() to take effect.
    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    bool IsInPIE() const;

    UFUNCTION(BlueprintCallable, Category="Agent|Log")
    int64 GetLogCursor() const;

    UFUNCTION(BlueprintCallable, Category="Agent|Log")
    FString GetLogsSince(int64 AfterCursor, int32 MaxLines,
                         FString CategoryFilter, EAgentLogVerbosity MinVerbosity) const;

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectKey(FString KeyName, bool bPressed, bool bRepeat);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectMouseMove(float X, float Y, bool bAbsolute);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectMouseButton(EAgentMouseButton Button, bool bPressed);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectAxis(FString AxisName, float Value);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectGamepad(EAgentGamepadButton Button, bool bPressed, float AnalogValue);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectXRButton(EAgentXRHand Hand, FString ButtonKeyName, bool bPressed);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool ClearXRControllerOverride(EAgentXRHand Hand);

    UFUNCTION(BlueprintCallable, Category="Agent|Helpers")
    TArray<FAgentHelperDescriptor> ListTestHelpers();

    UFUNCTION(BlueprintCallable, Category="Agent|Helpers")
    FString CallTestHelper(FString Name, FString JsonArgs);

    UFUNCTION(BlueprintCallable, Category="Agent|Perf")
    FString GetStatGroupText(FString GroupName);

    // Reads the on-screen UMG layer of the running PIE game (visible text, screen
    // position, focus) as a JSON string, so an agent can read prompts/labels instead
    // of injecting input blind. Empty/`available:false` when not in PIE.
    UFUNCTION(BlueprintCallable, Category="Agent|UI")
    FString DumpViewportUI();

    // Selects a CommonUI tab by its TabNameID on the live game's tab list (menus are
    // tab-driven). Returns false if no game-world CommonTabListWidgetBase is found or the
    // id doesn't exist. The #1 menu-navigation primitive an agent needs.
    UFUNCTION(BlueprintCallable, Category="Agent|UI")
    bool SelectTab(FString TabId);

    // Drives UI focus navigation through Slate (the path menus actually use -- distinct from
    // game input). Direction: up|down|left|right|accept|back. Returns whether Slate handled it.
    UFUNCTION(BlueprintCallable, Category="Agent|UI")
    bool NavigateUI(FString Direction);

    // Requests a screenshot of the composited game backbuffer INCLUDING the UMG/Slate
    // overlay (unlike HighResShot, which captures only the 3D scene). The engine writes
    // the PNG to Filename on the next rendered frame; poll for the file. Filename should
    // be an absolute path ending in .png. Returns false if no request could be made.
    UFUNCTION(BlueprintCallable, Category="Agent|Capture")
    bool CaptureViewportWithUI(FString Filename);

private:
    void OnPostPIEStarted(bool bSimulating);
    void OnPrePIEEnded(bool bSimulating);
    void OnEndPIE(bool bSimulating);
    void OnPausePIE(bool bSimulating);
    void OnResumePIE(bool bSimulating);
    void OnCancelPIE();

    EAgentPIEPhase CurrentPhase = EAgentPIEPhase::NotPlaying;
    double PIEStartTimeSeconds = 0.0;

    FDelegateHandle HPostPIEStarted;
    FDelegateHandle HPrePIEEnded;
    FDelegateHandle HEndPIE;
    FDelegateHandle HPausePIE;
    FDelegateHandle HResumePIE;
    FDelegateHandle HCancelPIE;

    TSharedPtr<class FAgentLogCapture> LogCapture;

    TArray<FAgentHelperDescriptor> HelperCache;
    void RefreshHelperCache();
};
