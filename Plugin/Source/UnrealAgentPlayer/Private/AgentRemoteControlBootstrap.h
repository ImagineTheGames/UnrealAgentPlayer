#pragma once

#include "CoreMinimal.h"

class UUAPAgentSubsystem;

// Creates a named, registered RemoteControl preset (UAP_Preset) exposing the editor
// agent subsystem's BlueprintCallable functions at a stable, discoverable HTTP surface
// (/remote/preset/UAP_Preset/function/<Name>). Driven from the subsystem's
// Initialize/Deinitialize (mirrors the runtime side) because binding OnPostEngineInit from
// a PostEngineInit-phase module runs too late and never fires.
class FAgentRemoteControlBootstrap
{
public:
    static void Expose(UUAPAgentSubsystem* Subsystem);
    static void Unexpose();
};
