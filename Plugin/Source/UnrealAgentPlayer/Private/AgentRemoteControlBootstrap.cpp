#include "AgentRemoteControlBootstrap.h"

#include "UnrealAgentPlayerModule.h"
#include "UAPAgentSubsystem.h"
#include "Editor.h"
#include "RemoteControlPreset.h"
#include "Modules/ModuleManager.h"
#include "UObject/Package.h"

static URemoteControlPreset* GAgentPreset = nullptr;

void FAgentRemoteControlBootstrap::Startup()
{
    FCoreDelegates::OnPostEngineInit.AddStatic(&FAgentRemoteControlBootstrap::EnsureSubsystemExposed);
}

void FAgentRemoteControlBootstrap::Shutdown()
{
    if (GAgentPreset)
    {
        GAgentPreset->RemoveFromRoot();
        GAgentPreset = nullptr;
    }
}

void FAgentRemoteControlBootstrap::EnsureSubsystemExposed()
{
    if (!GEditor) { return; }

    UUAPAgentSubsystem* Subsystem = GEditor->GetEditorSubsystem<UUAPAgentSubsystem>();
    if (!Subsystem)
    {
        UE_LOG(LogUAP, Warning, TEXT("Could not find UAPAgentSubsystem to auto-expose."));
        return;
    }

    GAgentPreset = NewObject<URemoteControlPreset>(
        GetTransientPackage(),
        URemoteControlPreset::StaticClass(),
        TEXT("UAP_Preset"),
        RF_Transient
    );
    GAgentPreset->AddToRoot();

    UClass* Cls = Subsystem->GetClass();
    for (TFieldIterator<UFunction> It(Cls); It; ++It)
    {
        UFunction* Func = *It;
        if (Func->HasAnyFunctionFlags(FUNC_BlueprintCallable))
        {
            GAgentPreset->ExposeFunction(Subsystem, Func);
        }
    }

    UE_LOG(LogUAP, Log, TEXT("UAP_Preset created and subsystem functions exposed."));
}
