#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentMotionController.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "RemoteControlSettings.h"
#include "UObject/UnrealType.h"

DEFINE_LOG_CATEGORY(LogUAPRuntime);
#define LOCTEXT_NAMESPACE "FUnrealAgentPlayerRuntimeModule"

void FUnrealAgentPlayerRuntimeModule::StartupModule()
{
    UE_LOG(LogUAPRuntime, Log, TEXT("UnrealAgentPlayerRuntime started."));
    MotionController = MakeShared<FAgentMotionController>();
    MotionController->Register();

#if WITH_EDITOR
    // Per-instance RemoteControl HTTP port for standalone -game processes. The default
    // port (30010) collides with the editor and with other instances; -UAPRCPort=N moves
    // THIS instance's RC HTTP server to N so several instances + the editor can each run
    // RemoteControl at once (required for parallel agent testing). This runs in the module
    // StartupModule (which always executes), AFTER WebRemoteControl tried/failed to bind
    // the default port; broadcasting the settings-change makes WebRemoteControl rebind to
    // N. (Done here, not in the GameInstance subsystem, which is collected too late in a
    // fresh standalone.) WITH_EDITOR because OnSettingChanged + the rebind handler are
    // editor-build only; -game launched from UnrealEditor.exe is WITH_EDITOR.
    int32 RCPort = 0;
    if (FParse::Value(FCommandLine::Get(), TEXT("UAPRCPort="), RCPort) && RCPort > 0)
    {
        if (URemoteControlSettings* RCSettings = GetMutableDefault<URemoteControlSettings>())
        {
            RCSettings->RemoteControlHttpServerPort = (uint32)RCPort;
            FProperty* Prop = URemoteControlSettings::StaticClass()->FindPropertyByName(TEXT("RemoteControlHttpServerPort"));
            FPropertyChangedEvent Evt(Prop);
            RCSettings->OnSettingChanged().Broadcast(RCSettings, Evt);
            UE_LOG(LogUAPRuntime, Log, TEXT("Agent RC HTTP port set to %d via -UAPRCPort."), RCPort);
        }
    }
#endif
}

void FUnrealAgentPlayerRuntimeModule::ShutdownModule()
{
    if (MotionController.IsValid())
    {
        MotionController->Unregister();
        MotionController.Reset();
    }
}

FUnrealAgentPlayerRuntimeModule* FUnrealAgentPlayerRuntimeModule::Get()
{
    return FModuleManager::GetModulePtr<FUnrealAgentPlayerRuntimeModule>("UnrealAgentPlayerRuntime");
}

#undef LOCTEXT_NAMESPACE
IMPLEMENT_MODULE(FUnrealAgentPlayerRuntimeModule, UnrealAgentPlayerRuntime)
