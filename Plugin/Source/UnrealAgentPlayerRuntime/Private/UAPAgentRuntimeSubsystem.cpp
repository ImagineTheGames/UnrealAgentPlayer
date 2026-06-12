#include "UAPAgentRuntimeSubsystem.h"

#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentInput.h"
#include "AgentLogCapture.h"
#include "AgentHelperDiscovery.h"
#include "AgentMotionController.h"
#include "AgentUIReader.h"
#include "AgentWorld.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "HAL/IConsoleManager.h"
#include "JsonObjectConverter.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UnrealClient.h"
#include "AgentRuntimeRCBootstrap.h"
#include "RemoteControlSettings.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"

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

void UUAPAgentRuntimeSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
    Super::Initialize(Collection);
    LogCapture = MakeShared<FAgentLogCapture>(4096);
    GLog->AddOutputDevice(LogCapture.Get());
    FAgentRuntimeRCBootstrap::Expose(this);

#if WITH_EDITOR
    // Per-instance RemoteControl port for standalone -game processes. The default
    // (30010) collides with the editor and with other instances; -UAPRCPort=N rebinds
    // the RC HTTP server to N. WebRemoteControl restarts the server on its
    // OnSettingChanged handler (active because -game from the editor binary is WITH_EDITOR).
    {
        int32 RCPort = 0;
        if (FParse::Value(FCommandLine::Get(), TEXT("UAPRCPort="), RCPort) && RCPort > 0)
        {
            URemoteControlSettings* RCSettings = GetMutableDefault<URemoteControlSettings>();
            if (RCSettings && RCSettings->RemoteControlHttpServerPort != (uint32)RCPort)
            {
                RCSettings->RemoteControlHttpServerPort = (uint32)RCPort;
                FProperty* Prop = URemoteControlSettings::StaticClass()->FindPropertyByName(TEXT("RemoteControlHttpServerPort"));
                FPropertyChangedEvent Evt(Prop);
                RCSettings->OnSettingChanged().Broadcast(RCSettings, Evt);
                UE_LOG(LogUAPRuntime, Log, TEXT("Agent RC HTTP port set to %d via -UAPRCPort."), RCPort);
            }
        }
    }
#endif

    UE_LOG(LogUAPRuntime, Log, TEXT("UAPAgentRuntimeSubsystem initialized."));
}

void UUAPAgentRuntimeSubsystem::Deinitialize()
{
    FAgentRuntimeRCBootstrap::Unexpose();
    if (LogCapture.IsValid())
    {
        GLog->RemoveOutputDevice(LogCapture.Get());
        LogCapture.Reset();
    }
    UE_LOG(LogUAPRuntime, Log, TEXT("UAPAgentRuntimeSubsystem deinitialized."));
    Super::Deinitialize();
}

FString UUAPAgentRuntimeSubsystem::GetPluginVersion() const
{
    return TEXT("0.0.1");
}

FString UUAPAgentRuntimeSubsystem::ExecuteConsoleCommand(FString Command)
{
    UWorld* World = FAgentWorld::GetActiveGameWorld();
    if (!World) { return TEXT("ERROR: No active game world"); }
    FUAPStringOutputDevice Ar;
    GEngine->Exec(World, *Command, Ar);
    return Ar.Buffer;
}

bool UUAPAgentRuntimeSubsystem::InjectKey(FString KeyName, bool bPressed, bool bRepeat)
{
    FKey Key(*KeyName);
    if (!Key.IsValid())
    {
        UE_LOG(LogUAPRuntime, Warning, TEXT("InjectKey: unknown key '%s'"), *KeyName);
        return false;
    }
    return FAgentInput::InjectKey(Key, bPressed, bRepeat);
}

bool UUAPAgentRuntimeSubsystem::InjectMouseMove(float X, float Y, bool bAbsolute)
{
    return FAgentInput::InjectMouseMove(FVector2D(X, Y), bAbsolute);
}

bool UUAPAgentRuntimeSubsystem::InjectMouseButton(EAgentMouseButton Button, bool bPressed)
{
    return FAgentInput::InjectMouseButton(Button, bPressed);
}

bool UUAPAgentRuntimeSubsystem::InjectAxis(FString AxisName, float Value)
{
    return FAgentInput::InjectAxis(FName(*AxisName), Value);
}

bool UUAPAgentRuntimeSubsystem::InjectGamepad(EAgentGamepadButton Button, bool bPressed, float AnalogValue)
{
    return FAgentInput::InjectGamepad(Button, bPressed, AnalogValue);
}

bool UUAPAgentRuntimeSubsystem::InjectXRButton(EAgentXRHand Hand, FString ButtonKeyName, bool bPressed)
{
    FKey Key(*ButtonKeyName);
    if (!Key.IsValid())
    {
        UE_LOG(LogUAPRuntime, Warning, TEXT("InjectXRButton: unknown key '%s'"), *ButtonKeyName);
        return false;
    }
    return FAgentInput::InjectKey(Key, bPressed, false);
}

bool UUAPAgentRuntimeSubsystem::InjectXRControllerPose(EAgentXRHand Hand, FVector Position, FRotator Orientation, bool bTracked)
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

bool UUAPAgentRuntimeSubsystem::ClearXRControllerOverride(EAgentXRHand Hand)
{
    FUnrealAgentPlayerRuntimeModule* Rtm = FUnrealAgentPlayerRuntimeModule::Get();
    FAgentMotionController* MC = Rtm ? Rtm->GetMotionController() : nullptr;
    if (!MC) { return false; }
    MC->ClearPose(Hand);
    return true;
}

FString UUAPAgentRuntimeSubsystem::DumpViewportUI()
{
    return FAgentUIReader::DumpViewportUI();
}

bool UUAPAgentRuntimeSubsystem::CaptureViewportWithUI(FString Filename)
{
    if (Filename.IsEmpty())
    {
        return false;
    }
    if (IConsoleVariable* CVar = IConsoleManager::Get().FindConsoleVariable(TEXT("t.IdleWhenNotForeground")))
    {
        CVar->Set(0, ECVF_SetByCode);
    }
    FScreenshotRequest::RequestScreenshot(Filename, /*bShowUI=*/true, /*bAddUniqueSuffix=*/false);
    return true;
}

int64 UUAPAgentRuntimeSubsystem::GetLogCursor() const
{
    return LogCapture.IsValid() ? LogCapture->GetCursor() : 0;
}

FString UUAPAgentRuntimeSubsystem::GetLogsSince(
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

void UUAPAgentRuntimeSubsystem::RefreshHelperCache()
{
    FAgentHelperDiscovery::RescanAll(HelperCache);
}

TArray<FAgentHelperDescriptor> UUAPAgentRuntimeSubsystem::ListTestHelpers()
{
    if (HelperCache.Num() == 0)
    {
        RefreshHelperCache();
    }
    return HelperCache;
}

FString UUAPAgentRuntimeSubsystem::CallTestHelper(FString Name, FString JsonArgs)
{
    UClass* Cls = nullptr;
    UFunction* Fn = FAgentHelperDiscovery::Resolve(Name, Cls);
    if (!Fn)
    {
        return TEXT(R"({"ok":false,"error":{"code":"HELPER_UNKNOWN","message":"helper not found"}})");
    }

    // Runtime: no PIE phase gating — game is always "Playing" when this subsystem is live.
    // Still respect the Phase metadata to catch misuse (e.g. editor-only helpers called at runtime).
    // UFunction metadata is editor-only (WITH_EDITORONLY_DATA); this DeveloperTool module also
    // compiles into non-editor Development builds where the metadata API does not exist - there
    // is no metadata to read there, so default to "Any". Matches AgentHelperDiscovery.cpp guard.
#if WITH_EDITORONLY_DATA
    const FString Phase = Fn->HasMetaData(TEXT("Phase")) ? Fn->GetMetaData(TEXT("Phase")) : TEXT("Any");
#else
    const FString Phase = TEXT("Any");
#endif
    if (Phase == TEXT("NotPlaying"))
    {
        return FString::Printf(
            TEXT(R"({"ok":false,"error":{"code":"PIE_WRONG_PHASE","message":"helper requires NotPlaying phase; runtime game is always Playing"}})"));
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
