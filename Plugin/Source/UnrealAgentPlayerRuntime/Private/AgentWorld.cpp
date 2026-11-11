#include "AgentWorld.h"
#include "Algo/Sort.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "Engine/GameViewportClient.h"
#include "Widgets/SWindow.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "GameFramework/PlayerController.h"
#include "GameFramework/Pawn.h"

namespace
{
    // The world an FScope made current. Weak: a PIE world torn down under a live scope must
    // read as "no override", never dangle. Game thread only, like every other caller here.
    TWeakObjectPtr<UWorld> GOverrideWorld;

    const TCHAR* UAPNetModeName(ENetMode Mode)
    {
        switch (Mode)
        {
            case NM_Standalone:      return TEXT("Standalone");
            case NM_DedicatedServer: return TEXT("DedicatedServer");
            case NM_ListenServer:    return TEXT("ListenServer");
            case NM_Client:          return TEXT("Client");
            default:                 return TEXT("Unknown");
        }
    }
}

FAgentWorld::FScope::FScope(UWorld* World)
    : Previous(GOverrideWorld)
{
    GOverrideWorld = World;
}

FAgentWorld::FScope::~FScope()
{
    GOverrideWorld = Previous;
}

UWorld* FAgentWorld::GetOverrideWorld()
{
    return GOverrideWorld.Get();
}

void FAgentWorld::GetAllGameWorlds(TArray<UWorld*>& Out)
{
    Out.Reset();
    if (!GEngine) { return; }

    TArray<const FWorldContext*> Pie;
    for (const FWorldContext& Ctx : GEngine->GetWorldContexts())
    {
        if (Ctx.WorldType == EWorldType::PIE && Ctx.World()) { Pie.Add(&Ctx); }
    }
    Algo::Sort(Pie, [](const FWorldContext* A, const FWorldContext* B) { return A->PIEInstance < B->PIEInstance; });
    for (const FWorldContext* Ctx : Pie) { Out.Add(Ctx->World()); }

    for (const FWorldContext& Ctx : GEngine->GetWorldContexts())
    {
        if (Ctx.WorldType == EWorldType::Game && Ctx.World()) { Out.Add(Ctx.World()); }
    }
}

int32 FAgentWorld::GetClientIndex(const UWorld* World)
{
    if (!GEngine || !World) { return INDEX_NONE; }
    for (const FWorldContext& Ctx : GEngine->GetWorldContexts())
    {
        if (Ctx.World() == World && Ctx.WorldType == EWorldType::PIE) { return Ctx.PIEInstance; }
    }
    return INDEX_NONE;
}

UWorld* FAgentWorld::GetActiveGameWorld()
{
    if (UWorld* Override = GOverrideWorld.Get()) { return Override; }
    if (!GEngine) { return nullptr; }
    UWorld* GameWorld = nullptr;
    for (const FWorldContext& Ctx : GEngine->GetWorldContexts())
    {
        if (Ctx.WorldType == EWorldType::PIE && Ctx.World()) { return Ctx.World(); }
        if (Ctx.WorldType == EWorldType::Game && Ctx.World()) { GameWorld = Ctx.World(); }
    }
    return GameWorld;
}

UGameViewportClient* FAgentWorld::GetActiveGameViewport()
{
    UWorld* W = GetActiveGameWorld();
    return W ? W->GetGameViewport() : nullptr;
}

FString FAgentWorld::DescribeClientsShort()
{
    TArray<UWorld*> Worlds;
    GetAllGameWorlds(Worlds);
    if (Worlds.Num() == 0) { return TEXT("(no game world - start PIE first)"); }
    TArray<FString> Parts;
    for (UWorld* W : Worlds)
    {
        Parts.Add(FString::Printf(TEXT("%d (%s, %s)"), GetClientIndex(W), *W->GetName(),
                                  UAPNetModeName(W->GetNetMode())));
    }
    return FString::Join(Parts, TEXT(", "));
}

bool FAgentWorld::ResolveClients(const FString& Client, TArray<UWorld*>& Out, FString& OutError)
{
    Out.Reset();
    OutError.Reset();
    const FString Sel = Client.TrimStartAndEnd();
    if (Sel.IsEmpty()) { return true; }   // no override: historical default behaviour

    TArray<UWorld*> All;
    GetAllGameWorlds(All);
    if (All.Num() == 0)
    {
        OutError = TEXT("no game world is running, so there is no client to target - start PIE first "
                        "(uap pie start).");
        return false;
    }

    if (Sel.Equals(TEXT("all"), ESearchCase::IgnoreCase))
    {
        Out = All;
        return true;
    }

    if (!Sel.IsNumeric())
    {
        OutError = FString::Printf(
            TEXT("Client must be a PIE instance index (\"0\", \"1\", ...) or \"all\"; got '%s'. "
                 "Clients right now: %s."), *Sel, *DescribeClientsShort());
        return false;
    }

    const int32 Wanted = FCString::Atoi(*Sel);
    for (UWorld* W : All)
    {
        if (GetClientIndex(W) == Wanted) { Out.Add(W); return true; }
    }
    // A standalone (non-PIE) game world has no PIE instance; "0" names it, so a caller that
    // always passes --client 0 still works against it.
    if (Wanted == 0 && All.Num() == 1 && GetClientIndex(All[0]) == INDEX_NONE)
    {
        Out.Add(All[0]);
        return true;
    }
    OutError = FString::Printf(
        TEXT("there is no client %d. Acting on a different client than the one named is the silent "
             "wrong answer this refusal exists to stop. Clients right now: %s."),
        Wanted, *DescribeClientsShort());
    return false;
}

FString FAgentWorld::DescribeClientsJson()
{
    TArray<UWorld*> Worlds;
    GetAllGameWorlds(Worlds);
    UWorld* Default = nullptr;
    {
        // The default with NO override, whatever scope is live.
        FScope NoOverride(nullptr);
        Default = GetActiveGameWorld();
    }

    TArray<TSharedPtr<FJsonValue>> Arr;
    for (UWorld* W : Worlds)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetNumberField(TEXT("client"), GetClientIndex(W));
        O->SetStringField(TEXT("world"), W->GetName());
        O->SetStringField(TEXT("map"), W->GetMapName());
        O->SetStringField(TEXT("net_mode"), UAPNetModeName(W->GetNetMode()));
        O->SetBoolField(TEXT("default"), W == Default);
        UGameViewportClient* GV = W->GetGameViewport();
        O->SetBoolField(TEXT("has_viewport"), GV != nullptr);
        O->SetBoolField(TEXT("accepts_input"), GV && GV->Viewport && !GV->IgnoreInput());
        if (GV && GV->GetWindow().IsValid())
        {
            O->SetStringField(TEXT("window_title"), GV->GetWindow()->GetTitle().ToString());
        }
        APlayerController* PC = W->GetFirstPlayerController();
        O->SetStringField(TEXT("player_controller"), PC ? PC->GetName() : FString());
        O->SetStringField(TEXT("pawn"), (PC && PC->GetPawn()) ? PC->GetPawn()->GetName() : FString());
        Arr.Add(MakeShared<FJsonValueObject>(O));
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetBoolField(TEXT("ok"), true);
    Root->SetNumberField(TEXT("count"), Arr.Num());
    Root->SetArrayField(TEXT("clients"), Arr);
    FString Out;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
    FJsonSerializer::Serialize(Root, Writer);
    return Out;
}
