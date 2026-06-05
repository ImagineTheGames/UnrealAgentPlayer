#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "LoopbackTestHelpers.generated.h"

UCLASS()
class ULoopbackTestHelpers : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    UFUNCTION(BlueprintCallable, Category="Agent|Loopback",
              meta=(AgentTestHelper, Phase="Playing",
                    ToolTip="Always returns true. Smoke-test helper for the bridge."))
    static bool PingPong();

    UFUNCTION(BlueprintCallable, Category="Agent|Loopback",
              meta=(AgentTestHelper, Phase="Any",
                    ToolTip="Echoes back the integer you pass."))
    static int32 Echo(int32 Value);
};
