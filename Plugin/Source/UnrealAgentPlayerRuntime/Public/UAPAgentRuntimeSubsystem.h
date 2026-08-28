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

    UFUNCTION(BlueprintCallable, Category="Agent|Status") FString GetPluginVersion() const;
    UFUNCTION(BlueprintCallable, Category="Agent|Status") FString ExecuteConsoleCommand(FString Command);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectKey(FString KeyName, bool bPressed, bool bRepeat);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectMouseMove(float X, float Y, bool bAbsolute);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectMouseButton(EAgentMouseButton Button, bool bPressed);
    // SlateUser: "" (or omitted) resolves the target Slate user automatically; "0"/"1"/... targets
    // one explicitly and REFUSES LOUDLY if Slate has no such user. It is a string because
    // RemoteControl zero-initialises the argument struct: an omitted int32 would arrive as 0,
    // a valid index, and would silently move every existing call onto the Slate route.
    // Why it exists at all: Slate DISCARDS an event whose user index does not match the
    // handler's owner (FAnalogCursor::IsRelevantInput -- engine AnalogCursor.cpp:192), with no
    // error, so a mis-stamped injection looks exactly like a broken feature.
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectAxis(FString AxisName, float Value, FString SlateUser);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectGamepad(EAgentGamepadButton Button, bool bPressed, float AnalogValue, FString SlateUser);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectXRButton(EAgentXRHand Hand, FString ButtonKeyName, bool bPressed);
    // Sustained input: re-asserted every frame in-engine for Seconds, then released. A single
    // injected event cannot drive locomotion across a ~1s CLI round-trip, and HoldAxis is the
    // VR locomotion verb (thumbsticks are axis FKeys, not buttons). All return JSON so a
    // refusal says why; they validate before injecting, so a refusal has no side effects.
    // ReleaseHeldInput("") is the recovery hatch: it also flushes keys the registry lost
    // track of, so a stuck key never needs a PIE restart. See AgentInput.h.
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString HoldKey(FString KeyName, float Seconds);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString HoldAxis(FString AxisKeyName, float Value, float Seconds, FString SlateUser);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString ReleaseHeldInput(FString KeyName);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") FString GetHeldInput();
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool ClearXRControllerOverride(EAgentXRHand Hand);
    UFUNCTION(BlueprintCallable, Category="Agent|UI") FString DumpViewportUI();
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
