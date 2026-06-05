#pragma once

#include "CoreMinimal.h"
#include "Dom/JsonObject.h"

class UFunction;
class FProperty;

namespace UAP::Schema
{
    TSharedPtr<FJsonObject> BuildArgSchema(const UFunction* Func, bool& bOutSupported, FString& OutReason);
    TSharedPtr<FJsonObject> BuildReturnSchema(const UFunction* Func, bool& bOutSupported, FString& OutReason);
    TSharedPtr<FJsonObject> PropertyToSchema(const FProperty* Prop, bool& bOutSupported, FString& OutReason);
}
