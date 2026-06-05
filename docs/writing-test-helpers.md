# Writing Agent Test Helpers

A test helper is a UFUNCTION that the agent can call to assert game state. They auto-register when tagged with `meta=(AgentTestHelper)`.

## C++

```cpp
UCLASS()
class YOURMODULE_API UMyTestHelpers : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()

    UFUNCTION(BlueprintCallable, Category="Agent|YourArea",
              meta=(AgentTestHelper, Phase="Playing",
                    ToolTip="Plain-English description the agent reads."))
    static bool YourAssertion(FName Param);
};
```

## Blueprint

1. Create a Blueprint function library and add a function.
2. Select the function; in the details panel, find the **Agent Test Helper** category and tick
   **Expose as Agent Test Helper**. (This is the convenience checkbox the plugin's detail
   customization adds — it sets the `AgentTestHelper` metadata key on the function. You can also
   add the key by hand via the generic function Metadata list.)
3. Optionally add `Phase`, `Category`, `ToolTip` keys via the function's Metadata list.
4. Compile the Blueprint (the helper is discovered on the next `helper_list` scan; reload the
   editor if it was already cached).

## Metadata keys

- `AgentTestHelper` (required) — marks the function for discovery.
- `Phase` — `Playing` / `NotPlaying` / `Any`. Agent gets a fast-fail error if called in the wrong phase.
- `Category` — free-form, pipe-delimited hierarchy (`Agent|Doors`).
- `ToolTip` — surfaces to the agent in `helper_list`.

## Supported types

- Primitives: `bool`, `int32`, `int64`, `float`, `double`, `FString`, `FName`, `FText`
- Math: `FVector`, `FVector2D`, `FRotator`, `FQuat`, `FTransform`, `FColor`
- Enums marked `UENUM`
- Structs marked `USTRUCT(BlueprintType)` (recursive)
- Arrays/maps of supported types
- `UObject*` (serialized as object path)

Unsupported: delegates, `TSet`, raw pointers. Helpers using these appear in `helper_list` with `supported: false`.

## Philosophy

Grow the helper library over time. Every time the agent hits an ambiguity ("can't tell if X happened"), add a helper for X. The library is the shared vocabulary between game devs and the agent.
