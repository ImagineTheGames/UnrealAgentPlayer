#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonObject.h"

class UFunction;
class FProperty;

namespace UAP::Schema
{
    UNREALAGENTPLAYERRUNTIME_API TSharedPtr<FJsonObject> BuildArgSchema(const UFunction* Func, bool& bOutSupported, FString& OutReason);
    UNREALAGENTPLAYERRUNTIME_API TSharedPtr<FJsonObject> BuildReturnSchema(const UFunction* Func, bool& bOutSupported, FString& OutReason);
    UNREALAGENTPLAYERRUNTIME_API TSharedPtr<FJsonObject> PropertyToSchema(const FProperty* Prop, bool& bOutSupported, FString& OutReason);
}
