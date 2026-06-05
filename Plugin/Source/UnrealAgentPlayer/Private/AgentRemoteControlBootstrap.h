#pragma once

#include "CoreMinimal.h"

class FAgentRemoteControlBootstrap
{
public:
    static void Startup();
    static void Shutdown();

private:
    static void EnsureSubsystemExposed();
};
