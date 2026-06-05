#include "AgentHelperBlueprintDetails.h"

#include "DetailLayoutBuilder.h"
#include "DetailCategoryBuilder.h"
#include "DetailWidgetRow.h"
#include "K2Node_FunctionEntry.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Widgets/Input/SCheckBox.h"
#include "Widgets/Text/STextBlock.h"

#define LOCTEXT_NAMESPACE "AgentHelperBlueprintDetails"

static const FName MetaKey_AgentTestHelper(TEXT("AgentTestHelper"));

TSharedRef<IDetailCustomization> FAgentHelperBlueprintDetails::MakeInstance()
{
    return MakeShared<FAgentHelperBlueprintDetails>();
}

void FAgentHelperBlueprintDetails::CustomizeDetails(IDetailLayoutBuilder& DetailBuilder)
{
    TArray<TWeakObjectPtr<UObject>> Objects;
    DetailBuilder.GetObjectsBeingCustomized(Objects);
    for (const TWeakObjectPtr<UObject>& Obj : Objects)
    {
        if (UK2Node_FunctionEntry* Entry = Cast<UK2Node_FunctionEntry>(Obj.Get()))
        {
            FunctionEntry = Entry;
            break;
        }
    }
    if (!FunctionEntry.IsValid()) { return; }

    IDetailCategoryBuilder& Cat = DetailBuilder.EditCategory(
        TEXT("AgentTestHelper"),
        LOCTEXT("AgentCategory", "Agent Test Helper"),
        ECategoryPriority::Important);

    Cat.AddCustomRow(LOCTEXT("AgentHelperFilter", "Agent Test Helper"))
    .NameContent()
    [
        SNew(STextBlock)
        .Font(IDetailLayoutBuilder::GetDetailFont())
        .Text(LOCTEXT("AgentHelperLabel", "Expose as Agent Test Helper"))
        .ToolTipText(LOCTEXT("AgentHelperTip",
            "When checked, UnrealAgentPlayer discovers this function via helper_list / helper_call."))
    ]
    .ValueContent()
    [
        SNew(SCheckBox)
        .IsChecked(this, &FAgentHelperBlueprintDetails::IsHelperChecked)
        .OnCheckStateChanged(this, &FAgentHelperBlueprintDetails::OnHelperCheckChanged)
    ];
}

ECheckBoxState FAgentHelperBlueprintDetails::IsHelperChecked() const
{
    if (!FunctionEntry.IsValid()) { return ECheckBoxState::Unchecked; }
    return FunctionEntry->MetaData.HasMetaData(MetaKey_AgentTestHelper)
        ? ECheckBoxState::Checked : ECheckBoxState::Unchecked;
}

void FAgentHelperBlueprintDetails::OnHelperCheckChanged(ECheckBoxState NewState)
{
    if (!FunctionEntry.IsValid()) { return; }
    if (NewState == ECheckBoxState::Checked)
    {
        FunctionEntry->MetaData.SetMetaData(MetaKey_AgentTestHelper, FString());
    }
    else if (FunctionEntry->MetaData.HasMetaData(MetaKey_AgentTestHelper))
    {
        FunctionEntry->MetaData.RemoveMetaData(MetaKey_AgentTestHelper);
    }
    if (UBlueprint* BP = FunctionEntry->GetBlueprint())
    {
        FBlueprintEditorUtils::MarkBlueprintAsModified(BP);
    }
}

#undef LOCTEXT_NAMESPACE
