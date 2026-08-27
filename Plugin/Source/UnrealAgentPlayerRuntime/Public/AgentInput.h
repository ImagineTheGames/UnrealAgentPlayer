#pragma once

#include "CoreMinimal.h"
#include "InputCoreTypes.h"
#include "UAPAgentTypes.h"

class SWidget;

/** One entry of the held-input registry, as reported back to an agent. */
struct FAgentHeldInputInfo
{
    FString Key;
    bool    bAnalog = false;
    float   Value = 0.f;
    float   RemainingSeconds = 0.f;
};

class UNREALAGENTPLAYERRUNTIME_API FAgentInput
{
public:
    // NOTE ON RETURN VALUES: these report whether the event was DELIVERED to the game, not
    // whether the game did anything with it. UGameViewportClient::InputKey forwards
    // UPlayerInput::InputKey, which for IE_Pressed returns IsKeyHandledByAction() -- a check
    // against the LEGACY ActionMappings array only. Under Enhanced Input that is false for
    // essentially every key, so "handled" is useless as a success signal and treating it as
    // one is what leaked a permanently stuck key (see HoldKey).
    static bool InjectKey(FKey Key, bool bPressed, bool bRepeat);
    static bool InjectMouseMove(FVector2D Delta, bool bAbsolute);
    static bool InjectMouseButton(EAgentMouseButton Btn, bool bPressed);
    static bool InjectAxis(FName AxisName, float Value);
    static bool InjectGamepad(EAgentGamepadButton Btn, bool bPressed, float AnalogValue);

    /** One analog sample for an axis FKey, routed through the game viewport (see .cpp). */
    static bool InjectAxisKey(FKey Key, float Value);

    /** True when there is a game viewport that will accept injected input right now. */
    static bool HasLiveViewport();

    /** Engine ground truth: is this key currently down on the first local player? */
    static bool IsKeyDown(FKey Key);

    // --- Held input -------------------------------------------------------------------
    // A single injected event cannot drive sustained locomotion: an external CLI round-trip
    // is ~1s, and anything that calls APlayerController::FlushPressedKeys (input-mode change,
    // focus loss, PC recreation) silently drops a latched key with no signal to the caller.
    // Analog axes are worse -- a real stick re-sends its value every frame, so a one-shot
    // sample is not what the game sees. These re-assert the input every frame, in-engine,
    // until the duration expires, then release cleanly.
    //
    // The *Json entrypoints are what the subsystems expose: a refusal has to say WHY, because
    // a guessed error message ("unknown key name") sent a real investigation down the wrong
    // path. Every one of them validates fully BEFORE pressing anything, so a refused call has
    // no side effects, and unwinds anything it did press if a later step fails.

    static FString HoldKeyJson(const FString& KeyName, float Seconds);
    static FString HoldAxisJson(const FString& AxisKeyName, float Value, float Seconds);

    /**
     * Release input. Empty KeyName is the RECOVERY path: it releases every registry entry AND
     * flushes every key the engine still has down, so a key the registry lost track of can
     * always be cleared without restarting PIE. A named key is force-released whether or not
     * the registry knows about it.
     */
    static FString ReleaseHeldJson(const FString& KeyName);
    static FString GetHeldJson();

    /** Press Key now and hold it (IE_Repeat every frame) for Seconds, then release. */
    static bool HoldKey(FKey Key, float Seconds);

    /** Drive an analog axis FKey at Value every frame for Seconds, then recentre to 0. */
    static bool HoldAxis(FKey Key, float Value, float Seconds);

    /** Release a held input early. An invalid Key releases everything. Returns count released. */
    static int32 ReleaseHeld(FKey Key);

    /** APlayerController::FlushPressedKeys on every local PC. Returns how many were flushed. */
    static int32 FlushAllPressedKeys();

    static void GetHeld(TArray<FAgentHeldInputInfo>& Out);

    /** Module shutdown: drop the ticker and forget every hold. */
    static void ShutdownHolds();

private:
    static TSharedPtr<SWidget> FindPIEViewportWidget();
    static FKey GamepadButtonToKey(EAgentGamepadButton Btn);
    static FKey MouseButtonToKey(EAgentMouseButton Btn);
};
