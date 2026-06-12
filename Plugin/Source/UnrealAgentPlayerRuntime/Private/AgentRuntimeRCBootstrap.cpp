#include "AgentRuntimeRCBootstrap.h"
#include "UAPAgentRuntimeSubsystem.h"
#include "RemoteControlPreset.h"
#include "IRemoteControlModule.h"
#include "Modules/ModuleManager.h"
#include "UObject/Package.h"
#include "UObject/UObjectGlobals.h"
#include "UObject/UnrealType.h"

static URemoteControlPreset* GRuntimePreset = nullptr;

void FAgentRuntimeRCBootstrap::Expose(UUAPAgentRuntimeSubsystem* Subsystem)
{
    if (!Subsystem || GRuntimePreset) { return; }
    // No RemoteControl server (and a null editor transaction buffer) during headless commandlets
    // like cook; ExposeFunction -> Modify() would fatal. RC has no purpose there. See the editor
    // bootstrap for the full explanation.
    if (IsRunningCommandlet()) { return; }
    GRuntimePreset = NewObject<URemoteControlPreset>(GetTransientPackage(),
        URemoteControlPreset::StaticClass(), TEXT("UAP_RuntimePreset"), RF_Transient);
    GRuntimePreset->AddToRoot();
    for (TFieldIterator<UFunction> It(Subsystem->GetClass()); It; ++It)
    {
        if (It->HasAnyFunctionFlags(FUNC_BlueprintCallable))
        {
            // Stable label = exact function name -> /remote/preset/UAP_RuntimePreset/function/<Name>.
            GRuntimePreset->ExposeFunction(Subsystem, *It,
                FRemoteControlPresetExposeArgs(It->GetName(), FGuid()));
        }
    }

    // Register so the /remote/preset/UAP_RuntimePreset routes resolve (works in PIE and
    // standalone, bound to the live runtime subsystem). See AgentRemoteControlBootstrap
    // for why this registration is required.
    IRemoteControlModule::Get().RegisterEmbeddedPreset(GRuntimePreset, /*bReplaceExisting=*/true);
}

void FAgentRuntimeRCBootstrap::Unexpose()
{
    if (GRuntimePreset)
    {
        if (FModuleManager::Get().IsModuleLoaded(TEXT("RemoteControl")))
        {
            IRemoteControlModule::Get().UnregisterEmbeddedPreset(GRuntimePreset);
        }
        GRuntimePreset->RemoveFromRoot();
        GRuntimePreset = nullptr;
    }
}
