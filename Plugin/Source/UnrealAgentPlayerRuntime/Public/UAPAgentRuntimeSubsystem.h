#pragma once
#include "CoreMinimal.h"
#include "Subsystems/GameInstanceSubsystem.h"
#include "UAPAgentTypes.h"
#include "UAPAgentRuntimeSubsystem.generated.h"

UCLASS()
class UNREALAGENTPLAYERRUNTIME_API UUAPAgentRuntimeSubsystem : public UGameInstanceSubsystem
{
    GENERATED_BODY()
public:
    virtual void Initialize(FSubsystemCollectionBase& Collection) override;
    virtual void Deinitialize() override;

    // The body of Initialize / Deinitialize, callable without a subsystem collection. A -game
    // process loads this module AFTER its GameInstance has collected its subsystems, so the engine
    // never creates this class there; the module then builds one by hand (see
    // FUnrealAgentPlayerRuntimeModule::EnsureStandaloneSubsystem) and drives it through these two.
    void InitializeRuntime();
    void ShutdownRuntime();

    UFUNCTION(BlueprintCallable, Category="Agent|Status") FString GetPluginVersion() const;
    UFUNCTION(BlueprintCallable, Category="Agent|Status") FString ExecuteConsoleCommand(FString Command);
    // CLIENT (last parameter of every verb below that injects, holds, releases or reads UI):
    // which PIE client of a multi-client session the verb acts on. "" = the first game world (the
    // historical default, so an old caller is unchanged), "N" = PIE instance N, "all" = every
    // client. It is a string for the RemoteControl reason documented on SlateUser: an omitted
    // value arrives zero-initialised, and "" is the only value that survives that as "default".
    // In-engine only - it never raises, focuses or sends OS input to a window. [17tm466z4a6]
    // ListClients JSON: {"ok":true,"clients":[{client, world, net_mode, default, accepts_input, window_title, pawn}]}
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString ListClients();
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectKey(FString KeyName, bool bPressed, bool bRepeat, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectMouseMove(float X, float Y, bool bAbsolute, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectMouseButton(EAgentMouseButton Button, bool bPressed, FString Client);
    // The mouse position inside a CAPTURED viewport. Win32 SetCursorPos and
    // FSlateApplication::SetCursorPos are both inert while PIE holds the mouse, so these do not
    // move the pointer -- they stamp the position onto the injected pointer events, which is
    // what Slate actually routes by. Both return JSON with a read-back of every layer and with
    // the WIDGET FOUND UNDER THE POINT, because the failure this replaced was a click that
    // reported ok:true from the top-left corner. X/Y on ClickMouse are strings so that
    // "omitted" is distinguishable from 0,0. See AgentInput.h.
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString SetMousePosition(float X, float Y, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString ClickMouse(EAgentMouseButton Button, FString X, FString Y, FString Client);
    // SlateUser: "" (or omitted) resolves the target Slate user automatically; "0"/"1"/... targets
    // one explicitly and REFUSES LOUDLY if Slate has no such user. It is a string because
    // RemoteControl zero-initialises the argument struct: an omitted int32 would arrive as 0,
    // a valid index, and would silently move every existing call onto the Slate route.
    // Why it exists at all: Slate DISCARDS an event whose user index does not match the
    // handler's owner (FAnalogCursor::IsRelevantInput -- engine AnalogCursor.cpp:192), with no
    // error, so a mis-stamped injection looks exactly like a broken feature.
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectAxis(FString AxisName, float Value, FString SlateUser, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectGamepad(EAgentGamepadButton Button, bool bPressed, float AnalogValue, FString SlateUser, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectXRButton(EAgentXRHand Hand, FString ButtonKeyName, bool bPressed, FString Client);
    // Sustained input: re-asserted every frame in-engine for Seconds, then released. A single
    // injected event cannot drive locomotion across a ~1s CLI round-trip, and HoldAxis is the
    // VR locomotion verb (thumbsticks are axis FKeys, not buttons). All return JSON so a
    // refusal says why; they validate before injecting, so a refusal has no side effects.
    // ReleaseHeldInput("") is the recovery hatch: it also flushes keys the registry lost
    // track of, so a stuck key never needs a PIE restart. See AgentInput.h.
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString HoldKey(FString KeyName, float Seconds, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString HoldAxis(FString AxisKeyName, float Value, float Seconds, FString SlateUser, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString ReleaseHeldInput(FString KeyName, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString GetHeldInput(FString Client);
    // Timed input script: the whole thing in ONE call, played in-engine against world time, so the
    // CLI round trip (0.2-0.6 s of jitter on `HoldKey`) cannot move a step [17tm466zqax].
    // ScriptJson {"steps":[{"kind":"hold"|"axis","key","value","start","end"}]} (seconds after 0);
    // StartAt "" = now, "+x" = x s after arrival, "x" = absolute world time. GetInputSequenceLog
    // answers when each step ACTUALLY fired (start_error_ms, frame), for measuring the playback.
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString RunInputSequence(FString ScriptJson, FString StartAt, FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString GetInputSequenceLog(FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool ClearXRControllerOverride(EAgentXRHand Hand);
    // Opt-in aim-pose emulation (OFF by default) and the injected controllers' state. Same
    // contract as UUAPAgentSubsystem; both forward to FAgentMotionController.
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString SetXRAimEmulation(bool bEnabled, FVector GripToAimLocation, FRotator GripToAimRotation);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString GetXRControllerState();
    UFUNCTION(BlueprintCallable, Category="Agent|UI") FString DumpViewportUI(FString Client);
    UFUNCTION(BlueprintCallable, Category="Agent|Capture") bool CaptureViewportWithUI(FString Filename);
    UFUNCTION(BlueprintCallable, Category="Agent|Log") int64 GetLogCursor() const;
    UFUNCTION(BlueprintCallable, Category="Agent|Log") FString GetLogsSince(int64 AfterCursor, int32 MaxLines, FString CategoryFilter, EAgentLogVerbosity MinVerbosity) const;
    UFUNCTION(BlueprintCallable, Category="Agent|Helpers") TArray<FAgentHelperDescriptor> ListTestHelpers();
    // JSON twin of ListTestHelpers -- use this from an agent CLI. RemoteControl's preset-call
    // route strips nested struct fields, so the TArray return arrives as [{},{},...].
    UFUNCTION(BlueprintCallable, Category="Agent|Helpers") FString ListTestHelpersJson();
    UFUNCTION(BlueprintCallable, Category="Agent|Helpers") FString CallTestHelper(FString Name, FString JsonArgs);
    // Frame-rate property sampling (sub-second behaviour a ~1s exec round-trip cannot see).
    UFUNCTION(BlueprintCallable, Category="Agent|Sample") FString StartPropertySample(FString ObjectPath, FString PropertyPath, float Seconds, int32 MaxSamples);
    UFUNCTION(BlueprintCallable, Category="Agent|Sample") FString ReadPropertySample();
    UFUNCTION(BlueprintCallable, Category="Agent|Sample") bool StopPropertySample();

private:
    TSharedPtr<class FAgentLogCapture> LogCapture;
    TArray<FAgentHelperDescriptor> HelperCache;
    void RefreshHelperCache();
};
