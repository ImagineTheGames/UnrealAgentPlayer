#pragma once

#include "CoreMinimal.h"
#include "IDetailCustomization.h"
#include "Styling/SlateTypes.h"
#include "UObject/WeakObjectPtr.h"

class IDetailLayoutBuilder;
class UK2Node_FunctionEntry;

/**
 * Adds an "Agent Test Helper" category with an "Expose as Agent Test Helper" checkbox to the
 * details panel of a Blueprint function entry. Ticking it sets the `AgentTestHelper` metadata
 * key on the function, which FAgentHelperDiscovery reads via UFunction::HasMetaData - so a
 * Blueprint function library can register helpers without touching C++.
 */
class FAgentHelperBlueprintDetails : public IDetailCustomization
{
public:
    static TSharedRef<IDetailCustomization> MakeInstance();

    virtual void CustomizeDetails(IDetailLayoutBuilder& DetailBuilder) override;

private:
    TWeakObjectPtr<UK2Node_FunctionEntry> FunctionEntry;

    ECheckBoxState IsHelperChecked() const;
    void OnHelperCheckChanged(ECheckBoxState NewState);
};
