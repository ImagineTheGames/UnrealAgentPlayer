#include "AgentRemoteControlBootstrap.h"

#include "UnrealAgentPlayerModule.h"
#include "UAPAgentSubsystem.h"
#include "RemoteControlPreset.h"
#include "IRemoteControlModule.h"
#include "Modules/ModuleManager.h"
#include "UObject/Package.h"

static URemoteControlPreset* GAgentPreset = nullptr;

void FAgentRemoteControlBootstrap::Expose(UUAPAgentSubsystem* Subsystem)
{
    if (!Subsystem || GAgentPreset) { return; }

    // Skip during headless commandlets (e.g. the cook commandlet). There is no RemoteControl
    // HTTP server to expose to, and ExposeFunction below calls URemoteControlPreset::ExposeFunction
    // -> Registry->Modify(), which routes through the editor transaction buffer. That buffer is
    // null in a commandlet, so the cast inside the transaction system fatals ("Cast of nullptr to
    // TransBuffer failed") and fails the cook.
    if (IsRunningCommandlet()) { return; }

    GAgentPreset = NewObject<URemoteControlPreset>(
        GetTransientPackage(),
        URemoteControlPreset::StaticClass(),
        TEXT("UAP_Preset"),
        RF_Transient);
    GAgentPreset->AddToRoot();

    for (TFieldIterator<UFunction> It(Subsystem->GetClass()); It; ++It)
    {
        UFunction* Func = *It;
        if (Func->HasAnyFunctionFlags(FUNC_BlueprintCallable))
        {
            // Pin the exposed label to the exact C++ function name so the call path is a
            // stable /remote/preset/UAP_Preset/function/<FunctionName> (otherwise RC derives
            // a display-name label clients would have to guess).
            GAgentPreset->ExposeFunction(Subsystem, Func,
                FRemoteControlPresetExposeArgs(Func->GetName(), FGuid()));
        }
    }

    // Register the preset with the RemoteControl module. Without this, ResolvePreset()
    // returns null and every /remote/preset/UAP_Preset/... HTTP route 404s. Driven from the
    // subsystem's Initialize (not OnPostEngineInit, which fires before this PostEngineInit-
    // phase module binds it) so it actually runs. Bound to the live subsystem instance, so
    // stateful functions (e.g. log capture) work too.
    IRemoteControlModule::Get().RegisterEmbeddedPreset(GAgentPreset, /*bReplaceExisting=*/true);

    UE_LOG(LogUAP, Log, TEXT("UAP_Preset registered; call via /remote/preset/UAP_Preset/function/<Name>."));
}

void FAgentRemoteControlBootstrap::Unexpose()
{
    if (GAgentPreset)
    {
        if (FModuleManager::Get().IsModuleLoaded(TEXT("RemoteControl")))
        {
            IRemoteControlModule::Get().UnregisterEmbeddedPreset(GAgentPreset);
        }
        GAgentPreset->RemoveFromRoot();
        GAgentPreset = nullptr;
    }
}
