#pragma once

#include "CoreMinimal.h"
#include "UAPAgentTypes.h"

class UClass;
class UFunction;
class UObject;

class UNREALAGENTPLAYERRUNTIME_API FAgentHelperDiscovery
{
public:
    static void RescanAll(TArray<FAgentHelperDescriptor>& OutList);

    /**
     * Serialize a helper list to JSON: {"helpers":[{name, category, tooltip, phase,
     * arg_schema, return_schema, supported, unsupported_reason}, ...]}.
     *
     * Agents must read the list through this, not through the TArray<FAgentHelperDescriptor>
     * return: RemoteControl's preset-call route filters serialized properties down to the
     * function's own out/return params, which strips every field of the nested struct and
     * yields [{},{},...]. A JSON string keeps the names -- the whole point of the verb -- and
     * matches how the rest of this plugin returns structured data.
     */
    static FString ToJson(const TArray<FAgentHelperDescriptor>& List);
    static UFunction* Resolve(const FString& FullName, UClass*& OutClass);
    static bool IsHelperFunction(const UFunction* Func);
};
