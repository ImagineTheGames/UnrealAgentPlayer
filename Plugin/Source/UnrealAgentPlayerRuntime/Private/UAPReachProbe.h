#pragma once
#include "CoreMinimal.h"

class UWorld;

// Runtime twin of mcp-server xr_reach.py's _REACH script, so a standalone -game client (which has
// no editor Python world) can answer `uap xr reach`. Reads the item point, the hand's grab sphere
// and the tracking delta from the world, and returns the same JSON the script printed:
// {ok, actor, target_how, target[3], grab[3], grab_component, grab_radius, controller,
//  delta_tracking[3], ctrl_rel_loc[3], ctrl_rel_rot[3]} or {ok:false, error}.
// Hand "left"/"right"; Target "actor[.component]"; OffsetCsv "" or "x,y,z" (cm, component local).
FString UAPReachProbeJson(UWorld* World, const FString& Hand, const FString& Target, const FString& OffsetCsv);
