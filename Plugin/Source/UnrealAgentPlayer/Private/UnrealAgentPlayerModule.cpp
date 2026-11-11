#include "UnrealAgentPlayerModule.h"
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
    // This module used to set SPI_SETFOREGROUNDLOCKTIMEOUT to 0 - with SPIF_UPDATEINIFILE, so a
    // PERSISTENT per-user Windows setting (HKCU\Control Panel\Desktop\ForegroundLockTimeout) -
    // so that FocusEditorWindow could steal the foreground. FocusEditorWindow is gone (uap never
    // activates a window: owner rule 2026-11-11) and the zero is worse than useless now: it
    // switches OFF the foreground lock that stops a background process, such as this editor
    // opening a PIE window on a remote request, from jumping in front of whatever the human is
    // using. So if an older build left it at 0, put back the Windows default (200000 ms) once.
    // Only the value this plugin itself wrote is touched; any other non-default setting is left.
#if PLATFORM_WINDOWS
    {
        DWORD Timeout = 1;
        if (::SystemParametersInfo(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, &Timeout, 0) && Timeout == 0)
        {
            const BOOL bRestored = ::SystemParametersInfo(SPI_SETFOREGROUNDLOCKTIMEOUT, 0,
                                       (PVOID)(UPTRINT)200000, SPIF_SENDWININICHANGE | SPIF_UPDATEINIFILE);
            UE_LOG(LogUAP, Log, TEXT("Windows foreground-lock timeout was 0 (an older uap build set it persistently); restored the default 200000 ms: %s"),
                   bRestored ? TEXT("ok") : TEXT("FAILED"));
        }
    }
#endif

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
    UE_LOG(LogUAP, Log, TEXT("UnrealAgentPlayer module shutdown."));
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FUnrealAgentPlayerModule, UnrealAgentPlayer)
