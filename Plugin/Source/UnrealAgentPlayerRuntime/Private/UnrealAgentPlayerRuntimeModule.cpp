#include "UnrealAgentPlayerRuntimeModule.h"

DEFINE_LOG_CATEGORY(LogUAPRuntime);
#define LOCTEXT_NAMESPACE "FUnrealAgentPlayerRuntimeModule"

void FUnrealAgentPlayerRuntimeModule::StartupModule()
{
    UE_LOG(LogUAPRuntime, Log, TEXT("UnrealAgentPlayerRuntime started."));
}

void FUnrealAgentPlayerRuntimeModule::ShutdownModule()
{
}

FUnrealAgentPlayerRuntimeModule* FUnrealAgentPlayerRuntimeModule::Get()
{
    return FModuleManager::GetModulePtr<FUnrealAgentPlayerRuntimeModule>("UnrealAgentPlayerRuntime");
}

#undef LOCTEXT_NAMESPACE
IMPLEMENT_MODULE(FUnrealAgentPlayerRuntimeModule, UnrealAgentPlayerRuntime)
