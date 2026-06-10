#pragma once
#include "CoreMinimal.h"
class UWorld;
class UGameViewportClient;
class UNREALAGENTPLAYERRUNTIME_API FAgentWorld
{
public:
    static UWorld* GetActiveGameWorld();
    static UGameViewportClient* GetActiveGameViewport();
};
