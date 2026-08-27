#pragma once

#include "CoreMinimal.h"

/**
 * Frame-rate property sampler.
 *
 * An agent's finest sampling granularity from outside the editor is one exec round-trip
 * (~1s), which cannot see anything sub-second: animation judder, a 0.6s AI wind-up, a
 * one-frame pop. This records a value IN-ENGINE once per frame for a bounded window, then
 * hands the whole series back in one call so the agent can compute deltas / jitter.
 *
 * One series at a time (a test measures one thing); starting a new one replaces the old.
 */
class UNREALAGENTPLAYERRUNTIME_API FAgentSampler
{
public:
    /**
     * Begin sampling. Returns a JSON status string: {"ok":true,"object":"...","property":"..."}
     * or {"ok":false,"error":"..."}.
     *
     * ObjectPath accepts a full object path (/Game/...), an actor name/substring in the live
     * game world, or one of the shortcuts PlayerPawn / PlayerController / PlayerCameraManager.
     *
     * PropertyPath is a dot-separated FProperty chain (e.g. "CharacterMovement.Velocity"),
     * walking through object and struct properties, with component names usable as a step on
     * an actor. The final step may instead be a computed value: WorldLocation, WorldRotation,
     * WorldScale, WorldTransform, ForwardVector or Velocity.
     */
    static FString Start(const FString& ObjectPath, const FString& PropertyPath,
                         float Seconds, int32 MaxSamples);

    /** JSON: {"ok":true,"active":bool,"object":..,"property":..,"count":N,"samples":[{"t":sec,"v":..}]} */
    static FString Read();

    /** Stop early, keeping whatever was collected. */
    static void Stop();

    /** Module shutdown: drop the ticker and the series. */
    static void Shutdown();
};
