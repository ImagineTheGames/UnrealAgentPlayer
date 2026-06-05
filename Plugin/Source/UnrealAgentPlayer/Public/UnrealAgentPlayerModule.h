#pragma once

#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"

DECLARE_LOG_CATEGORY_EXTERN(LogUAP, Log, All);

class FUnrealAgentPlayerModule : public IModuleInterface
{
public:
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;

    static class FAgentMotionController* GetMotionController();

private:
    TSharedPtr<class FAgentMotionController> MotionController;
};
