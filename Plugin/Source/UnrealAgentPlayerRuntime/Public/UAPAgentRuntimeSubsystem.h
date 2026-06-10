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
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectAxis(FString AxisName, float Value);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectGamepad(EAgentGamepadButton Button, bool bPressed, float AnalogValue);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectXRButton(EAgentXRHand Hand, FString ButtonKeyName, bool bPressed);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked);
    UFUNCTION(BlueprintCallable, Category="Agent|Input") bool ClearXRControllerOverride(EAgentXRHand Hand);
    UFUNCTION(BlueprintCallable, Category="Agent|UI") FString DumpViewportUI();
    UFUNCTION(BlueprintCallable, Category="Agent|Capture") bool CaptureViewportWithUI(FString Filename);
    UFUNCTION(BlueprintCallable, Category="Agent|Log") int64 GetLogCursor() const;
    UFUNCTION(BlueprintCallable, Category="Agent|Log") FString GetLogsSince(int64 AfterCursor, int32 MaxLines, FString CategoryFilter, EAgentLogVerbosity MinVerbosity) const;
    UFUNCTION(BlueprintCallable, Category="Agent|Helpers") TArray<FAgentHelperDescriptor> ListTestHelpers();
    UFUNCTION(BlueprintCallable, Category="Agent|Helpers") FString CallTestHelper(FString Name, FString JsonArgs);

private:
    TSharedPtr<class FAgentLogCapture> LogCapture;
    TArray<FAgentHelperDescriptor> HelperCache;
    void RefreshHelperCache();
};
