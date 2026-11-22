#include "UAPInputActionLibrary.h"

#include "AgentInput.h"
#include "AgentWorld.h"
#include "EnhancedInputSubsystems.h"
#include "InputAction.h"
#include "InputActionValue.h"
#include "Engine/LocalPlayer.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "Misc/PackageName.h"
#include "UnrealAgentPlayerRuntimeModule.h"

namespace
{
    FString UAPInputActionLibraryJson(bool bOk, const FString& Action, float Value, const FString& Error)
    {
        return FString::Printf(TEXT("{\"ok\":%s,\"action\":\"%s\",\"value\":%g,\"error\":\"%s\"}"),
            bOk ? TEXT("true") : TEXT("false"), *Action.ReplaceCharWithEscapedChar(), Value, *Error.ReplaceCharWithEscapedChar());
    }
}

FString UUAPInputActionLibrary::InjectInputAction(FString ActionPath, float Value, FString Client)
{
    FString Path = ActionPath;
    if (!Path.Contains(TEXT(".")))
    {
        Path += TEXT(".") + FPackageName::GetShortName(ActionPath);
    }
    const UInputAction* Action = LoadObject<UInputAction>(nullptr, *Path);
    if (!Action)
    {
        UE_LOG(LogUAPRuntime, Error, TEXT("InjectInputAction: no UInputAction at '%s'"), *Path);
        return UAPInputActionLibraryJson(false, ActionPath, Value, FString::Printf(TEXT("no UInputAction at %s"), *Path));
    }

    FString Failure;
    const bool bOk = FAgentInput::OnClients(TEXT("InjectInputAction"), Client, [&]() -> bool
    {
        UWorld* World = FAgentWorld::GetActiveGameWorld();
        APlayerController* PC = World ? World->GetFirstPlayerController() : nullptr;
        ULocalPlayer* LP = PC ? PC->GetLocalPlayer() : nullptr;
        UEnhancedInputLocalPlayerSubsystem* Sub = LP ? LP->GetSubsystem<UEnhancedInputLocalPlayerSubsystem>() : nullptr;
        if (!Sub)
        {
            Failure = TEXT("no EnhancedInputLocalPlayerSubsystem on the first local player (no live PIE?)");
            return false;
        }
        const FInputActionValue V = Action->ValueType == EInputActionValueType::Boolean
            ? FInputActionValue(Value > 0.5f) : FInputActionValue(Value);
        Sub->InjectInputForAction(Action, V, {}, {});
        return true;
    });
    if (!bOk && Failure.IsEmpty()) { Failure = TEXT("client selector refused or no live game world"); }
    return UAPInputActionLibraryJson(bOk, Action->GetPathName(), Value, Failure);
}
