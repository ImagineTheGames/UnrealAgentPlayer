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
    // Starts PIE in a specific play mode. Mode: "flat" (default PIE window) or "vr"
    // (the editor's VR Preview, i.e. the HMD code path -- OpenXR input, IsHeadMountedDisplay
    // Enabled() branches). Returns a JSON status: {"ok":true,"mode":"vr"} or
    // {"ok":false,"error":"..."}; "vr" fails cleanly when no HMD is connected instead of
    // silently starting flat PIE and making an HMD-only bug look absent.
    UFUNCTION(BlueprintCallable, Category="Agent|PIE")
    FString StartPIEMode(FString Mode);

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

    // --- Sustained input ------------------------------------------------------------------
    // One injected event cannot drive sustained locomotion from outside the editor: the CLI
    // round-trip is ~1s, and a latched key is silently dropped by any FlushPressedKeys. These
    // re-assert the input every frame IN-ENGINE for Seconds, then release cleanly.
    //
    // All four return JSON so a refusal can say WHY. They validate fully BEFORE injecting, so
    // a refusal has zero side effects ("pressed":false is part of the contract), and they
    // unwind anything they pressed if a later step fails.

    // Press KeyName and hold it for Seconds (IE_Repeat every frame), then release.
    // {"ok":true,"key":..,"seconds":..,"pressed":true} | {"ok":false,"key":..,"error":..,"pressed":false}
    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    FString HoldKey(FString KeyName, float Seconds);

    // Drive an analog axis FKey at Value every frame for Seconds, then recentre to 0. This is
    // the VR locomotion verb: thumbstick keys are axes, not buttons (e.g.
    // OculusTouch_Left_Thumbstick_Y). A single sample is not what the game sees -- a real
    // stick re-sends its value every frame.
    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    FString HoldAxis(FString AxisKeyName, float Value, float Seconds);

    // Release input, and the RECOVERY escape hatch. An empty KeyName releases every registry
    // entry AND flushes every key the engine still has down, so a key the registry lost track
    // of can always be cleared without restarting PIE. A named key is force-released whether
    // or not the registry knows about it.
    // {"ok":true,"released":N,"controllers_flushed":N,"flushed":bool}
    // {"ok":true,"key":..,"released":N,"was_held":bool,"forced":bool,"down_before":bool}
    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    FString ReleaseHeldInput(FString KeyName);

    // JSON: {"ok":true,"held":[{key, analog, value, remaining_seconds, down}]}. `down` is
    // engine ground truth; if it disagrees with the registry, use ReleaseHeldInput("").
    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    FString GetHeldInput();

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked);

    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    bool ClearXRControllerOverride(EAgentXRHand Hand);

    UFUNCTION(BlueprintCallable, Category="Agent|Helpers")
    TArray<FAgentHelperDescriptor> ListTestHelpers();

    // Same list as ListTestHelpers, as a JSON string. Use THIS from an agent CLI: the
    // RemoteControl preset-call route serializes a returned struct with a property filter
    // that only admits the function's own out/return params, so every nested field of
    // FAgentHelperDescriptor is dropped and ListTestHelpers comes back as [{},{},...].
    // A JSON string return sidesteps the engine filter entirely (same shape as
    // DumpViewportUI / GetLogsSince / CallTestHelper).
    UFUNCTION(BlueprintCallable, Category="Agent|Helpers")
    FString ListTestHelpersJson();

    // --- Frame-rate sampling ---------------------------------------------------------------
    // Records a property once per frame in-engine, so an agent can measure sub-second behaviour
    // (judder, a 0.6s wind-up, a one-frame pop) that a ~1s exec round-trip cannot see.
    UFUNCTION(BlueprintCallable, Category="Agent|Sample")
    FString StartPropertySample(FString ObjectPath, FString PropertyPath, float Seconds, int32 MaxSamples);

    UFUNCTION(BlueprintCallable, Category="Agent|Sample")
    FString ReadPropertySample();

    UFUNCTION(BlueprintCallable, Category="Agent|Sample")
    bool StopPropertySample();

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
