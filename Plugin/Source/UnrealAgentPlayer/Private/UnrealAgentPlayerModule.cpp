#include "UnrealAgentPlayerModule.h"
#include "AgentRemoteControlBootstrap.h"
#include "AgentMotionController.h"
#include "AgentHelperBlueprintDetails.h"
#include "PropertyEditorModule.h"
#include "K2Node_FunctionEntry.h"
#if PLATFORM_WINDOWS
#include "Windows/AllowWindowsPlatformTypes.h"
#include <Windows.h>
#include "Windows/HideWindowsPlatformTypes.h"
#endif

DEFINE_LOG_CATEGORY(LogUAP);

#define LOCTEXT_NAMESPACE "FUnrealAgentPlayerModule"

void FUnrealAgentPlayerModule::StartupModule()
{
    UE_LOG(LogUAP, Log, TEXT("UnrealAgentPlayer module started."));
#if PLATFORM_WINDOWS
    // Drop the foreground-lock timeout so FocusEditorWindow's SetForegroundWindow is
    // honored even when another app currently holds focus. Lets an external agent raise
    // the editor for menu-bar / native-chrome driving (the in-process key/mouse injection
    // path does not need this). Editor-only tool, so changing the per-session setting is fine.
    ::SystemParametersInfo(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, (PVOID)0,
                           SPIF_SENDWININICHANGE | SPIF_UPDATEINIFILE);
#endif
    FAgentRemoteControlBootstrap::Startup();
    MotionController = MakeShared<FAgentMotionController>();
    MotionController->Register();

    FPropertyEditorModule& PropEd = FModuleManager::LoadModuleChecked<FPropertyEditorModule>("PropertyEditor");
    PropEd.RegisterCustomClassLayout(
        UK2Node_FunctionEntry::StaticClass()->GetFName(),
        FOnGetDetailCustomizationInstance::CreateStatic(&FAgentHelperBlueprintDetails::MakeInstance));
}

void FUnrealAgentPlayerModule::ShutdownModule()
{
    if (FModuleManager::Get().IsModuleLoaded("PropertyEditor"))
    {
        FPropertyEditorModule& PropEd = FModuleManager::GetModuleChecked<FPropertyEditorModule>("PropertyEditor");
        PropEd.UnregisterCustomClassLayout(UK2Node_FunctionEntry::StaticClass()->GetFName());
    }
    if (MotionController.IsValid())
    {
        MotionController->Unregister();
        MotionController.Reset();
    }
    FAgentRemoteControlBootstrap::Shutdown();
    UE_LOG(LogUAP, Log, TEXT("UnrealAgentPlayer module shutdown."));
}

FAgentMotionController* FUnrealAgentPlayerModule::GetMotionController()
{
    FUnrealAgentPlayerModule* Mod = FModuleManager::GetModulePtr<FUnrealAgentPlayerModule>("UnrealAgentPlayer");
    return Mod ? Mod->MotionController.Get() : nullptr;
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FUnrealAgentPlayerModule, UnrealAgentPlayer)
