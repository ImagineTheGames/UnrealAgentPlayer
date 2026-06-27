#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentMotionController.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "RemoteControlSettings.h"
#include "UObject/UnrealType.h"
#include "Misc/App.h"
#include "Misc/Crc.h"
#if WITH_EDITOR
#include "SocketSubsystem.h"
#include "Sockets.h"
#include "IPAddress.h"
#endif

DEFINE_LOG_CATEGORY(LogUAPRuntime);
#define LOCTEXT_NAMESPACE "FUnrealAgentPlayerRuntimeModule"

#if WITH_EDITOR
namespace
{
    // Push a new RC HTTP port into the settings and broadcast the change so WebRemoteControl
    // rebinds its server to it.
    void ApplyRCPort(int32 Port)
    {
        if (URemoteControlSettings* RCSettings = GetMutableDefault<URemoteControlSettings>())
        {
            RCSettings->RemoteControlHttpServerPort = (uint32)Port;
            FProperty* Prop = URemoteControlSettings::StaticClass()->FindPropertyByName(
                TEXT("RemoteControlHttpServerPort"));
            FPropertyChangedEvent Evt(Prop);
            RCSettings->OnSettingChanged().Broadcast(RCSettings, Evt);
        }
    }
}
#endif

void FUnrealAgentPlayerRuntimeModule::StartupModule()
{
    UE_LOG(LogUAPRuntime, Log, TEXT("UnrealAgentPlayerRuntime started."));
    MotionController = MakeShared<FAgentMotionController>();
    MotionController->Register();

#if WITH_EDITOR
    // RemoteControl HTTP port management so several editors (and -game instances) can each
    // run RemoteControl at once -- required for parallel agent testing. The default port
    // (30010) is a single bind: the first process gets it, later ones fail and have NO RC.
    //
    // When our bind fails because another editor holds the default, move THIS editor to a
    // free port and rebind. A successful bind is never disturbed (IsHttpServerRunning guard),
    // so the first editor keeps 30010. An explicit -UAPRCPort=N still forces a specific port.
    int32 RCPort = 0;
    if (FParse::Value(FCommandLine::Get(), TEXT("UAPRCPort="), RCPort) && RCPort > 0)
    {
        ApplyRCPort(RCPort);
        UE_LOG(LogUAPRuntime, Log, TEXT("Agent RC HTTP port set to %d via -UAPRCPort."), RCPort);
    }
    else
    {
        // Defer until WebRemoteControl is loaded and has finished its initial bind attempt,
        // then move only if it failed. A one-shot ticker (~2s) is robust to module/init order.
        RCPortTickerHandle = FTSTicker::GetCoreTicker().AddTicker(
            FTickerDelegate::CreateLambda([this](float) { EnsureRCPortBound(); return false; }), 2.0f);
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
#if WITH_EDITOR
    if (RCPortTickerHandle.IsValid())
    {
        FTSTicker::GetCoreTicker().RemoveTicker(RCPortTickerHandle);
        RCPortTickerHandle.Reset();
    }
#endif
}

#if WITH_EDITOR
void FUnrealAgentPlayerRuntimeModule::EnsureRCPortBound()
{
    RCPortTickerHandle.Reset();

    // If a non-default RC port is already configured (pinned per-project in
    // Config/DefaultRemoteControl.ini under [/Script/RemoteControlCommon.RemoteControlSettings])
    // AND WebRemoteControl already bound it, leave it ALONE -- do not rebind.
    //
    // Why: a rebind goes through OnSettingChanged, and the engine's HTTP server starts the new
    // listener WITHOUT releasing the old one (it caches listeners per port). So ANY rebind leaves
    // the editor serving on TWO ports; the leftover one floats between editors by boot order and
    // cross-targets agent commands. Pinning the port in config makes WebRemoteControl bind the
    // right port at startup (never the default 30010), and respecting that bind here keeps exactly
    // ONE port per editor -- the actual fix for the multi-bind tangle. We only auto-assign below
    // when still on the default port (i.e. no per-project pin).
    if (const URemoteControlSettings* RCSettings = GetDefault<URemoteControlSettings>())
    {
        const int32 Configured = (int32)RCSettings->RemoteControlHttpServerPort;
        if (Configured != 30010 && ProbeFreePort(Configured, Configured) != Configured)
        {
            UE_LOG(LogUAPRuntime, Log,
                TEXT("RC HTTP port %d already bound from config; leaving it (no rebind)."), Configured);
            return;
        }
    }

    // We cannot trust IWebRemoteControlModule::IsHttpServerRunning() -- it returns true even
    // when the socket bind failed because another editor holds the port. So instead of trying
    // to DETECT a conflict, we AVOID one: pick a deterministic base port per project (different
    // projects never collide) and probe upward for the first genuinely free TCP port, then
    // rebind RC to it. A socket bind test is reliable where IsHttpServerRunning is not.
    const FString Proj = FApp::GetProjectName();
    const uint32 Hash = FCrc::StrCrc32(*Proj);
    const int32 Base = 30011 + (int32)(Hash % 80);   // 30011..30090, never the default 30010
    int32 Port = ProbeFreePort(Base, 30099);
    if (Port <= 0)
    {
        Port = ProbeFreePort(30011, Base - 1);       // wrap to cover the whole range
    }
    if (Port <= 0)
    {
        UE_LOG(LogUAPRuntime, Warning, TEXT("No free RC HTTP port found in 30011-30099."));
        return;
    }
    ApplyRCPort(Port);
    UE_LOG(LogUAPRuntime, Log,
        TEXT("Assigned RC HTTP port %d (base %d for project '%s')."), Port, Base, *Proj);
}

int32 FUnrealAgentPlayerRuntimeModule::ProbeFreePort(int32 Start, int32 End)
{
    ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
    if (!SS)
    {
        return 0;
    }
    for (int32 P = Start; P <= End; ++P)
    {
        TSharedRef<FInternetAddr> Addr = SS->CreateInternetAddr();
        bool bValidIp = false;
        Addr->SetIp(TEXT("127.0.0.1"), bValidIp);
        Addr->SetPort(P);
        FSocket* Sock = SS->CreateSocket(NAME_Stream, TEXT("UAPPortProbe"), Addr->GetProtocolType());
        if (!Sock)
        {
            continue;
        }
        Sock->SetReuseAddr(false);
        const bool bBound = Sock->Bind(*Addr);
        Sock->Close();
        SS->DestroySocket(Sock);
        if (bBound)
        {
            return P;
        }
    }
    return 0;
}
#endif // WITH_EDITOR

int32 FUnrealAgentPlayerRuntimeModule::GetConfiguredRCPort()
{
    const URemoteControlSettings* S = GetDefault<URemoteControlSettings>();
    if (!S)
    {
        return 0;
    }
    const int32 Port = (int32)S->RemoteControlHttpServerPort;
#if WITH_EDITOR
    // Reliable bound-check (IsHttpServerRunning lies -- it stays true even on a failed bind).
    // If we can still bind this port, our RC is NOT serving on it -> report 0; if the bind
    // fails, the port is held (by our own RC) -> report it. So an agent CLI never drives the
    // wrong editor.
    if (ProbeFreePort(Port, Port) == Port)
    {
        return 0;
    }
#endif
    return Port;
}

FUnrealAgentPlayerRuntimeModule* FUnrealAgentPlayerRuntimeModule::Get()
{
    return FModuleManager::GetModulePtr<FUnrealAgentPlayerRuntimeModule>("UnrealAgentPlayerRuntime");
}

#undef LOCTEXT_NAMESPACE
IMPLEMENT_MODULE(FUnrealAgentPlayerRuntimeModule, UnrealAgentPlayerRuntime)
