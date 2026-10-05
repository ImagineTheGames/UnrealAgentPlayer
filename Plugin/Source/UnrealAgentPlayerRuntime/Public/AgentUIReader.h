#pragma once

#include "CoreMinimal.h"

// Reads the on-screen UMG layer of the running PIE game so an agent can see
// prompts/labels (e.g. "Press E to open") and which element is focused, instead
// of injecting input blind. Returns a JSON string; see DumpViewportUI.
class UNREALAGENTPLAYERRUNTIME_API FAgentUIReader
{
public:
    // JSON: { "available": bool, "count": int, "focused": "<text under focused widget>",
    //         "texts": [ { "index": int, "text": str, "x": num, "y": num,
    //                      "focused": bool, "enabled": bool, "disabled_by": str }, ... ] }
    //
    // x/y are absolute screen-space pixel coordinates of the widget's cached geometry.
    //
    // `enabled` is the EFFECTIVE state -- false when this text sits inside a disabled
    // ancestor, which is the case that matters because a click there cannot reach it
    // (Slate truncates the hit path at the disabled widget; see ClickMouse, which refuses).
    // `disabled_by` names the widget responsible, and is present only when enabled is false.
    //
    // `texts` is ordered top-to-bottom then left-to-right, and `index` is that position. The
    // order is part of the contract: a caller resolving a DUPLICATED label needs the list to
    // mean something. It used to come out in UObject allocation order.
    //
    // This answers "is this widget present", never "does the player see it" -- an occluded
    // widget is still in the Slate tree and still reported here. Use a screenshot for that.
    static FString DumpViewportUI();
};
