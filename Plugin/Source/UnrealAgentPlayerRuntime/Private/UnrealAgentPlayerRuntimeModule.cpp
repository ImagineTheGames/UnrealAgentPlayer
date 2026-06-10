#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentMotionController.h"

DEFINE_LOG_CATEGORY(LogUAPRuntime);
#define LOCTEXT_NAMESPACE "FUnrealAgentPlayerRuntimeModule"

void FUnrealAgentPlayerRuntimeModule::StartupModule()
{
    UE_LOG(LogUAPRuntime, Log, TEXT("UnrealAgentPlayerRuntime started."));
    MotionController = MakeShared<FAgentMotionController>();
    MotionController->Register();
}

void FUnrealAgentPlayerRuntimeModule::ShutdownModule()
{
    if (MotionController.IsValid())
    {
        MotionController->Unregister();
        MotionController.Reset();
    }
}

FUnrealAgentPlayerRuntimeModule* FUnrealAgentPlayerRuntimeModule::Get()
{
    return FModuleManager::GetModulePtr<FUnrealAgentPlayerRuntimeModule>("UnrealAgentPlayerRuntime");
}

#undef LOCTEXT_NAMESPACE
IMPLEMENT_MODULE(FUnrealAgentPlayerRuntimeModule, UnrealAgentPlayerRuntime)
