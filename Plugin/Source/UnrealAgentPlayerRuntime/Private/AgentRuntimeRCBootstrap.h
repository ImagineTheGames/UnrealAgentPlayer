#pragma once
#include "CoreMinimal.h"
class UUAPAgentRuntimeSubsystem;
class FAgentRuntimeRCBootstrap
{
public:
    static void Expose(UUAPAgentRuntimeSubsystem* Subsystem);
    static void Unexpose();
};
