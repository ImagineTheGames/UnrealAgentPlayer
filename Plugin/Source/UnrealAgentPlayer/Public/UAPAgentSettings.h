#pragma once

#include "CoreMinimal.h"
#include "Engine/DeveloperSettings.h"
#include "UAPAgentSettings.generated.h"

UCLASS(config=Editor, defaultconfig, meta=(DisplayName="Unreal Agent Player"))
class UNREALAGENTPLAYER_API UUAPAgentSettings : public UDeveloperSettings
{
    GENERATED_BODY()

public:
    virtual FName GetCategoryName() const override { return TEXT("Plugins"); }

    UPROPERTY(config, EditAnywhere, Category="Log Capture",
              meta=(ClampMin="256", ClampMax="65536"))
    int32 LogBufferCapacity = 4096;

    UPROPERTY(config, EditAnywhere, Category="Log Capture")
    bool bCaptureVerboseAndBelow = false;
};
