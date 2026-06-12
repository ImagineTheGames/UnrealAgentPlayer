#include "UAPAgentSubsystem.h"

#include "UnrealAgentPlayerModule.h"
#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentRemoteControlBootstrap.h"
#include "Editor.h"
#include "HAL/PlatformTime.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "Kismet/KismetSystemLibrary.h"
#include "Misc/OutputDevice.h"
#include "AgentInput.h"
#include "AgentLogCapture.h"
#include "UAPAgentSettings.h"
#include "AgentHelperDiscovery.h"
#include "JsonObjectConverter.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "RHIGlobals.h"
#include "RenderTimer.h"
#include "AgentMotionController.h"
#include "AgentUIReader.h"
#include "UnrealClient.h"
#include "HAL/IConsoleManager.h"
#include "Framework/Application/SlateApplication.h"
#include "Widgets/SWindow.h"
#include "GenericPlatform/GenericWindow.h"
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
    HPostPIEStarted = FEditorDelegates::PostPIEStarted.AddUObject(this, &UUAPAgentSubsystem::OnPostPIEStarted);
    HPrePIEEnded   = FEditorDelegates::PrePIEEnded.AddUObject(this, &UUAPAgentSubsystem::OnPrePIEEnded);
    HEndPIE        = FEditorDelegates::EndPIE.AddUObject(this, &UUAPAgentSubsystem::OnEndPIE);
    HPausePIE      = FEditorDelegates::PausePIE.AddUObject(this, &UUAPAgentSubsystem::OnPausePIE);
    HResumePIE     = FEditorDelegates::ResumePIE.AddUObject(this, &UUAPAgentSubsystem::OnResumePIE);
    HCancelPIE     = FEditorDelegates::CancelPIE.AddUObject(this, &UUAPAgentSubsystem::OnCancelPIE);
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
    FAgentRemoteControlBootstrap::Unexpose();
    if (LogCapture.IsValid())
    {
        GLog->RemoveOutputDevice(LogCapture.Get());
        LogCapture.Reset();
    }
    FEditorDelegates::PostPIEStarted.Remove(HPostPIEStarted);
    FEditorDelegates::PrePIEEnded.Remove(HPrePIEEnded);
    FEditorDelegates::EndPIE.Remove(HEndPIE);
    FEditorDelegates::PausePIE.Remove(HPausePIE);
    FEditorDelegates::ResumePIE.Remove(HResumePIE);
    FEditorDelegates::CancelPIE.Remove(HCancelPIE);
    UE_LOG(LogUAP, Log, TEXT("UAPAgentSubsystem deinitialized."));
    Super::Deinitialize();
}

FString UUAPAgentSubsystem::GetPluginVersion() const
{
    return TEXT("0.0.1");
}

FString UUAPAgentSubsystem::ExecuteConsoleCommand(FString Command)
{
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

bool UUAPAgentSubsystem::FocusEditorWindow()
{
#if PLATFORM_WINDOWS
    if (!FSlateApplication::IsInitialized()) { return false; }
    FSlateApplication& App = FSlateApplication::Get();

    // Pick the main editor window: the regular top-level window whose title ends in
    // "Unreal Editor" (format "<Project> - Unreal Editor"). GetActiveTopLevelWindow can
    // return a transient dialog (e.g. Restore Packages), so title-match first and fall
    // back to the active window, then to the first regular window.
    TSharedPtr<SWindow> Win;
    for (const TSharedRef<SWindow>& W : App.GetTopLevelWindows())
    {
        if (W->IsRegularWindow() && W->GetTitle().ToString().Contains(TEXT("Unreal Editor")))
        {
            Win = W;
            break;
        }
    }
    if (!Win.IsValid()) { Win = App.GetActiveTopLevelWindow(); }
    if (!Win.IsValid() || !Win->IsRegularWindow())
    {
        for (const TSharedRef<SWindow>& W : App.GetTopLevelWindows())
        {
            if (W->IsRegularWindow()) { Win = W; break; }
        }
    }
    if (!Win.IsValid()) { return false; }

    TSharedPtr<FGenericWindow> Native = Win->GetNativeWindow();
    HWND Hwnd = Native.IsValid() ? reinterpret_cast<HWND>(Native->GetOSWindowHandle()) : nullptr;
    if (!Hwnd) { return false; }

    // Raise Z-order first (TOPMOST/NOTOPMOST bounce) - always works in-process, focus or not.
    ::ShowWindow(Hwnd, SW_RESTORE);
    ::SetWindowPos(Hwnd, HWND_TOPMOST,   0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
    ::SetWindowPos(Hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE);
    ::BringWindowToTop(Hwnd);

    // Grabbing OS keyboard focus while another app is foreground is normally blocked by
    // Windows' foreground lock. Attaching our input queue to the current foreground
    // thread's queue (and AllowSetForegroundWindow) lets SetForegroundWindow succeed.
    // Combined with SPI_SETFOREGROUNDLOCKTIMEOUT=0 set at module startup.
    const HWND  ForegroundWnd = ::GetForegroundWindow();
    const DWORD ForegroundTid = ForegroundWnd ? ::GetWindowThreadProcessId(ForegroundWnd, nullptr) : 0;
    const DWORD ThisTid       = ::GetCurrentThreadId();
    const bool  bAttach       = ForegroundTid != 0 && ForegroundTid != ThisTid;
    // Use 1/0, not TRUE/FALSE: those Win32 macros are undef'd by HideWindowsPlatformTypes.
    if (bAttach) { ::AttachThreadInput(ThisTid, ForegroundTid, 1); }
    ::AllowSetForegroundWindow(ASFW_ANY);
    const bool bFg = ::SetForegroundWindow(Hwnd) != 0;
    if (bAttach) { ::AttachThreadInput(ThisTid, ForegroundTid, 0); }
    return bFg;
#else
    return false;
#endif
}

void UUAPAgentSubsystem::OnPostPIEStarted(bool) { CurrentPhase = EAgentPIEPhase::Playing; PIEStartTimeSeconds = FPlatformTime::Seconds(); }
void UUAPAgentSubsystem::OnPrePIEEnded(bool)    { CurrentPhase = EAgentPIEPhase::Ending; }
void UUAPAgentSubsystem::OnEndPIE(bool)         { CurrentPhase = EAgentPIEPhase::NotPlaying; PIEStartTimeSeconds = 0.0; }
void UUAPAgentSubsystem::OnPausePIE(bool)       { CurrentPhase = EAgentPIEPhase::Paused; }
void UUAPAgentSubsystem::OnResumePIE(bool)      { CurrentPhase = EAgentPIEPhase::Playing; }
void UUAPAgentSubsystem::OnCancelPIE()          { CurrentPhase = EAgentPIEPhase::NotPlaying; PIEStartTimeSeconds = 0.0; }

EAgentPIEPhase UUAPAgentSubsystem::GetPIEPhase() const { return CurrentPhase; }

double UUAPAgentSubsystem::GetPIEElapsedSeconds() const
{
    if (PIEStartTimeSeconds <= 0.0) { return 0.0; }
    return FPlatformTime::Seconds() - PIEStartTimeSeconds;
}

int64 UUAPAgentSubsystem::GetLogCursor() const
{
    return LogCapture.IsValid() ? LogCapture->GetCursor() : 0;
}

FString UUAPAgentSubsystem::GetLogsSince(
    int64 AfterCursor, int32 MaxLines, FString CategoryFilter, EAgentLogVerbosity MinVerbosity) const
{
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

bool UUAPAgentSubsystem::InjectKey(FString KeyName, bool bPressed, bool bRepeat)
{
    FKey Key(*KeyName);
    if (!Key.IsValid())
    {
        UE_LOG(LogUAP, Warning, TEXT("InjectKey: unknown key '%s'"), *KeyName);
        return false;
    }
    return FAgentInput::InjectKey(Key, bPressed, bRepeat);
}

bool UUAPAgentSubsystem::InjectMouseMove(float X, float Y, bool bAbsolute)
{
    return FAgentInput::InjectMouseMove(FVector2D(X, Y), bAbsolute);
}

bool UUAPAgentSubsystem::InjectMouseButton(EAgentMouseButton Button, bool bPressed)
{
    return FAgentInput::InjectMouseButton(Button, bPressed);
}

bool UUAPAgentSubsystem::InjectAxis(FString AxisName, float Value)
{
    return FAgentInput::InjectAxis(FName(*AxisName), Value);
}

bool UUAPAgentSubsystem::InjectGamepad(EAgentGamepadButton Button, bool bPressed, float AnalogValue)
{
    return FAgentInput::InjectGamepad(Button, bPressed, AnalogValue);
}

bool UUAPAgentSubsystem::InjectXRButton(EAgentXRHand Hand, FString ButtonKeyName, bool bPressed)
{
    // Quest Touch buttons are regular FKeys (e.g. OculusTouch_Left_X_Click). Route via Slate.
    FKey Key(*ButtonKeyName);
    if (!Key.IsValid())
    {
        UE_LOG(LogUAP, Warning, TEXT("InjectXRButton: unknown key '%s'"), *ButtonKeyName);
        return false;
    }
    return FAgentInput::InjectKey(Key, bPressed, false);
}

bool UUAPAgentSubsystem::InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked)
{
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
    FUnrealAgentPlayerRuntimeModule* Rtm = FUnrealAgentPlayerRuntimeModule::Get();
    FAgentMotionController* MC = Rtm ? Rtm->GetMotionController() : nullptr;
    if (!MC) { return false; }
    MC->ClearPose(Hand);
    return true;
}

FString UUAPAgentSubsystem::DumpViewportUI()
{
    return FAgentUIReader::DumpViewportUI();
}

bool UUAPAgentSubsystem::CaptureViewportWithUI(FString Filename)
{
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

    // bShowUI=true reads back the composited backbuffer (3D scene + UMG/Slate), unlike
    // HighResShot (scene only). The filename overload reliably processes the request
    // in-editor and writes the PNG on the next rendered frame; the caller polls for it.
    // NOTE: in embedded PIE the backbuffer is the whole editor window, so the shot
    // includes editor chrome around the game viewport. Cropping to just the viewport is
    // a known follow-up (best done in the MCP layer to avoid engine screenshot-timing
    // fragility).
    FScreenshotRequest::RequestScreenshot(Filename, /*bShowUI=*/true, /*bAddUniqueSuffix=*/false);
    return true;
}

void UUAPAgentSubsystem::RefreshHelperCache()
{
    FAgentHelperDiscovery::RescanAll(HelperCache);
}

TArray<FAgentHelperDescriptor> UUAPAgentSubsystem::ListTestHelpers()
{
    if (HelperCache.Num() == 0)
    {
        RefreshHelperCache();
    }
    return HelperCache;
}

FString UUAPAgentSubsystem::CallTestHelper(FString Name, FString JsonArgs)
{
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
    if (!GEngine) { return TEXT(""); }

    // GRenderThreadTime / GGPUFrameTime are platform cycle counts updated each frame.
    const double RenderMs = FPlatformTime::ToMilliseconds(GRenderThreadTime);
    const double GpuMs    = FPlatformTime::ToMilliseconds(GGPUFrameTime);

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
