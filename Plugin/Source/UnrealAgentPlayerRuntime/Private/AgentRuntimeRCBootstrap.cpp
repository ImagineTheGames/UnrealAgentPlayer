#include "AgentRuntimeRCBootstrap.h"
#include "UAPAgentRuntimeSubsystem.h"
#include "RemoteControlPreset.h"
#include "UObject/Package.h"
#include "UObject/UObjectGlobals.h"
#include "UObject/UnrealType.h"

static URemoteControlPreset* GRuntimePreset = nullptr;

void FAgentRuntimeRCBootstrap::Expose(UUAPAgentRuntimeSubsystem* Subsystem)
{
    if (!Subsystem || GRuntimePreset) { return; }
    GRuntimePreset = NewObject<URemoteControlPreset>(GetTransientPackage(),
        URemoteControlPreset::StaticClass(), TEXT("UAP_RuntimePreset"), RF_Transient);
    GRuntimePreset->AddToRoot();
    for (TFieldIterator<UFunction> It(Subsystem->GetClass()); It; ++It)
    {
        if (It->HasAnyFunctionFlags(FUNC_BlueprintCallable))
        {
            GRuntimePreset->ExposeFunction(Subsystem, *It);
        }
    }
}

void FAgentRuntimeRCBootstrap::Unexpose()
{
    if (GRuntimePreset) { GRuntimePreset->RemoveFromRoot(); GRuntimePreset = nullptr; }
}
