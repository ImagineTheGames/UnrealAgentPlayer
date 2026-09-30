#pragma once

#include "CoreMinimal.h"

/**
 * In-viewport overlay saying what uap is doing (ClickUp 17tm466gqfj).
 *
 * Registered with UDebugDrawService so it draws in the LEVEL EDITOR viewport and in PIE, with
 * no widget, no actor and no tick of its own -- the draw callback is the only work it does,
 * and `uap.Overlay 0` unregisters it so a disabled overlay is not called at all.
 */
class UNREALAGENTPLAYERRUNTIME_API FUAPOverlay
{
public:
    /** Registers the debug-draw callback if `uap.Overlay` is on, and starts watching the CVar. */
    static void Register();
    static void Unregister();
};
