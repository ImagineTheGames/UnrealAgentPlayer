using UnrealBuildTool;

public class UnrealAgentPlayerRuntime : ModuleRules
{
    public UnrealAgentPlayerRuntime(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;
        bUseUnity = false;

        PublicDependencyModuleNames.AddRange(new[]
        {
            "Core", "CoreUObject", "Engine", "InputCore",
        });

        PrivateDependencyModuleNames.AddRange(new[]
        {
            "Slate", "SlateCore", "ApplicationCore", "UMG",
            "RHI", "RenderCore", "HeadMountedDisplay",
            "Json", "JsonUtilities",
            "RemoteControl", "RemoteControlCommon",
            "DeveloperSettings", "Projects",
        });
    }
}
