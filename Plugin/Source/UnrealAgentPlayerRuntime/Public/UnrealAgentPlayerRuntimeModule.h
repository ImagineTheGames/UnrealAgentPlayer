#pragma once
#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"

UNREALAGENTPLAYERRUNTIME_API DECLARE_LOG_CATEGORY_EXTERN(LogUAPRuntime, Log, All);

class FUnrealAgentPlayerRuntimeModule : public IModuleInterface
{
public:
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;
    static FUnrealAgentPlayerRuntimeModule* Get();
};
