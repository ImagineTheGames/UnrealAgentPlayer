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
            // Needed by ClearRemoteExecGlobals, which sweeps the Python remote-exec globals dict
            // on a level change. The .uplugin already lists PythonScriptPlugin as a required
            // plugin (remote exec is how the agent CLI talks to the editor), so this adds no new
            // plugin requirement -- only a module dependency on its public interface.
            "PythonScriptPlugin",
        });
    }
}
