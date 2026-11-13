#include "UAPAgentLevelSubsystem.h"

#include "UnrealAgentPlayerModule.h"
#include "UAPAgentSubsystem.h"
#include "AgentRemoteControlBootstrap.h"
#include "AgentActivity.h"
#include "Editor.h"
#include "FileHelpers.h"
#include "Engine/World.h"
#include "HAL/FileManager.h"
#include "HAL/PlatformTime.h"
#include "Interfaces/IPluginManager.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "AssetRegistry/AssetData.h"
#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetRegistry/IAssetRegistry.h"
#include "IPythonScriptPlugin.h"
#include "PythonScriptTypes.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Modules/ModuleManager.h"

namespace
{
    // The pause between the RemoteControl reply and the load. Long enough for the HTTP reply to
    // flush; the point is only that the load runs on a LATER ticker pass than whatever call
    // queued it (Python remote-exec included), never nested inside it.
    constexpr float GLevelOpenDelaySeconds = 0.25f;
    constexpr int32 GMaxDirtyNames = 8;

    FString UAPLevelToJson(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, W);
        return Out;
    }

    FString UAPLevelError(const FString& Error)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("ok"), false);
        O->SetStringField(TEXT("error"), Error);
        return UAPLevelToJson(O);
    }

    TArray<TSharedPtr<FJsonValue>> UAPLevelStrings(const TArray<FString>& In)
    {
        TArray<TSharedPtr<FJsonValue>> Out;
        for (const FString& S : In) { Out.Add(MakeShared<FJsonValueString>(S)); }
        return Out;
    }

    // Python's own garbage. ClearRemoteExecGlobals drops what the globals dict references; a
    // reference cycle that holds a UObject wrapper survives that until the cycle collector runs.
    // Run it BEFORE the load so no wrapper from an earlier call can outlive the old world.
    void UAPLevelPythonCollect()
    {
        IPythonScriptPlugin* Python = IPythonScriptPlugin::Get();
        if (!Python || !Python->IsPythonAvailable() || !Python->IsPythonInitialized()) { return; }
        FPythonCommandEx Cmd;
        Cmd.ExecutionMode = EPythonCommandExecutionMode::EvaluateStatement;
        Cmd.Flags = EPythonCommandFlags::Unattended;
        Cmd.Command = TEXT("__import__('gc').collect()");
        Python->ExecPythonCommandEx(Cmd);
    }

    UWorld* UAPLevelEditorWorld()
    {
        return GEditor ? GEditor->GetEditorWorldContext().World() : nullptr;
    }

    // A content-only project plugin named like the package's mount root (Dream plugins live under
    // Plugins/Dreams/<Name>/<Name>.uplugin). Same mount the game's own Dreams subsystem performs
    // for the editor: add to the plugin list, then mount. Refuses a plugin with C++ modules: that
    // cannot be mounted late and a half-mounted one would be worse than a refusal.
    bool UAPLevelMountContentPlugin(const FString& Root, FString& OutError)
    {
        IPluginManager& PM = IPluginManager::Get();
        TSharedPtr<IPlugin> Plugin = PM.FindPlugin(Root);
        if (!Plugin.IsValid())
        {
            TArray<FString> Found;
            IFileManager::Get().FindFilesRecursive(Found, *FPaths::ProjectPluginsDir(),
                *(Root + TEXT(".uplugin")), /*Files=*/true, /*Directories=*/false);
            if (Found.Num() != 1)
            {
                OutError = Found.Num() == 0
                    ? FString::Printf(TEXT("mount root '/%s/' is not mounted and no project plugin '%s.uplugin' was found under %s"),
                        *Root, *Root, *FPaths::ProjectPluginsDir())
                    : FString::Printf(TEXT("mount root '/%s/' is not mounted and %d plugins named '%s' were found; pass a mounted map"),
                        *Root, Found.Num(), *Root);
                return false;
            }
            FText Why;
            if (!PM.AddToPluginsList(Found[0], &Why))
            {
                OutError = FString::Printf(TEXT("could not add plugin %s: %s"), *Found[0], *Why.ToString());
                return false;
            }
            Plugin = PM.FindPlugin(Root);
        }
        if (!Plugin.IsValid() || !Plugin->CanContainContent())
        {
            OutError = FString::Printf(TEXT("plugin '%s' cannot contain content"), *Root);
            return false;
        }
        if (Plugin->GetDescriptor().Modules.Num() > 0)
        {
            OutError = FString::Printf(TEXT("plugin '%s' has C++ modules and cannot be mounted late; enable it and restart the editor"), *Root);
            return false;
        }
        PM.MountNewlyCreatedPlugin(Root);
        return true;
    }

    // Resolve what the caller typed to {long package name, .umap filename}.
    bool UAPLevelResolve(const FString& InMap, FString& OutPackage, FString& OutFilename,
                         FString& OutMountedPlugin, FString& OutError)
    {
        FString Map = InMap.TrimStartAndEnd();
        if (Map.IsEmpty()) { OutError = TEXT("no map given"); return false; }

        if (Map.StartsWith(TEXT("/")))
        {
            // /Pkg/Path/Map or the object path /Pkg/Path/Map.Map
            int32 Dot = INDEX_NONE;
            if (Map.FindLastChar(TEXT('.'), Dot)) { Map.LeftInline(Dot); }
            OutPackage = Map;
        }
        else
        {
            // A bare asset name: it must be a World asset, and unique.
            IAssetRegistry& AR = FModuleManager::LoadModuleChecked<FAssetRegistryModule>("AssetRegistry").Get();
            TArray<FAssetData> Worlds;
            AR.GetAssetsByClass(UWorld::StaticClass()->GetClassPathName(), Worlds);
            TArray<FString> Hits;
            for (const FAssetData& D : Worlds)
            {
                if (D.AssetName.ToString().Equals(Map, ESearchCase::IgnoreCase))
                {
                    Hits.AddUnique(D.PackageName.ToString());
                }
            }
            if (Hits.Num() == 0)
            {
                OutError = FString::Printf(TEXT("no map named '%s' in the asset registry; pass the full package path (e.g. /Game/Maps/%s). A Dream map whose plugin is not mounted yet is not listed: pass /<Plugin>/Maps/<Map>"), *Map, *Map);
                return false;
            }
            if (Hits.Num() > 1)
            {
                OutError = FString::Printf(TEXT("'%s' is ambiguous: %s. Pass the full package path."), *Map, *FString::Join(Hits, TEXT(", ")));
                return false;
            }
            OutPackage = Hits[0];
        }

        FText Reason;
        if (!FPackageName::IsValidLongPackageName(OutPackage, /*bIncludeReadOnlyRoots=*/true, &Reason))
        {
            OutError = FString::Printf(TEXT("'%s' is not a valid package path: %s"), *OutPackage, *Reason.ToString());
            return false;
        }

        FString Converted;
        if (!FPackageName::TryConvertLongPackageNameToFilename(OutPackage, Converted, FPackageName::GetMapPackageExtension()))
        {
            // The root is not mounted. Handle the Dream-plugin case rather than failing.
            FString Root, Rest;
            OutPackage.RightChop(1).Split(TEXT("/"), &Root, &Rest);
            FString MountError;
            if (Root.IsEmpty() || !UAPLevelMountContentPlugin(Root, MountError))
            {
                OutError = MountError.IsEmpty() ? FString::Printf(TEXT("'%s' has no mount root"), *OutPackage) : MountError;
                return false;
            }
            OutMountedPlugin = Root;
            if (!FPackageName::TryConvertLongPackageNameToFilename(OutPackage, Converted, FPackageName::GetMapPackageExtension()))
            {
                OutError = FString::Printf(TEXT("plugin '%s' mounted but '%s' still does not resolve to a file"), *Root, *OutPackage);
                return false;
            }
        }

        if (!IFileManager::Get().FileExists(*Converted))
        {
            OutError = FString::Printf(TEXT("map file not found: %s (package %s)"), *Converted, *OutPackage);
            return false;
        }
        OutFilename = FPaths::ConvertRelativePathToFull(Converted);
        return true;
    }
}

void UUAPAgentLevelSubsystem::Initialize(FSubsystemCollectionBase& Collection)
{
    Super::Initialize(Collection);
    // Same RemoteControl preset as UUAPAgentSubsystem (UAP_Preset), so the CLI calls these
    // verbs exactly like every other one.
    FAgentRemoteControlBootstrap::Expose(this);
    UE_LOG(LogUAP, Log, TEXT("UAPAgentLevelSubsystem initialized."));
}

void UUAPAgentLevelSubsystem::Deinitialize()
{
    if (TickHandle.IsValid())
    {
        FTSTicker::GetCoreTicker().RemoveTicker(TickHandle);
        TickHandle.Reset();
    }
    Super::Deinitialize();
}

FString UUAPAgentLevelSubsystem::CurrentMapPackage() const
{
    const UWorld* W = UAPLevelEditorWorld();
    return W ? W->GetOutermost()->GetName() : FString();
}

bool UUAPAgentLevelSubsystem::IsMapDirty(TArray<FString>& OutNames) const
{
    TArray<UPackage*> Dirty;
    FEditorFileUtils::GetDirtyWorldPackages(Dirty);
    if (const UWorld* W = UAPLevelEditorWorld())
    {
        UPackage* Pkg = W->GetOutermost();
        if (Pkg && Pkg->IsDirty()) { Dirty.AddUnique(Pkg); }
    }
    for (const UPackage* P : Dirty)
    {
        if (OutNames.Num() < GMaxDirtyNames) { OutNames.Add(P->GetName()); }
    }
    return Dirty.Num() > 0;
}

FString UUAPAgentLevelSubsystem::GetCurrentLevel()
{
    UAP_ACTIVITY(TEXT("level current"), FString());
    UWorld* W = UAPLevelEditorWorld();
    if (!W) { return UAPLevelError(TEXT("no editor world")); }

    TArray<FString> DirtyNames;
    const bool bDirty = IsMapDirty(DirtyNames);

    FString Filename;
    FPackageName::TryConvertLongPackageNameToFilename(W->GetOutermost()->GetName(), Filename, FPackageName::GetMapPackageExtension());

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("map"), W->GetOutermost()->GetName());
    O->SetStringField(TEXT("world"), W->GetName());
    O->SetStringField(TEXT("filename"), Filename.IsEmpty() ? Filename : FPaths::ConvertRelativePathToFull(Filename));
    O->SetBoolField(TEXT("dirty"), bDirty);
    O->SetArrayField(TEXT("dirty_packages"), UAPLevelStrings(DirtyNames));
    O->SetBoolField(TEXT("pie_in_progress"), GEditor && GEditor->IsPlaySessionInProgress());
    O->SetStringField(TEXT("open_state"), State);
    return UAPLevelToJson(O);
}

FString UUAPAgentLevelSubsystem::OpenLevel(FString Map, bool bDiscard)
{
    UAP_ACTIVITY(TEXT("level open"), Map);
    if (!GEditor) { return UAPLevelError(TEXT("no editor")); }

    if (State == TEXT("pending"))
    {
        return UAPLevelError(FString::Printf(TEXT("level open request %d (%s) is still pending; poll GetLevelOpenStatus"), RequestSeq, *ReqPackage));
    }
    if (GEditor->IsPlaySessionInProgress())
    {
        return UAPLevelError(TEXT("PIE is running or queued: refusing to swap the editor level under it. `uap pie stop` first."));
    }

    FString Package, Filename, Mounted, Error;
    if (!UAPLevelResolve(Map, Package, Filename, Mounted, Error)) { return UAPLevelError(Error); }

    TArray<FString> DirtyNames;
    if (!bDiscard && IsMapDirty(DirtyNames))
    {
        return UAPLevelError(FString::Printf(
            TEXT("the open map '%s' has unsaved changes (%s). Opening '%s' would lose them. Nothing was saved or loaded. Pass --discard to throw them away, or save first."),
            *CurrentMapPackage(), *FString::Join(DirtyNames, TEXT(", ")), *Package));
    }

    ++RequestSeq;
    State = TEXT("pending");
    ReqPackage = Package;
    ReqFilename = Filename;
    bReqDiscard = bDiscard;
    MapBefore = CurrentMapPackage();
    ResultJson.Empty();
    ClearedGlobals.Empty();

    if (TickHandle.IsValid()) { FTSTicker::GetCoreTicker().RemoveTicker(TickHandle); }
    TickHandle = FTSTicker::GetCoreTicker().AddTicker(
        FTickerDelegate::CreateUObject(this, &UUAPAgentLevelSubsystem::TickOpen), GLevelOpenDelaySeconds);

    UE_LOG(LogUAP, Log, TEXT("level open #%d queued: %s (%s) discard=%d"), RequestSeq, *Package, *Filename, bDiscard ? 1 : 0);

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetBoolField(TEXT("queued"), true);
    O->SetNumberField(TEXT("request"), RequestSeq);
    O->SetStringField(TEXT("map"), Package);
    O->SetStringField(TEXT("filename"), Filename);
    O->SetBoolField(TEXT("discard"), bDiscard);
    if (!Mounted.IsEmpty()) { O->SetStringField(TEXT("mounted_plugin"), Mounted); }
    return UAPLevelToJson(O);
}

bool UUAPAgentLevelSubsystem::TickOpen(float /*DeltaTime*/)
{
    // One-shot. Runs from the core ticker: not inside the RemoteControl handler that queued it
    // and not inside any Python frame.
    TickHandle.Reset();
    if (State != TEXT("pending")) { return false; }

    // The world can have changed in the quarter second since the request was accepted. Re-check
    // both refusals now, because acting on a stale OK is how a map gets thrown away.
    if (GEditor && GEditor->IsPlaySessionInProgress())
    {
        FinishOpen(false, TEXT("PIE started before the load ran; level not changed"), CurrentMapPackage(), 0.0);
        return false;
    }
    TArray<FString> DirtyNames;
    if (!bReqDiscard && IsMapDirty(DirtyNames))
    {
        FinishOpen(false, FString::Printf(TEXT("the open map became dirty before the load ran (%s); level not changed"),
            *FString::Join(DirtyNames, TEXT(", "))), CurrentMapPackage(), 0.0);
        return false;
    }

    // Flush every reference Python could still hold to the outgoing world: drop the remote-exec
    // globals (the same sweep the OnMapLoad hook does, but here we are NOT inside a Python
    // frame), then run Python's cycle collector.
    if (UUAPAgentSubsystem* Agent = GEditor ? GEditor->GetEditorSubsystem<UUAPAgentSubsystem>() : nullptr)
    {
        ClearedGlobals = Agent->ClearRemoteExecGlobals();
    }
    UAPLevelPythonCollect();

    const double T0 = FPlatformTime::Seconds();
    bool bLoaded = false;
    {
        // No modal dialog may ever open under a headless agent.
        TGuardValue<bool> Unattended(GIsRunningUnattendedScript, true);
        bLoaded = FEditorFileUtils::LoadMap(ReqFilename, /*LoadAsTemplate=*/false, /*bShowProgress=*/true);
    }
    const double Seconds = FPlatformTime::Seconds() - T0;

    const FString After = CurrentMapPackage();
    if (!bLoaded)
    {
        FinishOpen(false, FString::Printf(TEXT("FEditorFileUtils::LoadMap returned false for %s; editor is on '%s'"), *ReqFilename, *After), After, Seconds);
    }
    else if (!After.Equals(ReqPackage, ESearchCase::IgnoreCase))
    {
        FinishOpen(false, FString::Printf(TEXT("loaded, but the editor world is '%s', not the requested '%s'"), *After, *ReqPackage), After, Seconds);
    }
    else
    {
        FinishOpen(true, FString(), After, Seconds);
    }
    return false;
}

void UUAPAgentLevelSubsystem::FinishOpen(bool bOk, const FString& Error, const FString& MapAfter, double LoadSeconds)
{
    State = bOk ? TEXT("done") : TEXT("failed");

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), bOk);
    O->SetStringField(TEXT("state"), State);
    O->SetNumberField(TEXT("request"), RequestSeq);
    O->SetStringField(TEXT("map"), ReqPackage);
    O->SetStringField(TEXT("map_before"), MapBefore);
    O->SetStringField(TEXT("map_after"), MapAfter);
    O->SetNumberField(TEXT("load_seconds"), LoadSeconds);
    O->SetBoolField(TEXT("discard"), bReqDiscard);
    if (!ClearedGlobals.IsEmpty()) { O->SetStringField(TEXT("cleared_globals"), ClearedGlobals); }
    if (!Error.IsEmpty()) { O->SetStringField(TEXT("error"), Error); }
    ResultJson = UAPLevelToJson(O);

    if (bOk) { UE_LOG(LogUAP, Log, TEXT("level open #%d done: %s in %.2fs"), RequestSeq, *MapAfter, LoadSeconds); }
    else { UE_LOG(LogUAP, Warning, TEXT("level open #%d FAILED: %s"), RequestSeq, *Error); }
}

FString UUAPAgentLevelSubsystem::GetLevelOpenStatus()
{
    if (!ResultJson.IsEmpty() && State != TEXT("pending")) { return ResultJson; }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("state"), State);
    O->SetNumberField(TEXT("request"), RequestSeq);
    if (State == TEXT("pending")) { O->SetStringField(TEXT("map"), ReqPackage); }
    return UAPLevelToJson(O);
}
