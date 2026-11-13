#pragma once

#include "CoreMinimal.h"

class UObject;

// Creates a named, registered RemoteControl preset (UAP_Preset) exposing the editor
// agent subsystem's BlueprintCallable functions at a stable, discoverable HTTP surface
// (/remote/preset/UAP_Preset/function/<Name>). Driven from the subsystem's
// Initialize/Deinitialize (mirrors the runtime side) because binding OnPostEngineInit from
// a PostEngineInit-phase module runs too late and never fires.
class FAgentRemoteControlBootstrap
{
public:
    // Adds every BlueprintCallable function of Subsystem to the shared UAP_Preset (creating and
    // registering the preset on the first call). Called once per agent subsystem.
    static void Expose(UObject* Subsystem);
    static void Unexpose();
};
