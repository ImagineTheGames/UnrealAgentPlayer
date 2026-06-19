using UnrealBuildTool;

public class UnrealAgentPlayer : ModuleRules
{
    public UnrealAgentPlayer(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;
        bUseUnity = false;

        PublicDependencyModuleNames.AddRange(new[]
        {
            "Core",
            "CoreUObject",
            "Engine",
            "InputCore",
            "UnrealAgentPlayerRuntime",
        });

        PrivateDependencyModuleNames.AddRange(new[]
        {
            "Slate",
            "SlateCore",
            "UMG",
            "ApplicationCore",
            "EditorSubsystem",
            "UnrealEd",
            "LevelEditor",
            "EditorFramework",
            "Projects",
            "Json",
            "JsonUtilities",
            "RemoteControl",
            "RemoteControlCommon",
            "DeveloperSettings",
            "RHI",
            "RenderCore",
            "HeadMountedDisplay",
            "BlueprintGraph",
            "PropertyEditor",
            "CommonUI",
        });
    }
}
