#pragma once

#include "CoreMinimal.h"
#include "UObject/ObjectMacros.h"
#include "UAPAgentTypes.generated.h"

UENUM(BlueprintType)
enum class EAgentPIEPhase : uint8
{
    NotPlaying  UMETA(DisplayName="NotPlaying"),
    Starting    UMETA(DisplayName="Starting"),
    Playing     UMETA(DisplayName="Playing"),
    Paused      UMETA(DisplayName="Paused"),
    Ending      UMETA(DisplayName="Ending"),
};

UENUM(BlueprintType)
enum class EAgentLogVerbosity : uint8
{
    NoLogging   = 0,
    Fatal       = 1,
    Error       = 2,
    Warning     = 3,
    Display     = 4,
    Log         = 5,
    Verbose     = 6,
    VeryVerbose = 7,
};

UENUM(BlueprintType)
enum class EAgentMouseButton : uint8
{
    Left, Right, Middle, XButton1, XButton2,
};

UENUM(BlueprintType)
enum class EAgentGamepadButton : uint8
{
    FaceBottom, FaceRight, FaceLeft, FaceTop,
    ShoulderLeft, ShoulderRight,
    TriggerLeft, TriggerRight,
    ThumbLeft, ThumbRight,
    DPadUp, DPadDown, DPadLeft, DPadRight,
    Start, Back, Special,
    LeftStickX, LeftStickY, RightStickX, RightStickY,
};

USTRUCT(BlueprintType)
struct FAgentHelperDescriptor
{
    GENERATED_BODY()

    UPROPERTY(BlueprintReadOnly) FString Name;
    UPROPERTY(BlueprintReadOnly) FString Category;
    UPROPERTY(BlueprintReadOnly) FString Tooltip;
    UPROPERTY(BlueprintReadOnly) FString PhaseRequirement;
    UPROPERTY(BlueprintReadOnly) FString ArgSchemaJson;
    UPROPERTY(BlueprintReadOnly) FString ReturnSchemaJson;
    UPROPERTY(BlueprintReadOnly) bool    bSupported = true;
    UPROPERTY(BlueprintReadOnly) FString UnsupportedReason;
};

UENUM(BlueprintType)
enum class EAgentXRHand : uint8
{
    Left,
    Right,
};

USTRUCT(BlueprintType)
struct FAgentControllerPose
{
    GENERATED_BODY()

    UPROPERTY(BlueprintReadWrite) FVector  Position = FVector::ZeroVector;
    UPROPERTY(BlueprintReadWrite) FRotator Orientation = FRotator::ZeroRotator;
    UPROPERTY(BlueprintReadWrite) bool     bTracked = true;
};
