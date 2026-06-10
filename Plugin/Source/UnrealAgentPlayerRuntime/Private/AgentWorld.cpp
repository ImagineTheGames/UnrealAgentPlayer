#include "AgentWorld.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "Engine/GameViewportClient.h"

UWorld* FAgentWorld::GetActiveGameWorld()
{
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
