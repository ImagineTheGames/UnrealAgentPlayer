#pragma once
#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "UAPInputActionLibrary.generated.h"

/**
 * Drives an Enhanced Input action the way an OpenXR controller does: UEnhancedInputLocalPlayerSubsystem::
 * InjectInputForAction on the local player. Key injection (InjectKey) stops at the PlayerController's key
 * mappings; a game whose logic hangs off an action (the VR pawn's jump stick) cannot be reached that way.
 * Called through Remote Control on the CDO: /Script/UnrealAgentPlayerRuntime.Default__UAPInputActionLibrary.
 */
UCLASS()
class UNREALAGENTPLAYERRUNTIME_API UUAPInputActionLibrary : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    /**
     * ActionPath: object path of the UInputAction ("/Game/VRE/Input/Actions/MotionControllerThumbRight_Y"
     * or the full "Pkg.Name" form). Value: the action value (bool actions read it as value > 0.5).
     * Client: "" = first game world, "N" = PIE instance N, "all" = every client (same as the key verbs).
     * Returns a JSON string {ok, action, value, error}.
     */
    UFUNCTION(BlueprintCallable, Category="Agent|Input")
    static FString InjectInputAction(FString ActionPath, float Value, FString Client);
};
