#pragma once

#include "CoreMinimal.h"

// Reads the on-screen UMG layer of the running PIE game so an agent can see
// prompts/labels (e.g. "Press E to open") and which element is focused, instead
// of injecting input blind. Returns a JSON string; see DumpViewportUI.
class UNREALAGENTPLAYERRUNTIME_API FAgentUIReader
{
public:
    // JSON: { "available": bool, "count": int, "focused": "<text under focused widget>",
    //         "texts": [ { "text": str, "x": num, "y": num, "focused": bool }, ... ] }
    // x/y are absolute screen-space pixel coordinates of the widget's cached geometry.
    static FString DumpViewportUI();
};
