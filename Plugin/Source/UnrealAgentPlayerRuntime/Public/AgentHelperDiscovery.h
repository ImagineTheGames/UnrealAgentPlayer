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
    static UFunction* Resolve(const FString& FullName, UClass*& OutClass);
    static bool IsHelperFunction(const UFunction* Func);
};
