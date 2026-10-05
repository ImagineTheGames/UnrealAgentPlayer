#include "AgentUIReader.h"
#include "AgentWorld.h"

#include "UObject/UObjectIterator.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "Components/TextBlock.h"
#include "Components/RichTextBlock.h"
#include "Components/PanelWidget.h"
#include "Widgets/SWidget.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

namespace
{
    // True if the widget or any UMG ancestor currently holds keyboard / user focus
    // (the focused button highlights its child label, which itself never takes focus).
    bool AncestorHasFocus(UWidget* Widget, APlayerController* PC)
    {
        for (UWidget* Cur = Widget; Cur; Cur = Cur->GetParent())
        {
            if (Cur->HasKeyboardFocus()) { return true; }
            if (PC && Cur->HasUserFocus(PC)) { return true; }
        }
        return false;
    }

    FString DescribeSlateWidget(const TSharedPtr<SWidget>& W)
    {
        if (!W.IsValid()) { return FString(); }
        const FName Tag = W->GetTag();
        return Tag.IsNone() ? W->GetType().ToString()
                            : FString::Printf(TEXT("%s[%s]"), *W->GetType().ToString(),
                                              *Tag.ToString());
    }

    /**
     * Effective enabled state, judged the way a CLICK is judged rather than the way the UMG
     * property reads.
     *
     * A label inside a disabled button has its OWN bIsEnabled true -- disabling does not
     * propagate onto children, it is applied at hit-test time. Slate's
     * FHittestGrid::GetBubblePath (engine SlateCore/Private/Input/HittestGrid.cpp:228) walks
     * the PAINT parents and truncates the hit path at the outermost widget whose IsEnabled()
     * is false, so that same walk with that same predicate is exactly what decides whether a
     * click can reach this text. Walking UMG's GetParent() instead would miss the pure-Slate
     * widgets a UMG tree is built out of (SButton's own content border, SObjectWidget, ...).
     *
     * Returns true when nothing in the chain is disabled; otherwise OutDisabledBy names the
     * OUTERMOST disabled widget, which is the one the truncation happens at.
     */
    bool IsEffectivelyEnabled(UWidget* Widget, FString& OutDisabledBy)
    {
        OutDisabledBy.Reset();
        TSharedPtr<SWidget> Cur = Widget ? Widget->GetCachedWidget() : nullptr;
        bool bEnabled = true;
        while (Cur.IsValid())
        {
            if (!Cur->IsEnabled())
            {
                bEnabled = false;
                // Keep walking rather than breaking: the last one found on the way up is the
                // outermost, and that is the one the hit test truncates at.
                OutDisabledBy = DescribeSlateWidget(Cur);
            }
            Cur = Cur->Advanced_GetPaintParentWidget();
        }
        return bEnabled;
    }

    struct FUIEntry
    {
        FString Text;
        FVector2D Pos = FVector2D::ZeroVector;
        bool bFocused = false;
        bool bEnabled = true;
        FString DisabledBy;
    };

    FString SerializeEmpty(bool bAvailable)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetBoolField(TEXT("available"), bAvailable);
        Root->SetNumberField(TEXT("count"), 0);
        Root->SetStringField(TEXT("focused"), FString());
        Root->SetArrayField(TEXT("texts"), TArray<TSharedPtr<FJsonValue>>());
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }
}

FString FAgentUIReader::DumpViewportUI()
{
    UWorld* World = FAgentWorld::GetActiveGameWorld();
    if (!World)
    {
        return SerializeEmpty(false);
    }

    APlayerController* PC = World->GetFirstPlayerController();
    TArray<FUIEntry> Entries;
    FString FocusedText;

    auto Emit = [&](UWidget* Widget, const FString& RawText)
    {
        if (!IsValid(Widget) || Widget->GetWorld() != World || !Widget->IsVisible())
        {
            return;
        }
        const FString Text = RawText.TrimStartAndEnd();
        if (Text.IsEmpty())
        {
            return;
        }
        // Skip widgets that are visible-by-flag but not actually laid out this frame:
        // an unpainted widget reports an absolute position of (0,0). Real on-screen
        // widgets have a non-zero screen position (game UI is never at the 0,0 pixel).
        const FVector2D Pos = Widget->GetCachedGeometry().GetAbsolutePosition();
        if (Pos.IsNearlyZero())
        {
            return;
        }

        FUIEntry Entry;
        Entry.Text = Text;
        Entry.Pos = Pos;
        Entry.bFocused = AncestorHasFocus(Widget, PC);
        Entry.bEnabled = IsEffectivelyEnabled(Widget, Entry.DisabledBy);
        Entries.Add(MoveTemp(Entry));
    };

    // UTextBlock (incl. UCommonTextBlock and other subclasses).
    for (TObjectIterator<UTextBlock> It; It; ++It)
    {
        Emit(*It, It->GetText().ToString());
    }
    // URichTextBlock (separate hierarchy; source text may include inline markup).
    for (TObjectIterator<URichTextBlock> It; It; ++It)
    {
        Emit(*It, It->GetText().ToString());
    }

    // SCREEN ORDER, not allocation order. The iterators above walk the object table, so the
    // list used to come out in UObject allocation order -- which is why a caller taking "the
    // first match" for a duplicated label (`>`, `Equip`) got an unpredictable row rather than
    // the top one, and why three separate wrong claims were made about this matcher in one
    // night [17tm466g08h]. Rounding Y to the pixel keeps this a total order (a tolerance-based
    // compare is not transitive and can violate the sort's contract).
    Entries.Sort([](const FUIEntry& A, const FUIEntry& B)
    {
        const int32 AY = FMath::RoundToInt(A.Pos.Y);
        const int32 BY = FMath::RoundToInt(B.Pos.Y);
        if (AY != BY) { return AY < BY; }
        const int32 AX = FMath::RoundToInt(A.Pos.X);
        const int32 BX = FMath::RoundToInt(B.Pos.X);
        if (AX != BX) { return AX < BX; }
        return A.Text < B.Text;
    });

    TArray<TSharedPtr<FJsonValue>> Texts;
    Texts.Reserve(Entries.Num());
    for (int32 i = 0; i < Entries.Num(); ++i)
    {
        const FUIEntry& E = Entries[i];
        TSharedRef<FJsonObject> Obj = MakeShared<FJsonObject>();
        Obj->SetNumberField(TEXT("index"), i);
        Obj->SetStringField(TEXT("text"), E.Text);
        Obj->SetNumberField(TEXT("x"), E.Pos.X);
        Obj->SetNumberField(TEXT("y"), E.Pos.Y);
        Obj->SetBoolField(TEXT("focused"), E.bFocused);
        Obj->SetBoolField(TEXT("enabled"), E.bEnabled);
        if (!E.bEnabled)
        {
            Obj->SetStringField(TEXT("disabled_by"), E.DisabledBy);
        }
        Texts.Add(MakeShared<FJsonValueObject>(Obj));

        if (E.bFocused && FocusedText.IsEmpty())
        {
            FocusedText = E.Text;
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetBoolField(TEXT("available"), true);
    Root->SetNumberField(TEXT("count"), Texts.Num());
    Root->SetStringField(TEXT("focused"), FocusedText);
    Root->SetArrayField(TEXT("texts"), Texts);

    FString Out;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
    FJsonSerializer::Serialize(Root, Writer);
    return Out;
}
