#pragma once
#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"
#include "Containers/Ticker.h"

UNREALAGENTPLAYERRUNTIME_API DECLARE_LOG_CATEGORY_EXTERN(LogUAPRuntime, Log, All);

class UNREALAGENTPLAYERRUNTIME_API FUnrealAgentPlayerRuntimeModule : public IModuleInterface
{
public:
    virtual void StartupModule() override;
    virtual void ShutdownModule() override;
    static FUnrealAgentPlayerRuntimeModule* Get();

    class FAgentMotionController* GetMotionController() { return MotionController.Get(); }

    // The RemoteControl HTTP port this editor's RC server actually bound (0 if none). Lets an
    // agent CLI resolve the right port per editor instead of guessing 30010 -- so two editors
    // can each run RemoteControl at once. Surfaced to scripting via UAPAgentSubsystem::GetRemoteControlPort.
    static int32 GetConfiguredRCPort();

private:
    TSharedPtr<class FAgentMotionController> MotionController;

#if WITH_EDITOR
    // Deferred (post-init) check: if our RC HTTP server failed to bind (port taken by another
    // editor), move to a free port and rebind. A successful bind is left untouched. Runs from
    // a ticker so WebRemoteControl is loaded and has finished its initial bind attempt.
    void EnsureRCPortBound();
    // First free TCP port in [Start, End] on 127.0.0.1, or 0 if none.
    static int32 ProbeFreePort(int32 Start, int32 End);

    FTSTicker::FDelegateHandle RCPortTickerHandle;
#endif
};
