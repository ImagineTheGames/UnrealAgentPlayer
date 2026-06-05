#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "USchoolsOutTestHelpers.generated.h"

UCLASS()
class USchoolsOutTestHelpers : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()

public:
    UFUNCTION(BlueprintCallable, Category="Agent|Player",
              meta=(AgentTestHelper, Phase="Playing"))
    static FVector GetPlayerLocation();

    UFUNCTION(BlueprintCallable, Category="Agent|Player",
              meta=(AgentTestHelper, Phase="Playing"))
    static float GetPlayerHealth();

    UFUNCTION(BlueprintCallable, Category="Agent|Doors",
              meta=(AgentTestHelper, Phase="Playing",
                    ToolTip="Returns true if the door with matching Tag is fully open."))
    static bool IsDoorOpen(FName DoorTag);

    UFUNCTION(BlueprintCallable, Category="Agent|Progression",
              meta=(AgentTestHelper, Phase="Any"))
    static bool HasCompletedTutorial();
};
