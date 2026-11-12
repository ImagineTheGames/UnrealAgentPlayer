#include "UAPAgentSubsystem.h"

#include "UnrealAgentPlayerModule.h"
#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentRemoteControlBootstrap.h"
#include "Editor.h"
#include "LevelEditor.h"
#include "ILevelEditor.h"
#include "SLevelViewport.h"
#include "LevelEditorViewport.h"
#include "UnrealEngine.h"
#include "HAL/FileManager.h"
#include "Modules/ModuleManager.h"
#include "LevelEditorSubsystem.h"
#include "PlayInEditorDataTypes.h"
#include "HAL/PlatformTime.h"
#include "DynamicRHI.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "Kismet/KismetSystemLibrary.h"
#include "Misc/OutputDevice.h"
#include "AgentActivity.h"
#include "AgentInput.h"
#include "AgentWorld.h"
#include "Editor/EditorPerformanceSettings.h"
#include "AgentLogCapture.h"
#include "Misc/Paths.h"
#include "UAPAgentSettings.h"
#include "AgentHelperDiscovery.h"
#include "AgentSampler.h"
#include "IXRTrackingSystem.h"
#include "IHeadMountedDisplay.h"
#include "JsonObjectConverter.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "RHIGlobals.h"
#include "RenderTimer.h"
#include "AgentMotionController.h"
#include "AgentUIReader.h"
#include "CommonTabListWidgetBase.h"
#include "UnrealClient.h"
#include "HAL/IConsoleManager.h"
#include "Framework/Application/SlateApplication.h"
#include "Widgets/SWindow.h"
#include "Widgets/SViewport.h"
#include "Engine/GameViewportClient.h"
#include "ImageUtils.h"
#include "GenericPlatform/GenericWindow.h"
#include "IPythonScriptPlugin.h"
#include "PythonScriptTypes.h"
#if PLATFORM_WINDOWS
#include "Windows/AllowWindowsPlatformTypes.h"
#include <Windows.h>
#include "Windows/HideWindowsPlatformTypes.h"
#endif

namespace
{
    class FUAPStringOutputDevice : public FOutputDevice
    {
    public:
        FString Buffer;
        virtual void Serialize(const TCHAR* V, ELogVerbosity::Type, const FName&) override
        {
            Buffer += V;
        }
        virtual bool CanBeUsedOnAnyThread() const override { return true; }
    };
}

void UUAPAgentSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
    Super::Initialize(Collection);
    HPreBeginPIE   = FEditorDelegates::PreBeginPIE.AddUObject(this, &UUAPAgentSubsystem::OnPreBeginPIE);
    HPostPIEStarted = FEditorDelegates::PostPIEStarted.AddUObject(this, &UUAPAgentSubsystem::OnPostPIEStarted);
    HPrePIEEnded   = FEditorDelegates::PrePIEEnded.AddUObject(this, &UUAPAgentSubsystem::OnPrePIEEnded);
    HEndPIE        = FEditorDelegates::EndPIE.AddUObject(this, &UUAPAgentSubsystem::OnEndPIE);
    HPausePIE      = FEditorDelegates::PausePIE.AddUObject(this, &UUAPAgentSubsystem::OnPausePIE);
    HResumePIE     = FEditorDelegates::ResumePIE.AddUObject(this, &UUAPAgentSubsystem::OnResumePIE);
    HCancelPIE     = FEditorDelegates::CancelPIE.AddUObject(this, &UUAPAgentSubsystem::OnCancelPIE);
    HPreMapLoad    = FEditorDelegates::OnMapLoad.AddUObject(this, &UUAPAgentSubsystem::OnPreMapLoad);
    const UUAPAgentSettings* Settings = GetDefault<UUAPAgentSettings>();
    LogCapture = MakeShared<FAgentLogCapture>(Settings ? Settings->LogBufferCapacity : 4096);
    GLog->AddOutputDevice(LogCapture.Get());

    // Expose this subsystem over RemoteControl from Initialize (reliably runs) rather than
    // an OnPostEngineInit module delegate (binds too late in a PostEngineInit-phase module
    // and never fires). Mirrors the runtime subsystem.
    FAgentRemoteControlBootstrap::Expose(this);

    UE_LOG(LogUAP, Log, TEXT("UAPAgentSubsystem initialized."));
}

void UUAPAgentSubsystem::Deinitialize()
{
    SuppressBackgroundThrottle(false);
    FAgentRemoteControlBootstrap::Unexpose();
    if (LogCapture.IsValid())
    {
        GLog->RemoveOutputDevice(LogCapture.Get());
        LogCapture.Reset();
    }
    FEditorDelegates::PreBeginPIE.Remove(HPreBeginPIE);
    FEditorDelegates::PostPIEStarted.Remove(HPostPIEStarted);
    FEditorDelegates::PrePIEEnded.Remove(HPrePIEEnded);
    FEditorDelegates::EndPIE.Remove(HEndPIE);
    FEditorDelegates::PausePIE.Remove(HPausePIE);
    FEditorDelegates::ResumePIE.Remove(HResumePIE);
    FEditorDelegates::CancelPIE.Remove(HCancelPIE);
    FEditorDelegates::OnMapLoad.Remove(HPreMapLoad);
    UE_LOG(LogUAP, Log, TEXT("UAPAgentSubsystem deinitialized."));
    Super::Deinitialize();
}

FString UUAPAgentSubsystem::GetPluginVersion() const
{
    return TEXT("0.0.1");
}

int32 UUAPAgentSubsystem::GetRemoteControlPort() const
{
    return FUnrealAgentPlayerRuntimeModule::GetConfiguredRCPort();
}

FString UUAPAgentSubsystem::ExecuteConsoleCommand(FString Command)
{
    UAP_ACTIVITY(TEXT("console"), Command);
    if (!GEditor)
    {
        return TEXT("ERROR: GEditor not available");
    }
    UWorld* World = GEditor->GetEditorWorldContext().World();
    if (!World)
    {
        return TEXT("ERROR: No editor world");
    }
    FUAPStringOutputDevice Ar;
    GEngine->Exec(World, *Command, Ar);
    return Ar.Buffer;
}

// The editor drops to exactly 3.0 fps whenever it is not the OS-foreground window
// (UEditorPerformanceSettings::bThrottleCPUWhenNotForeground), which voids every timing taken
// then. The old cure was to take focus - raising the editor window over whatever the human at
// the machine was doing. Owner rule 2026-11-11: uap never activates a window. So the setting
// itself is turned off for the length of a PIE session and put back when it ends. In memory only
// (no SaveConfig), so nothing is left changed on disk, and a crashed editor comes back with the
// ini value. Nothing here needs, or takes, focus.
void UUAPAgentSubsystem::SuppressBackgroundThrottle(bool bSuppress)
{
    UEditorPerformanceSettings* Settings = GetMutableDefault<UEditorPerformanceSettings>();
    if (!Settings) { return; }
    if (bSuppress)
    {
        if (bThrottleSuppressed) { return; }
        bPriorThrottleSetting = Settings->bThrottleCPUWhenNotForeground;
        Settings->bThrottleCPUWhenNotForeground = false;
        bThrottleSuppressed = true;
        UE_LOG(LogUAP, Log, TEXT("bThrottleCPUWhenNotForeground off for the PIE session (was %d); restored when PIE ends."),
               bPriorThrottleSetting ? 1 : 0);
    }
    else if (bThrottleSuppressed)
    {
        Settings->bThrottleCPUWhenNotForeground = bPriorThrottleSetting;
        bThrottleSuppressed = false;
        UE_LOG(LogUAP, Log, TEXT("bThrottleCPUWhenNotForeground restored to %d."), bPriorThrottleSetting ? 1 : 0);
    }
}

void UUAPAgentSubsystem::OnPreBeginPIE(bool)    { SuppressBackgroundThrottle(true); }
void UUAPAgentSubsystem::OnPostPIEStarted(bool) { SuppressBackgroundThrottle(true); CurrentPhase = EAgentPIEPhase::Playing; PIEStartTimeSeconds = FPlatformTime::Seconds(); }
void UUAPAgentSubsystem::OnPrePIEEnded(bool)    { CurrentPhase = EAgentPIEPhase::Ending; }
void UUAPAgentSubsystem::OnEndPIE(bool)         { SuppressBackgroundThrottle(false); CurrentPhase = EAgentPIEPhase::NotPlaying; PIEStartTimeSeconds = 0.0; }
void UUAPAgentSubsystem::OnPausePIE(bool)       { CurrentPhase = EAgentPIEPhase::Paused; }
void UUAPAgentSubsystem::OnResumePIE(bool)      { CurrentPhase = EAgentPIEPhase::Playing; }
void UUAPAgentSubsystem::OnCancelPIE()          { SuppressBackgroundThrottle(false); CurrentPhase = EAgentPIEPhase::NotPlaying; PIEStartTimeSeconds = 0.0; }

EAgentPIEPhase UUAPAgentSubsystem::GetPIEPhase() const { return CurrentPhase; }

double UUAPAgentSubsystem::GetPIEElapsedSeconds() const
{
    if (PIEStartTimeSeconds <= 0.0) { return 0.0; }
    return FPlatformTime::Seconds() - PIEStartTimeSeconds;
}

bool UUAPAgentSubsystem::StartPIE()
{
    UAP_ACTIVITY(TEXT("pie start"), FString());
    if (!GEditor) { return false; }
    ULevelEditorSubsystem* LES = GEditor->GetEditorSubsystem<ULevelEditorSubsystem>();
    if (LES && LES->IsInPlayInEditor()) { return true; }

    // Launch PIE in its OWN floating window. We deliberately leave DestinationSlateViewport
    // UNSET: that makes the editor spawn a standalone top-level PIE window instead of playing
    // embedded inside a level-viewport tab. A top-level window can't be hidden behind an
    // asset-editor tab, so screenshot capture of the game viewport stays reliable even when
    // an agent opens a Blueprint/asset editor mid-test. (Embedded PIE shares the main editor
    // window; opening a WBP there occludes the viewport tab -> the capture would grab the
    // Blueprint editor instead of the game. That was the fumble we are eliminating.)
    FRequestPlaySessionParams Params;
    Params.SessionDestination = EPlaySessionDestinationType::InProcess;
    Params.WorldType = EPlaySessionWorldType::PlayInEditor;
    // (DestinationSlateViewport intentionally not set -> new PIE window.)
    GEditor->RequestPlaySession(Params);   // queued; the editor tick starts it. Caller polls IsInPIE().
    return true;
}

FString UUAPAgentSubsystem::StartPIEMode(FString Mode)
{
    UAP_ACTIVITY(TEXT("pie start"), Mode);
    auto MakeResult = [](bool bOk, const FString& ModeName, const FString& Error)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("ok"), bOk);
        O->SetStringField(TEXT("mode"), ModeName);
        if (!Error.IsEmpty()) { O->SetStringField(TEXT("error"), Error); }
        FString Out;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(O, W);
        return Out;
    };

    const FString Wanted = Mode.IsEmpty() ? TEXT("flat") : Mode.ToLower();
    if (Wanted != TEXT("flat") && Wanted != TEXT("vr"))
    {
        return MakeResult(false, Wanted, TEXT("mode must be 'flat' or 'vr'"));
    }
    if (!GEditor) { return MakeResult(false, Wanted, TEXT("no GEditor")); }

    ULevelEditorSubsystem* LES = GEditor->GetEditorSubsystem<ULevelEditorSubsystem>();
    if (LES && LES->IsInPlayInEditor())
    {
        // Already playing: the play mode cannot be changed without stopping first. Say so
        // rather than reporting success for a session that may be the wrong mode.
        return MakeResult(true, Wanted, TEXT(""));
    }

    if (Wanted == TEXT("flat"))
    {
        return MakeResult(StartPIE(), Wanted, TEXT(""));
    }

    // VR Preview drives the real HMD code path (OpenXR input, IsHeadMountedDisplayEnabled
    // branches). Without a connected HMD the engine silently falls back, which would make an
    // HMD-only bug look absent -- so refuse with a concrete reason instead.
    if (!GEngine || !GEngine->XRSystem.IsValid())
    {
        return MakeResult(false, Wanted, TEXT("no XR system loaded; VR Preview needs an XR plugin (OpenXR/MetaXR) enabled"));
    }
    if (!GEngine->XRSystem->GetHMDDevice() || !GEngine->XRSystem->GetHMDDevice()->IsHMDConnected())
    {
        return MakeResult(false, Wanted, TEXT("no HMD connected; connect the headset (Link/Air Link) before `pie start --mode vr`"));
    }

    FRequestPlaySessionParams Params;
    Params.SessionDestination = EPlaySessionDestinationType::InProcess;
    Params.WorldType = EPlaySessionWorldType::PlayInEditor;
    Params.SessionPreviewTypeOverride = EPlaySessionPreviewType::VRPreview;
    GEditor->RequestPlaySession(Params);
    return MakeResult(true, Wanted, TEXT(""));
}

namespace
{
    // The actual stop, shared by StopPIE (legacy bool) and StopPIEEx (JSON envelope).
    //
    // A PIE START IS QUEUED, NOT IMMEDIATE. RequestPlaySession only sets GEditor's
    // PlaySessionRequest; the editor tick creates the play world one or more frames later
    // (StartQueuedPlaySessionRequest). RequestEndPlayMap, meanwhile, is a NO-OP unless PlayWorld
    // ALREADY exists -- `if (PlayWorld) { bRequestEndPlayMapQueued = true; }` -- and the tick's
    // teardown is likewise gated on `PlayWorld && bRequestEndPlayMapQueued`.
    //
    // So a stop landing in the gap between "start queued" and "play world created" did literally
    // nothing, and the queued start then brought PIE up AFTER the stop had reported success.
    // Cancelling the pending request SERIALISES the stop against the engine's own queued work
    // instead of racing it -- that is the fix, not the caller's polling.
    bool UAPRequestStopPIE(bool& bOutWasPlaying, bool& bOutCancelledQueuedStart,
                           bool& bOutInProgress, FString& OutError)
    {
        bOutWasPlaying = false;
        bOutCancelledQueuedStart = false;
        bOutInProgress = false;
        OutError.Reset();

        if (!GEditor) { OutError = TEXT("no GEditor"); return false; }

        ULevelEditorSubsystem* LES = GEditor->GetEditorSubsystem<ULevelEditorSubsystem>();
        if (!LES)
        {
            bOutInProgress = GEditor->IsPlaySessionInProgress();
            OutError = TEXT("no LevelEditorSubsystem");
            return false;
        }

        // Read "is it playing" BEFORE cancelling: CancelRequestPlaySession also resets
        // PlayInEditorSessionInfo, which is what IsInPlayInEditor() reads. The engine normally
        // keeps queued and live mutually exclusive (StartQueuedPlaySessionRequest resets the
        // request), but if they ever overlapped, cancelling first would make a LIVE session look
        // stopped and we would skip the end-play request entirely.
        bOutWasPlaying = LES->IsInPlayInEditor();

        bOutCancelledQueuedStart = GEditor->IsPlaySessionRequestQueued();
        if (bOutCancelledQueuedStart)
        {
            GEditor->CancelRequestPlaySession();
        }

        if (bOutWasPlaying)
        {
            // RequestEndPlayMap is gated on PlayWorld, not on the session info the cancel above
            // may have cleared, so this still lands.
            LES->EditorRequestEndPlay();
        }

        // Teardown still happens on a later editor tick, so we cannot report "PIE is gone" here
        // -- and must not pretend to: blocking would block the very tick that performs it.
        // `in_progress` is the honest answer (live OR queued); the CALLER polls IsPIEInProgress()
        // until it reads false. See `uap pie stop`.
        bOutInProgress = GEditor->IsPlaySessionInProgress();
        return true;
    }
}

bool UUAPAgentSubsystem::StopPIE()
{
    UAP_ACTIVITY(TEXT("pie stop"), FString());
    bool bWasPlaying = false, bCancelled = false, bInProgress = false;
    FString Error;
    return UAPRequestStopPIE(bWasPlaying, bCancelled, bInProgress, Error);
}

FString UUAPAgentSubsystem::StopPIEEx()
{
    UAP_ACTIVITY(TEXT("pie stop"), FString());
    bool bWasPlaying = false, bCancelled = false, bInProgress = false;
    FString Error;
    const bool bOk = UAPRequestStopPIE(bWasPlaying, bCancelled, bInProgress, Error);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), bOk);
    O->SetBoolField(TEXT("was_playing"), bWasPlaying);
    O->SetBoolField(TEXT("cancelled_queued_start"), bCancelled);
    O->SetBoolField(TEXT("in_progress"), bInProgress);
    if (!Error.IsEmpty()) { O->SetStringField(TEXT("error"), Error); }
    FString Out;
    TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
    FJsonSerializer::Serialize(O, W);
    return Out;
}

bool UUAPAgentSubsystem::IsInPIE() const
{
    if (!GEditor) { return false; }
    ULevelEditorSubsystem* LES = GEditor->GetEditorSubsystem<ULevelEditorSubsystem>();
    return LES ? LES->IsInPlayInEditor() : false;
}

bool UUAPAgentSubsystem::IsPIEInProgress() const
{
    // Live OR queued -- UEditorEngine::IsPlaySessionInProgress() is
    // IsPlayingSessionInEditor() || IsPlaySessionRequestQueued(). IsInPIE() sees only the first
    // half, so it reads FALSE during the window in which a start has been requested but the play
    // world does not exist yet: polling IsInPIE() to confirm a teardown confirms nothing in
    // exactly the case that matters. This is the authoritative "PIE is live or about to be".
    return GEditor ? GEditor->IsPlaySessionInProgress() : false;
}

int64 UUAPAgentSubsystem::GetLogCursor() const
{
    return LogCapture.IsValid() ? LogCapture->GetCursor() : 0;
}

FString UUAPAgentSubsystem::GetLogsSince(
    int64 AfterCursor, int32 MaxLines, FString CategoryFilter, EAgentLogVerbosity MinVerbosity) const
{
    UAP_ACTIVITY(TEXT("log"), CategoryFilter);
    if (!LogCapture.IsValid())
    {
        return TEXT(R"({"cursor":0,"lines":[]})");
    }
    TArray<FAgentLogEntry> Entries;
    int64 Cursor = AfterCursor;
    FName CatName = CategoryFilter.IsEmpty() ? NAME_None : FName(*CategoryFilter);
    LogCapture->ReadSince(AfterCursor, MaxLines, CatName, MinVerbosity, Entries, Cursor);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetNumberField(TEXT("cursor"), (double)Cursor);
    TArray<TSharedPtr<FJsonValue>> Arr;
    for (const FAgentLogEntry& E : Entries)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetNumberField(TEXT("cursor"), (double)E.Cursor);
        O->SetNumberField(TEXT("timestamp"), E.TimestampSeconds);
        O->SetStringField(TEXT("category"), E.Category.ToString());
        O->SetStringField(TEXT("verbosity"), ToString(E.Verbosity));
        O->SetStringField(TEXT("message"), E.Message);
        Arr.Add(MakeShared<FJsonValueObject>(O));
    }
    Root->SetArrayField(TEXT("lines"), Arr);

    FString Out;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
    FJsonSerializer::Serialize(Root, Writer);
    return Out;
}

bool UUAPAgentSubsystem::InjectKey(FString KeyName, bool bPressed, bool bRepeat, FString Client)
{
    UAP_ACTIVITY(TEXT("input key"), KeyName);
    FKey Key(*KeyName);
    if (!Key.IsValid())
    {
        UE_LOG(LogUAP, Warning, TEXT("InjectKey: unknown key '%s'"), *KeyName);
        return false;
    }
    return FAgentInput::OnClients(TEXT("InjectKey"), Client, [&]() { return FAgentInput::InjectKey(Key, bPressed, bRepeat); });
}

FString UUAPAgentSubsystem::ListClients()
{
    return FAgentWorld::DescribeClientsJson();
}

bool UUAPAgentSubsystem::InjectMouseMove(float X, float Y, bool bAbsolute, FString Client)
{
    UAP_ACTIVITY(TEXT("input mousemove"), FString());
    return FAgentInput::OnClients(TEXT("InjectMouseMove"), Client, [&]() { return FAgentInput::InjectMouseMove(FVector2D(X, Y), bAbsolute); });
}

bool UUAPAgentSubsystem::InjectMouseButton(EAgentMouseButton Button, bool bPressed, FString Client)
{
    UAP_ACTIVITY(TEXT("input mousebutton"), FString());
    return FAgentInput::OnClients(TEXT("InjectMouseButton"), Client, [&]() { return FAgentInput::InjectMouseButton(Button, bPressed); });
}

FString UUAPAgentSubsystem::SetMousePosition(float X, float Y, FString Client)
{
    UAP_ACTIVITY(TEXT("input mousepos"), FString());
    return FAgentInput::ForEachClientJson(Client, [&]() { return FAgentInput::SetMousePositionJson(X, Y); });
}

FString UUAPAgentSubsystem::ClickMouse(EAgentMouseButton Button, FString X, FString Y, FString Client)
{
    UAP_ACTIVITY(TEXT("click"), FString::Printf(TEXT("%s,%s"), *X, *Y));
    return FAgentInput::ForEachClientJson(Client, [&]() { return FAgentInput::ClickMouseJson(Button, X, Y); });
}

bool UUAPAgentSubsystem::InjectAxis(FString AxisName, float Value, FString SlateUser, FString Client)
{
    UAP_ACTIVITY(TEXT("input axis"), AxisName);
    int32 User = INDEX_NONE;
    FString UserError;
    if (!FAgentInput::ResolveSlateUserParam(SlateUser, User, UserError))
    {
        // bool cannot carry a reason, so the reason goes to the log where `uap log since`
        // will find it. Silence here is what made the original defect unfileable.
        UE_LOG(LogUAP, Error, TEXT("InjectAxis: %s"), *UserError);
        return false;
    }
    return FAgentInput::OnClients(TEXT("InjectAxis"), Client, [&]() { return FAgentInput::InjectAxis(FName(*AxisName), Value, User); });
}

bool UUAPAgentSubsystem::InjectGamepad(EAgentGamepadButton Button, bool bPressed, float AnalogValue, FString SlateUser, FString Client)
{
    UAP_ACTIVITY(TEXT("input gamepad"), FString());
    int32 User = INDEX_NONE;
    FString UserError;
    if (!FAgentInput::ResolveSlateUserParam(SlateUser, User, UserError))
    {
        UE_LOG(LogUAP, Error, TEXT("InjectGamepad: %s"), *UserError);
        return false;
    }
    return FAgentInput::OnClients(TEXT("InjectGamepad"), Client, [&]() { return FAgentInput::InjectGamepad(Button, bPressed, AnalogValue, User); });
}

bool UUAPAgentSubsystem::InjectXRButton(EAgentXRHand Hand, FString ButtonKeyName, bool bPressed, FString Client)
{
    UAP_ACTIVITY(TEXT("input xr"), ButtonKeyName);
    // Quest Touch buttons are regular FKeys (e.g. OculusTouch_Left_X_Click). Route via Slate.
    FKey Key(*ButtonKeyName);
    if (!Key.IsValid())
    {
        UE_LOG(LogUAP, Warning, TEXT("InjectXRButton: unknown key '%s'"), *ButtonKeyName);
        return false;
    }
    // The key name already says which hand it belongs to; Hand used to be ignored, so a call
    // that named the other hand pressed a key the caller did not mean. Refuse the contradiction.
    if (FAgentMotionController::KeyContradictsHand(Hand, ButtonKeyName))
    {
        UE_LOG(LogUAP, Warning, TEXT("InjectXRButton: key '%s' belongs to the other hand (Hand=%s); nothing pressed"),
            *ButtonKeyName, Hand == EAgentXRHand::Left ? TEXT("Left") : TEXT("Right"));
        return false;
    }
    return FAgentInput::OnClients(TEXT("InjectXRButton"), Client, [&]() { return FAgentInput::InjectKey(Key, bPressed, false); });
}

// Thin forwarders: the validate-before-press ordering, the refusal messages and the recovery
// path all live in FAgentInput so the editor and runtime subsystems cannot drift apart.
FString UUAPAgentSubsystem::HoldKey(FString KeyName, float Seconds, FString Client)
{
    UAP_ACTIVITY(TEXT("input hold"), KeyName);
    return FAgentInput::ForEachClientJson(Client, [&]() { return FAgentInput::HoldKeyJson(KeyName, Seconds); });
}

FString UUAPAgentSubsystem::HoldAxis(FString AxisKeyName, float Value, float Seconds, FString SlateUser, FString Client)
{
    UAP_ACTIVITY(TEXT("input axis-hold"), AxisKeyName);
    return FAgentInput::ForEachClientJson(Client, [&]() { return FAgentInput::HoldAxisJson(AxisKeyName, Value, Seconds, SlateUser); });
}

FString UUAPAgentSubsystem::ReleaseHeldInput(FString KeyName, FString Client)
{
    UAP_ACTIVITY(TEXT("input release"), KeyName);
    return FAgentInput::ForEachClientJson(Client, [&]() { return FAgentInput::ReleaseHeldJson(KeyName); });
}

FString UUAPAgentSubsystem::RunInputSequence(FString ScriptJson, FString StartAt, FString Client)
{
    UAP_ACTIVITY(TEXT("input sequence"), FString());
    return FAgentInput::ForEachClientJson(Client, [&]() { return FAgentInput::RunSequenceJson(ScriptJson, StartAt); });
}

FString UUAPAgentSubsystem::GetInputSequenceLog(FString Client)
{
    return FAgentInput::ForEachClientJson(Client, []() { return FAgentInput::GetSequenceLogJson(); });
}

FString UUAPAgentSubsystem::GetHeldInput(FString Client)
{
    // Several clients -> one envelope per client, so a caller can see which client holds what.
    return FAgentInput::ForEachClientJson(Client, []() { return FAgentInput::GetHeldJson(); });
}

FString UUAPAgentSubsystem::ReleaseSlatePointerCapture()
{
    UAP_ACTIVITY(TEXT("input release-capture"), FString());
    return FAgentInput::ReleaseSlatePointerCaptureJson();
}

FString UUAPAgentSubsystem::StartPropertySample(FString ObjectPath, FString PropertyPath,
                                                float Seconds, int32 MaxSamples)
{
    UAP_ACTIVITY(TEXT("sample start"), PropertyPath);
    return FAgentSampler::Start(ObjectPath, PropertyPath, Seconds, MaxSamples);
}

FString UUAPAgentSubsystem::ReadPropertySample()
{
    UAP_ACTIVITY(TEXT("sample read"), FString());
    return FAgentSampler::Read();
}

bool UUAPAgentSubsystem::StopPropertySample()
{
    UAP_ACTIVITY(TEXT("sample stop"), FString());
    FAgentSampler::Stop();
    return true;
}

bool UUAPAgentSubsystem::InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked)
{
    UAP_ACTIVITY(TEXT("input xr-pose"), FString());
    FUnrealAgentPlayerRuntimeModule* Rtm = FUnrealAgentPlayerRuntimeModule::Get();
    FAgentMotionController* MC = Rtm ? Rtm->GetMotionController() : nullptr;
    if (!MC) { return false; }
    FAgentControllerPose Pose;
    Pose.Position = Position;
    Pose.Orientation = Orientation;
    Pose.bTracked = bTracked;
    MC->SetPose(Hand, Pose);
    return true;
}

bool UUAPAgentSubsystem::ClearXRControllerOverride(EAgentXRHand Hand)
{
    UAP_ACTIVITY(TEXT("input xr-clear"), FString());
    FUnrealAgentPlayerRuntimeModule* Rtm = FUnrealAgentPlayerRuntimeModule::Get();
    FAgentMotionController* MC = Rtm ? Rtm->GetMotionController() : nullptr;
    if (!MC) { return false; }
    MC->ClearPose(Hand);
    return true;
}

FString UUAPAgentSubsystem::SetXRAimEmulation(bool bEnabled, FVector GripToAimLocation, FRotator GripToAimRotation)
{
    UAP_ACTIVITY(TEXT("input xr-aim"), bEnabled ? TEXT("on") : TEXT("off"));
    FUnrealAgentPlayerRuntimeModule* Rtm = FUnrealAgentPlayerRuntimeModule::Get();
    FAgentMotionController* MC = Rtm ? Rtm->GetMotionController() : nullptr;
    if (!MC) { return TEXT("{\"ok\":false,\"error\":\"agent motion controller not registered\"}"); }
    return MC->SetAimEmulationJson(bEnabled, GripToAimLocation, GripToAimRotation);
}

FString UUAPAgentSubsystem::GetXRControllerState()
{
    FUnrealAgentPlayerRuntimeModule* Rtm = FUnrealAgentPlayerRuntimeModule::Get();
    FAgentMotionController* MC = Rtm ? Rtm->GetMotionController() : nullptr;
    if (!MC) { return TEXT("{\"ok\":false,\"error\":\"agent motion controller not registered\"}"); }
    return MC->StateJson();
}

FString UUAPAgentSubsystem::DumpViewportUI(FString Client)
{
    UAP_ACTIVITY(TEXT("read-ui"), FString());
    return FAgentInput::ForEachClientJson(Client, []() { return FAgentUIReader::DumpViewportUI(); });
}

bool UUAPAgentSubsystem::SelectTab(FString TabId)
{
    UAP_ACTIVITY(TEXT("tab"), TabId);
    const FName TabName(*TabId);
    for (TObjectIterator<UCommonTabListWidgetBase> It; It; ++It)
    {
        UCommonTabListWidgetBase* TabList = *It;
        if (!IsValid(TabList))
        {
            continue;
        }
        // Only the live game's tab list (constructed + in a game world), not editor/CDO copies.
        const UWorld* W = TabList->GetWorld();
        if (!W || !W->IsGameWorld() || !TabList->IsConstructed())
        {
            continue;
        }
        if (TabList->GetTabButtonBaseByID(TabName) != nullptr)
        {
            return TabList->SelectTabByID(TabName);
        }
    }
    return false;
}

bool UUAPAgentSubsystem::NavigateUI(FString Direction)
{
    UAP_ACTIVITY(TEXT("nav"), Direction);
    if (!FSlateApplication::IsInitialized())
    {
        return false;
    }
    const FString D = Direction.ToLower();
    FKey Key;
    if (D == TEXT("up"))                                 { Key = EKeys::Up; }
    else if (D == TEXT("down"))                          { Key = EKeys::Down; }
    else if (D == TEXT("left"))                          { Key = EKeys::Left; }
    else if (D == TEXT("right"))                         { Key = EKeys::Right; }
    else if (D == TEXT("accept") || D == TEXT("select")) { Key = EKeys::Enter; }
    else if (D == TEXT("back") || D == TEXT("cancel"))   { Key = EKeys::Escape; }
    else { return false; }

    // Drive Slate's focus navigation (the path menus use), NOT the game-input viewport path:
    // ProcessKeyDownEvent lets Slate translate arrow/Enter/Escape into UI navigation/activation.
    // One resolver for every Slate-path injection in this plugin. This used to call
    // GetUserIndexForKeyboard() directly, which is only the SECOND choice: the user whose focus
    // path holds the game viewport is the one the game is actually routing.
    FString UserError;
    const int32 User = FAgentInput::ResolveSlateUserIndex(INDEX_NONE, UserError);
    if (User == INDEX_NONE)
    {
        UE_LOG(LogUAP, Error, TEXT("NavigateUI: %s"), *UserError);
        return false;
    }
    FSlateApplication& App = FSlateApplication::Get();
    FKeyEvent KeyDown(Key, App.GetModifierKeys(), (uint32)User, /*bIsRepeat*/ false, 0, 0);
    const bool bHandled = App.ProcessKeyDownEvent(KeyDown);
    FKeyEvent KeyUp(Key, App.GetModifierKeys(), (uint32)User, false, 0, 0);
    App.ProcessKeyUpEvent(KeyUp);
    return bHandled;
}

bool UUAPAgentSubsystem::CaptureViewportWithUI(FString Filename)
{
    UAP_ACTIVITY(TEXT("screenshot"), FPaths::GetCleanFilename(Filename));
    if (Filename.IsEmpty())
    {
        return false;
    }
    // Keep rendering while the editor is not the foreground window, otherwise the
    // backbuffer can be stale/black when an external agent triggers the capture.
    if (IConsoleVariable* CVar = IConsoleManager::Get().FindConsoleVariable(TEXT("t.IdleWhenNotForeground")))
    {
        CVar->Set(0, ECVF_SetByCode);
    }

    // Preferred path: target the PIE game viewport's Slate widget directly and draw ITS
    // window -- NOT whatever window/tab happens to be in the foreground. This makes the
    // capture immune to an agent opening a Blueprint/asset editor mid-test (which would
    // otherwise steal the active surface and get captured instead of the game), and it
    // crops to just the game view (no editor chrome). TakeScreenshot composites 3D + UMG
    // + Slate (like bShowUI) and fills the bitmap synchronously, so the PNG exists on
    // return -- no next-frame polling race.
    TSharedPtr<SViewport> ViewportWidget;
    if (GEngine)
    {
        for (const FWorldContext& Ctx : GEngine->GetWorldContexts())
        {
            if (Ctx.WorldType == EWorldType::PIE && Ctx.GameViewport)
            {
                ViewportWidget = Ctx.GameViewport->GetGameViewportWidget();
                if (ViewportWidget.IsValid()) { break; }
            }
        }
    }

    if (ViewportWidget.IsValid() && FSlateApplication::IsInitialized())
    {
        TArray<FColor> Bitmap;
        FIntVector Size(0, 0, 0);
        if (FSlateApplication::Get().TakeScreenshot(ViewportWidget.ToSharedRef(), Bitmap, Size)
            && Size.X > 0 && Size.Y > 0 && Bitmap.Num() >= Size.X * Size.Y)
        {
            // Slate readback leaves alpha at the framebuffer value (often 0); force opaque
            // so the saved PNG is not fully transparent.
            for (FColor& Px : Bitmap) { Px.A = 255; }

            const FImageView Img(Bitmap.GetData(), Size.X, Size.Y);
            if (FImageUtils::SaveImageByExtension(*Filename, Img))
            {
                return true;
            }
        }
        // Fall through to the legacy path on any failure above.
    }

    // No live PIE game viewport -- an idle editor showing only the level view. This used to
    // queue an FScreenshotRequest for the NEXT rendered frame, which an idle or unfocused
    // editor never produces (17tm466fv84 / 17tm466ft2k). Draw the level viewport ourselves
    // and read it back synchronously instead; the PNG exists when this returns.
    const FString Result = CaptureLevelViewport(Filename, FString());
    TSharedPtr<FJsonObject> Parsed;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Result);
    bool bOk = false;
    if (FJsonSerializer::Deserialize(Reader, Parsed) && Parsed.IsValid())
    {
        Parsed->TryGetBoolField(TEXT("ok"), bOk);
    }
    if (!bOk)
    {
        UE_LOG(LogUAP, Warning, TEXT("CaptureViewportWithUI: level viewport capture failed: %s"), *Result);
    }
    return bOk;
}

// ---------------------------------------------------------------------------------------------
// LEVEL EDITOR VIEWPORT: pose + capture (ClickUp 17tm466fv84 / 17tm466ft2k)
//
// Two defects lived here. (1) `CaptureViewportWithUI` with no PIE fell through to
// FScreenshotRequest, which waits for a NEXT rendered frame -- and an idle or unfocused editor
// never renders one, so the file never appeared. (2) `UnrealEditorSubsystem.set_level_viewport_
// camera_info` moves the FIRST PERSPECTIVE viewport client, while a capture grabs the ACTIVE one;
// in a multi-pane layout those are different panes, so a pose set and a screenshot taken a moment
// later silently disagreed (a 28 per channel "regression" that was not real).
//
// The fix is ONE resolver (`ResolveLevelPane`) used by both the pose and the capture, and a
// capture that DRAWS the viewport itself (`FViewport::Draw`, the call the editor's own tick makes
// for every viewport) and reads the pixels back synchronously. Nothing waits for a frame, so
// focus and realtime do not matter.
// ---------------------------------------------------------------------------------------------
namespace
{
    struct FUAPLevelPane
    {
        FLevelEditorViewportClient* Client = nullptr;
        FString Key;
        bool bActive = false;
    };

    // Every level editor viewport pane that currently has a client + a viewport.
    TArray<FUAPLevelPane> ListLevelPanes()
    {
        TArray<FUAPLevelPane> Out;
        if (!FModuleManager::Get().IsModuleLoaded(TEXT("LevelEditor")))
        {
            return Out;
        }
        FLevelEditorModule& Module = FModuleManager::GetModuleChecked<FLevelEditorModule>(TEXT("LevelEditor"));
        const TSharedPtr<ILevelEditor> Editor = Module.GetFirstLevelEditor();
        if (!Editor.IsValid())
        {
            return Out;
        }
        const TSharedPtr<SLevelViewport> Active = Editor->GetActiveViewportInterface();
        for (const TSharedPtr<SLevelViewport>& Pane : Editor->GetViewports())
        {
            if (!Pane.IsValid())
            {
                continue;
            }
            FUAPLevelPane Entry;
            Entry.Client = &Pane->GetLevelViewportClient();
            Entry.Key = Pane->GetConfigKey().ToString();
            Entry.bActive = Active.IsValid() && Active == Pane;
            if (Entry.Client && Entry.Client->Viewport)
            {
                Out.Add(Entry);
            }
        }
        return Out;
    }

    // The pane `UnrealEditorSubsystem.set_level_viewport_camera_info` moves: the first
    // perspective client in GEditor's list (UnrealEditorSubsystem.cpp GetViewportClient).
    FLevelEditorViewportClient* UnrealEditorSubsystemClient()
    {
        if (GEditor)
        {
            for (FLevelEditorViewportClient* VC : GEditor->GetLevelViewportClients())
            {
                if (VC && VC->IsPerspective())
                {
                    return VC;
                }
            }
        }
        return nullptr;
    }

    // ONE resolution for the pose set, the read-back and the capture, so they cannot land on
    // different panes. Selector: "" / "auto" = the ACTIVE pane when it is perspective, else the
    // first perspective pane; "active" = the active pane whatever it is; "first-perspective" =
    // the pane UnrealEditorSubsystem moves; otherwise a pane config key, or a unique suffix of
    // one ("Viewport1"). Returns false with Error set when nothing matches.
    bool ResolveLevelPane(const FString& Selector, const TArray<FUAPLevelPane>& Panes,
                          FUAPLevelPane& Out, FString& Why, FString& Error)
    {
        if (Panes.Num() == 0)
        {
            Error = TEXT("no level editor viewport pane found (is a level editor open and is its window visible?)");
            return false;
        }
        const FString Sel = Selector.TrimStartAndEnd();
        const FUAPLevelPane* Active = Panes.FindByPredicate([](const FUAPLevelPane& P) { return P.bActive; });
        const FLevelEditorViewportClient* FirstPersp = UnrealEditorSubsystemClient();
        const FUAPLevelPane* FirstPerspPane = Panes.FindByPredicate(
            [FirstPersp](const FUAPLevelPane& P) { return P.Client == FirstPersp; });

        if (Sel.IsEmpty() || Sel.Equals(TEXT("auto"), ESearchCase::IgnoreCase))
        {
            if (Active && Active->Client->IsPerspective())
            {
                Out = *Active;
                Why = TEXT("active pane (perspective)");
                return true;
            }
            const FUAPLevelPane* Persp = FirstPerspPane
                ? FirstPerspPane
                : Panes.FindByPredicate([](const FUAPLevelPane& P) { return P.Client->IsPerspective(); });
            if (Persp)
            {
                Out = *Persp;
                Why = Active ? TEXT("first perspective pane (the active pane is orthographic)")
                             : TEXT("first perspective pane (no pane is active)");
                return true;
            }
            Out = Active ? *Active : Panes[0];
            Why = TEXT("no perspective pane exists");
            return true;
        }
        if (Sel.Equals(TEXT("active"), ESearchCase::IgnoreCase))
        {
            if (!Active) { Error = TEXT("no pane is active"); return false; }
            Out = *Active;
            Why = TEXT("active pane");
            return true;
        }
        if (Sel.Equals(TEXT("first-perspective"), ESearchCase::IgnoreCase))
        {
            if (!FirstPerspPane) { Error = TEXT("no perspective pane exists"); return false; }
            Out = *FirstPerspPane;
            Why = TEXT("first perspective pane (the one UnrealEditorSubsystem moves)");
            return true;
        }
        TArray<const FUAPLevelPane*> Hits;
        for (const FUAPLevelPane& P : Panes)
        {
            if (P.Key.Equals(Sel, ESearchCase::IgnoreCase)) { Hits.Empty(); Hits.Add(&P); break; }
            if (P.Key.EndsWith(Sel, ESearchCase::IgnoreCase)) { Hits.Add(&P); }
        }
        if (Hits.Num() == 1)
        {
            Out = *Hits[0];
            Why = TEXT("named pane");
            return true;
        }
        TArray<FString> Keys;
        for (const FUAPLevelPane& P : Panes) { Keys.Add(P.Key); }
        Error = FString::Printf(TEXT("pane '%s' %s; panes: %s"), *Sel,
                                Hits.Num() == 0 ? TEXT("matches nothing") : TEXT("is ambiguous"),
                                *FString::Join(Keys, TEXT(", ")));
        return false;
    }

    TSharedRef<FJsonValueArray> UAPVec3Json(double A, double B, double C)
    {
        TArray<TSharedPtr<FJsonValue>> V;
        V.Add(MakeShared<FJsonValueNumber>(A));
        V.Add(MakeShared<FJsonValueNumber>(B));
        V.Add(MakeShared<FJsonValueNumber>(C));
        return MakeShared<FJsonValueArray>(V);
    }

    bool UAPReadVec3(const TSharedPtr<FJsonObject>& Obj, const TCHAR* Field, FVector& Out)
    {
        const TArray<TSharedPtr<FJsonValue>>* Arr = nullptr;
        if (!Obj->TryGetArrayField(Field, Arr) || !Arr || Arr->Num() != 3)
        {
            return false;
        }
        Out = FVector((*Arr)[0]->AsNumber(), (*Arr)[1]->AsNumber(), (*Arr)[2]->AsNumber());
        return true;
    }

    FString UAPLevelViewportJson(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, W);
        return Out;
    }

    FString UAPLevelViewportError(const FString& Error)
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetBoolField(TEXT("ok"), false);
        Obj->SetStringField(TEXT("error"), Error);
        return UAPLevelViewportJson(Obj);
    }

    // Describes the resolved pane + its pose + the layout, so a caller can SEE which pane it hit.
    void UAPDescribePane(const FUAPLevelPane& Pane, const FString& Why,
                         const TArray<FUAPLevelPane>& Panes, const TSharedRef<FJsonObject>& Obj)
    {
        FLevelEditorViewportClient* VC = Pane.Client;
        Obj->SetStringField(TEXT("pane"), Pane.Key);
        Obj->SetStringField(TEXT("pane_chosen_because"), Why);
        Obj->SetBoolField(TEXT("pane_is_active"), Pane.bActive);
        Obj->SetBoolField(TEXT("pane_is_perspective"), VC->IsPerspective());
        const bool bSameAsSubsystem = (VC == UnrealEditorSubsystemClient());
        Obj->SetBoolField(TEXT("pane_is_unreal_editor_subsystem_pane"), bSameAsSubsystem);
        TArray<TSharedPtr<FJsonValue>> Keys;
        for (const FUAPLevelPane& P : Panes)
        {
            Keys.Add(MakeShared<FJsonValueString>(
                P.Key + (P.bActive ? TEXT(" (active)") : TEXT("")) + (P.Client->IsPerspective() ? TEXT("") : TEXT(" (ortho)"))));
        }
        Obj->SetArrayField(TEXT("panes"), Keys);
        const FVector Loc = VC->GetViewLocation();
        const FRotator Rot = VC->GetViewRotation();
        Obj->SetField(TEXT("location"), UAPVec3Json(Loc.X, Loc.Y, Loc.Z));
        Obj->SetField(TEXT("rotation_pitch_yaw_roll"), UAPVec3Json(Rot.Pitch, Rot.Yaw, Rot.Roll));
        Obj->SetNumberField(TEXT("fov"), VC->ViewFOV);
        if (!bSameAsSubsystem)
        {
            Obj->SetStringField(TEXT("warning"),
                TEXT("this is NOT the pane that unreal.get_editor_subsystem(UnrealEditorSubsystem)."
                     "set_level_viewport_camera_info moves (the first perspective pane). A pose set "
                     "that way and this capture are different cameras. Set the pose with "
                     "`uap viewport pose` / `uap screenshot --pose` instead, or pass "
                     "--pane first-perspective."));
        }
    }

    // Applies the pose fields present in Req to the pane. Returns false + Error on a bad
    // request. Sets bApplied when at least one field was applied.
    bool UAPApplyPose(const FUAPLevelPane& Pane, const TSharedPtr<FJsonObject>& Req,
                      bool& bApplied, TArray<FString>& Warnings, FString& Error)
    {
        bApplied = false;
        FLevelEditorViewportClient* VC = Pane.Client;
        FVector Loc = VC->GetViewLocation();
        FRotator Rot = VC->GetViewRotation();
        FVector V;
        bool bHaveLoc = false;
        bool bHaveRot = false;

        if (Req->HasField(TEXT("location")))
        {
            if (!UAPReadVec3(Req, TEXT("location"), V)) { Error = TEXT("location must be [x,y,z]"); return false; }
            Loc = V;
            bHaveLoc = true;
        }
        if (Req->HasField(TEXT("rotation")))
        {
            if (!UAPReadVec3(Req, TEXT("rotation"), V)) { Error = TEXT("rotation must be [pitch,yaw,roll]"); return false; }
            Rot = FRotator(V.X, V.Y, V.Z);   // FRotator(Pitch, Yaw, Roll) -- NOT unreal.Rotator(roll, pitch, yaw)
            bHaveRot = true;
        }
        if (Req->HasField(TEXT("look_at")))
        {
            if (!UAPReadVec3(Req, TEXT("look_at"), V)) { Error = TEXT("look_at must be [x,y,z]"); return false; }
            if ((V - Loc).IsNearlyZero()) { Error = TEXT("look_at is the camera location; no direction"); return false; }
            Rot = (V - Loc).Rotation();
            Rot.Roll = 0.f;
            bHaveRot = true;
        }
        double Fov = 0.0;
        const bool bHaveFov = Req->TryGetNumberField(TEXT("fov"), Fov);
        if (!(bHaveLoc || bHaveRot || bHaveFov))
        {
            return true;   // read-only request
        }
        if (!VC->IsPerspective())
        {
            Error = FString::Printf(TEXT("pane '%s' is orthographic; a pose can only be set on a perspective pane"), *Pane.Key);
            return false;
        }
        if (VC->GetActiveActorLock().IsValid() || VC->GetCinematicActorLock().HasValidLockedActor())
        {
            Warnings.Add(TEXT("the pane is piloting an actor / locked to a cinematic actor: the editor overwrites "
                              "the free camera pose from that actor, so the requested pose may not show. "
                              "Eject the pilot first."));
        }
        if (bHaveLoc || bHaveRot)
        {
            VC->SetViewLocationForOrbiting(Loc);
            VC->SetViewLocation(Loc);
            VC->SetViewRotation(Rot);
        }
        if (bHaveFov)
        {
            VC->ViewFOV = FMath::Clamp((float)Fov, 5.f, 170.f);
        }
        VC->Invalidate();
        bApplied = true;
        return true;
    }

    // Shared by both verbs: parse the request, resolve the pane, apply the pose.
    bool UAPPrepareLevelPane(const FString& PoseJson, TSharedPtr<FJsonObject>& Req,
                             TArray<FUAPLevelPane>& Panes, FUAPLevelPane& Pane, FString& Why,
                             bool& bApplied, TArray<FString>& Warnings, FString& Error)
    {
        Req = MakeShared<FJsonObject>();
        if (!PoseJson.TrimStartAndEnd().IsEmpty())
        {
            TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(PoseJson);
            if (!FJsonSerializer::Deserialize(Reader, Req) || !Req.IsValid())
            {
                Error = TEXT("PoseJson is not a JSON object");
                return false;
            }
        }
        FString Selector;
        Req->TryGetStringField(TEXT("pane"), Selector);
        Panes = ListLevelPanes();
        if (!ResolveLevelPane(Selector, Panes, Pane, Why, Error))
        {
            return false;
        }
        return UAPApplyPose(Pane, Req, bApplied, Warnings, Error);
    }

    void UAPAddPoseWarnings(const TArray<FString>& Warnings, const TSharedRef<FJsonObject>& Obj)
    {
        if (Warnings.Num() == 0) { return; }
        TArray<TSharedPtr<FJsonValue>> W;
        for (const FString& S : Warnings) { W.Add(MakeShared<FJsonValueString>(S)); }
        Obj->SetArrayField(TEXT("pose_warnings"), W);
    }
}

FString UUAPAgentSubsystem::LevelViewportPose(FString PoseJson)
{
    UAP_ACTIVITY(TEXT("viewport pose"), FString());
    TSharedPtr<FJsonObject> Req;
    TArray<FUAPLevelPane> Panes;
    FUAPLevelPane Pane;
    FString Why, Error;
    bool bApplied = false;
    TArray<FString> Warnings;
    if (!UAPPrepareLevelPane(PoseJson, Req, Panes, Pane, Why, bApplied, Warnings, Error))
    {
        return UAPLevelViewportError(Error);
    }
    // The read-back is taken from the SAME client the pose was applied to, in the same call.
    TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
    Obj->SetBoolField(TEXT("ok"), true);
    Obj->SetBoolField(TEXT("pose_applied"), bApplied);
    UAPDescribePane(Pane, Why, Panes, Obj);
    UAPAddPoseWarnings(Warnings, Obj);
    return UAPLevelViewportJson(Obj);
}

FString UUAPAgentSubsystem::CaptureLevelViewport(FString Filename, FString PoseJson)
{
    UAP_ACTIVITY(TEXT("screenshot"), FPaths::GetCleanFilename(Filename));
    if (Filename.IsEmpty())
    {
        return UAPLevelViewportError(TEXT("Filename is empty"));
    }
    TSharedPtr<FJsonObject> Req;
    TArray<FUAPLevelPane> Panes;
    FUAPLevelPane Pane;
    FString Why, Error;
    bool bApplied = false;
    TArray<FString> Warnings;
    // Pose, read-back and capture all use `Pane.Client`, in this ONE call: nothing can move the
    // active pane in between.
    if (!UAPPrepareLevelPane(PoseJson, Req, Panes, Pane, Why, bApplied, Warnings, Error))
    {
        return UAPLevelViewportError(Error);
    }
    double DrawFrames = 3.0;   // the first frame after a pose change can still be streaming / TAA-cold
    Req->TryGetNumberField(TEXT("frames"), DrawFrames);
    const int32 Frames = FMath::Clamp((int32)DrawFrames, 1, 30);

    FLevelEditorViewportClient* VC = Pane.Client;
    FViewport* Viewport = VC->Viewport;
    const FIntPoint Size = Viewport ? Viewport->GetSizeXY() : FIntPoint::ZeroValue;
    if (!Viewport || Size.X <= 0 || Size.Y <= 0)
    {
        return UAPLevelViewportError(FString::Printf(
            TEXT("pane '%s' has a %dx%d viewport (the editor window is minimised or the pane is not "
                 "on screen), so there is nothing to draw. Restore the window without taking focus "
                 "or use another pane."), *Pane.Key, Size.X, Size.Y));
    }

    // Draw the viewport OURSELVES, the same call UEditorEngine::UpdateSingleViewportClient makes
    // for a realtime viewport. This is the whole fix: nothing waits for the editor to decide to
    // render a frame, so an idle, non-realtime or unfocused editor still produces one.
    TArray<FColor> Bitmap;
    {
        FScopedConditionalWorldSwitcher WorldSwitcher(VC);
        for (int32 i = 0; i < Frames; ++i)
        {
            VC->Invalidate();
            Viewport->Draw();
        }
        FReadSurfaceDataFlags ReadFlags(RCM_UNorm);
        ReadFlags.SetLinearToGamma(false);
        if (!Viewport->ReadPixels(Bitmap, ReadFlags, FIntRect(0, 0, Size.X, Size.Y))
            || Bitmap.Num() < Size.X * Size.Y)
        {
            return UAPLevelViewportError(TEXT("viewport pixel read-back returned no data"));
        }
    }
    for (FColor& Px : Bitmap) { Px.A = 255; }

    // Refuse an all-black / one-flat-colour frame: a "proof" screenshot of nothing is worse than none.
    bool bFlat = true;
    for (int32 i = 1; i < Bitmap.Num() && bFlat; ++i)
    {
        bFlat = (Bitmap[i].DWColor() == Bitmap[0].DWColor());
    }
    if (bFlat)
    {
        return UAPLevelViewportError(FString::Printf(
            TEXT("pane '%s' came back as one flat colour (0x%08X). The scene did not render: shaders or the "
                 "level may still be loading, or the pose looks at the sky or inside a mesh. Nothing was saved."),
            *Pane.Key, Bitmap[0].DWColor()));
    }

    IFileManager::Get().MakeDirectory(*FPaths::GetPath(Filename), /*Tree=*/true);
    const FImageView Img(Bitmap.GetData(), Size.X, Size.Y);
    if (!FImageUtils::SaveImageByExtension(*Filename, Img))
    {
        return UAPLevelViewportError(FString::Printf(TEXT("could not write %s"), *Filename));
    }

    TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
    Obj->SetBoolField(TEXT("ok"), true);
    Obj->SetStringField(TEXT("path"), Filename);
    Obj->SetNumberField(TEXT("width"), Size.X);
    Obj->SetNumberField(TEXT("height"), Size.Y);
    Obj->SetNumberField(TEXT("frames_drawn"), Frames);
    Obj->SetBoolField(TEXT("pose_applied"), bApplied);
    UAPDescribePane(Pane, Why, Panes, Obj);
    UAPAddPoseWarnings(Warnings, Obj);
    return UAPLevelViewportJson(Obj);
}

void UUAPAgentSubsystem::RefreshHelperCache()
{
    FAgentHelperDiscovery::RescanAll(HelperCache);
}

TArray<FAgentHelperDescriptor> UUAPAgentSubsystem::ListTestHelpers()
{
    UAP_ACTIVITY(TEXT("helpers"), FString());
    if (HelperCache.Num() == 0)
    {
        RefreshHelperCache();
    }
    return HelperCache;
}

FString UUAPAgentSubsystem::ListTestHelpersJson()
{
    return FAgentHelperDiscovery::ToJson(ListTestHelpers());
}

FString UUAPAgentSubsystem::CallTestHelper(FString Name, FString JsonArgs)
{
    UAP_ACTIVITY(TEXT("helper"), Name);
    UClass* Cls = nullptr;
    UFunction* Fn = FAgentHelperDiscovery::Resolve(Name, Cls);
    if (!Fn)
    {
        return TEXT(R"({"ok":false,"error":{"code":"HELPER_UNKNOWN","message":"helper not found"}})");
    }

    const FString Phase = Fn->HasMetaData(TEXT("Phase")) ? Fn->GetMetaData(TEXT("Phase")) : TEXT("Any");
    if (Phase == TEXT("Playing") && CurrentPhase != EAgentPIEPhase::Playing)
    {
        return FString::Printf(
            TEXT(R"({"ok":false,"error":{"code":"PIE_WRONG_PHASE","message":"need Playing, got %s"}})"),
            *StaticEnum<EAgentPIEPhase>()->GetNameStringByValue((int64)CurrentPhase));
    }

    UObject* Target = Cls->GetDefaultObject();
    if (!Target)
    {
        return TEXT(R"({"ok":false,"error":{"code":"HELPER_RAISED","message":"no default object"}})");
    }

    uint8* ParamBuffer = (uint8*)FMemory_Alloca(Fn->ParmsSize);
    FMemory::Memzero(ParamBuffer, Fn->ParmsSize);
    for (TFieldIterator<FProperty> It(Fn); It; ++It)
    {
        It->InitializeValue_InContainer(ParamBuffer);
    }

    TSharedPtr<FJsonObject> ArgsObj;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonArgs);
    if (!FJsonSerializer::Deserialize(Reader, ArgsObj) || !ArgsObj.IsValid())
    {
        ArgsObj = MakeShared<FJsonObject>();
    }

    for (TFieldIterator<FProperty> It(Fn); It; ++It)
    {
        if (It->HasAnyPropertyFlags(CPF_ReturnParm)) { continue; }
        const FString& FieldName = It->GetName();
        TSharedPtr<FJsonValue> Val = ArgsObj->TryGetField(FieldName);
        if (Val.IsValid())
        {
            FJsonObjectConverter::JsonValueToUProperty(Val, *It, It->ContainerPtrToValuePtr<void>(ParamBuffer));
        }
    }

    Target->ProcessEvent(Fn, ParamBuffer);

    FProperty* Ret = Fn->GetReturnProperty();
    FString OutJson = TEXT(R"({"ok":true,"result":null})");
    if (Ret)
    {
        TSharedPtr<FJsonValue> JV = FJsonObjectConverter::UPropertyToJsonValue(Ret, Ret->ContainerPtrToValuePtr<void>(ParamBuffer));
        TSharedRef<FJsonObject> Envelope = MakeShared<FJsonObject>();
        Envelope->SetBoolField(TEXT("ok"), true);
        Envelope->SetField(TEXT("result"), JV);
        FString Tmp;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Tmp);
        FJsonSerializer::Serialize(Envelope, W);
        OutJson = Tmp;
    }

    for (TFieldIterator<FProperty> It(Fn); It; ++It)
    {
        It->DestroyValue_InContainer(ParamBuffer);
    }

    return OutJson;
}

FString UUAPAgentSubsystem::GetStatGroupText(FString GroupName)
{
    UAP_ACTIVITY(TEXT("perf"), GroupName);
    if (!GEngine) { return TEXT(""); }

    // GRenderThreadTime and RHIGetGPUFrameCycles() are platform cycle counts updated each frame.
    // GGPUFrameTime was deprecated in UE 5.6 and removed in 5.8; RHIGetGPUFrameCycles() replaces it
    // and exists from 5.6 on, so this one call is correct on 5.6, 5.7, the Meta 5.7 fork, and 5.8.
    const double RenderMs = FPlatformTime::ToMilliseconds(GRenderThreadTime);
    const double GpuMs    = FPlatformTime::ToMilliseconds(RHIGetGPUFrameCycles());

    if (GroupName == TEXT("unit"))
    {
        const double FrameSecs = FApp::GetDeltaTime();
        const double GameSecs  = FApp::GetCurrentTime() - FApp::GetLastTime();
        return FString::Printf(
            TEXT("Frame: %.2f ms\nGame: %.2f ms\nDraw: %.2f ms\nGPU: %.2f ms"),
            FrameSecs * 1000.0,
            GameSecs  * 1000.0,
            RenderMs,
            GpuMs
        );
    }
    if (GroupName == TEXT("fps"))
    {
        const double FrameSecs = FMath::Max(FApp::GetDeltaTime(), 1e-6);
        return FString::Printf(TEXT("FPS: %.1f"), 1.0 / FrameSecs);
    }
    if (GroupName == TEXT("gpu"))
    {
        return FString::Printf(TEXT("GPU: %.2f ms"), GpuMs);
    }
    if (GroupName == TEXT("draw") || GroupName == TEXT("render"))
    {
        return FString::Printf(TEXT("Draw: %.2f ms"), RenderMs);
    }
    return TEXT("");
}

FString UUAPAgentSubsystem::ClearRemoteExecGlobals()
{
    UAP_ACTIVITY(TEXT("py clear-globals"), FString());
    auto MakeError = [](const FString& Error) -> FString
    {
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetBoolField(TEXT("ok"), false);
        Obj->SetStringField(TEXT("error"), Error);
        FString Out;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, W);
        return Out;
    };

    IPythonScriptPlugin* Python = IPythonScriptPlugin::Get();
    if (!Python || !Python->IsPythonAvailable() || !Python->IsPythonInitialized())
    {
        // Guarded rather than attempted: ExecPythonCommandEx itself does
        // ensureAlwaysMsgf(false) when Python is unavailable, which would fire an ensure on
        // every map load in a -NoPython editor.
        return MakeError(TEXT("python not available or not initialized"));
    }

    // ONE EXPRESSION, on purpose.
    //
    // EvaluateStatement is the only execution mode that hands the result back in
    // FPythonCommandEx::CommandResult, and it routes through FPythonScriptPlugin::RunString ->
    // EvalString(..., PyConsoleGlobalDict, PyConsoleLocalDict) -- the SAME dict the remote
    // execution channel uses for `uap exec`. So globals() inside this expression IS the dict an
    // agent's snippets write into; `import __main__` would NOT be (the console dict is a copy of
    // __main__'s, made once at startup).
    //
    // The inner comprehension is materialised into a list of names before the outer one runs, so
    // nothing mutates the dict it is walking. The (pop, True)[1] idiom pops as a side effect
    // while letting the outer comprehension yield the NAME rather than the popped value.
    FPythonCommandEx Cmd;
    Cmd.ExecutionMode = EPythonCommandExecutionMode::EvaluateStatement;
    Cmd.Flags = EPythonCommandFlags::Unattended;
    Cmd.Command = TEXT(
        "','.join([n for n in ["
        "n for n, v in list(globals().items()) "
        "if not n.startswith('_') "
        "and not isinstance(v, (bool, int, float, str, bytes, type(None))) "
        "and not callable(v) "
        "and not isinstance(v, __import__('types').ModuleType)"
        "] if (globals().pop(n, None), True)[1]])");

    if (!Python->ExecPythonCommandEx(Cmd))
    {
        return MakeError(FString::Printf(TEXT("python error: %s"), *Cmd.CommandResult));
    }

    // CommandResult is the REPR of the returned str, i.e. "'w,pawn'" -- or "''" when nothing was
    // dropped -- so peel the quotes before splitting.
    FString Joined = Cmd.CommandResult;
    Joined.TrimStartAndEndInline();
    if (Joined.Len() >= 2
        && ((Joined.StartsWith(TEXT("'")) && Joined.EndsWith(TEXT("'")))
            || (Joined.StartsWith(TEXT("\"")) && Joined.EndsWith(TEXT("\"")))))
    {
        Joined = Joined.Mid(1, Joined.Len() - 2);
    }

    TArray<FString> Names;
    Joined.ParseIntoArray(Names, TEXT(","), /*InCullEmpty=*/true);

    TArray<TSharedPtr<FJsonValue>> NameValues;
    NameValues.Reserve(Names.Num());
    for (FString& Name : Names)
    {
        Name.TrimStartAndEndInline();
        NameValues.Add(MakeShared<FJsonValueString>(Name));
    }

    TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
    Obj->SetBoolField(TEXT("ok"), true);
    Obj->SetArrayField(TEXT("cleared"), NameValues);
    Obj->SetNumberField(TEXT("count"), NameValues.Num());
    FString Out;
    TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
    FJsonSerializer::Serialize(Obj, W);
    return Out;
}

void UUAPAgentSubsystem::OnPreMapLoad(const FString& Filename, FCanLoadMap& /*OutCanLoadMap*/)
{
    // CLEAR, never veto. OutCanLoadMap is left untouched on purpose: the level change the agent
    // asked for still happens. Refusing it would trade a crash for a dead end, and the dead end
    // would be indistinguishable from a broken load_level.
    //
    // The cost of clearing is that a snippet which loads a level and THEN reads a global it set
    // earlier in the same snippet gets a NameError instead of a crash. That is the intended
    // trade, and the warning below names exactly which globals went, so the NameError is
    // explainable rather than mysterious.
    const FString Result = ClearRemoteExecGlobals();

    // PARSE the payload, never substring-match it. This first shipped as
    // Result.Contains("\"count\":0"), which matches the SUCCESS-with-nothing-dropped payload only,
    // and only at its current spacing. The error payload therefore fell through to the Warning
    // below -- and it fires on every editor launch, because OnMapLoad broadcasts for the startup
    // map BEFORE Python is initialised, so the verb answers
    // {"ok":false,"error":"python not available or not initialized"}. The log then asserted a clear
    // that had never happened, once per start. A whitespace change in the writer would have
    // reintroduced it, hence the parse.
    bool bOk = false;
    double Count = 0.0;
    TSharedPtr<FJsonObject> Parsed;
    const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Result);
    if (!FJsonSerializer::Deserialize(Reader, Parsed) || !Parsed.IsValid()
        || !Parsed->TryGetBoolField(TEXT("ok"), bOk))
    {
        // Unreadable: state that it could not be read, and claim nothing about what was swept.
        UE_LOG(LogUAP, Verbose,
            TEXT("OnMapLoad('%s'): ClearRemoteExecGlobals returned an unreadable payload: %s"),
            *Filename, *Result);
        return;
    }

    if (!bOk)
    {
        // Nothing was cleared and nothing needed to be. The normal instance is editor startup:
        // no Python interpreter yet means no remote-exec globals dict, so nothing can be rooted
        // in it. Deliberately quiet, and deliberately worded as a no-op.
        UE_LOG(LogUAP, Verbose,
            TEXT("OnMapLoad('%s'): no sweep performed, ClearRemoteExecGlobals unavailable: %s"),
            *Filename, *Result);
        return;
    }

    if (!Parsed->TryGetNumberField(TEXT("count"), Count) || Count <= 0.0)
    {
        UE_LOG(LogUAP, Verbose, TEXT("OnMapLoad('%s'): no remote-exec globals to clear."), *Filename);
        return;
    }

    UE_LOG(LogUAP, Warning,
        TEXT("OnMapLoad('%s'): cleared Python remote-exec globals that could root a UObject: %s. ")
        TEXT("A stale UWorld reference there would have killed the editor with 'World Memory Leaks'."),
        *Filename, *Result);
}
