#pragma once

#include "CoreMinimal.h"
#include "EditorSubsystem.h"
#include "Containers/Ticker.h"
#include "UAPAgentLevelSubsystem.generated.h"

// SAFE level changes for agents: `uap level open <map>` / `uap level current`.
//
// WHY THIS EXISTS. `ULevelEditorSubsystem.load_level` called from editor Python runs the whole map
// load (and the garbage collection inside it) NESTED INSIDE a live Python frame. Everything that
// frame holds -- locals, temporaries, wrappers of the outgoing world and its actors -- is rooted by
// FPyReferenceCollector while the old world is torn down, and the interpreter is re-entered by any
// delegate that fires mid-load. That has killed the SHARED editor repeatedly:
//   "World Memory Leaks: 1 leaks objects and packages"          (EditorServer.cpp:1938, x3)
//   "Assertion failed: IsValidLowLevel() [Obj.cpp:1312]"         (2026-11-13, GC inside Map_Load,
//                                                                 python311 frames on the stack)
// These verbs never load from Python. OpenLevel only QUEUES the request and returns; a core-ticker
// callback then runs on the game thread OUTSIDE any Python frame, sweeps the remote-exec globals,
// collects Python's own garbage, and only then calls FEditorFileUtils::LoadMap. [ClickUp 86akezbrp]
UCLASS()
class UNREALAGENTPLAYER_API UUAPAgentLevelSubsystem : public UEditorSubsystem
{
    GENERATED_BODY()

public:
    virtual void Initialize(FSubsystemCollectionBase& Collection) override;
    virtual void Deinitialize() override;

    // The map the editor has open, read back from the live editor world:
    // {"ok":true,"map":"/Game/.../L_X","world":"L_X","filename":"...","dirty":bool,
    //  "dirty_packages":[..up to 8..],"pie_in_progress":bool,"open_state":"idle|pending|done|failed"}
    UFUNCTION(BlueprintCallable, Category="Agent|Level")
    FString GetCurrentLevel();

    // QUEUES a safe level change and returns at once:
    // {"ok":true,"queued":true,"request":N,"map":"/Pkg/Path/Map","filename":"...", "mounted_plugin"?:".."}
    // or {"ok":false,"error":"..."}. Poll GetLevelOpenStatus() for the result (the game thread is
    // busy for the whole load, so the poll simply waits).
    //
    // Map: a long package name (/Game/Maps/L_X, /Game/Maps/L_X.L_X) or a bare map asset name
    // (L_X) that is unique in the asset registry. A package whose mount root is not mounted is
    // resolved to a CONTENT-ONLY project plugin of the same name (Dream plugins) and mounted.
    //
    // Refuses: PIE live or queued; a request already pending; a dirty map unless bDiscard. It
    // NEVER saves. bDiscard is a bool whose zero-initialised default (omitted by RemoteControl)
    // is the safe one: false.
    UFUNCTION(BlueprintCallable, Category="Agent|Level")
    FString OpenLevel(FString Map, bool bDiscard);

    // {"state":"idle|pending|done|failed","request":N,"map":..,"error"?:..,"map_before":..,
    //  "map_after":..,"load_seconds":..,"cleared_globals":[..]}
    UFUNCTION(BlueprintCallable, Category="Agent|Level")
    FString GetLevelOpenStatus();

private:
    bool TickOpen(float DeltaTime);
    void FinishOpen(bool bOk, const FString& Error, const FString& MapAfter, double LoadSeconds);

    FString CurrentMapPackage() const;
    // True when the open map (or any world package) has unsaved changes; names the first few.
    bool IsMapDirty(TArray<FString>& OutNames) const;

    FTSTicker::FDelegateHandle TickHandle;

    int32 RequestSeq = 0;
    FString State = TEXT("idle");
    FString ReqPackage;
    FString ReqFilename;
    bool bReqDiscard = false;
    FString MapBefore;
    FString ResultJson;      // the last finished request, as the status payload
    FString ClearedGlobals;  // the sweep payload of the pending/last request
};
