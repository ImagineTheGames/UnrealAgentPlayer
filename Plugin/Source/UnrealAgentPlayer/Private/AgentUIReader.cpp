#include "AgentUIReader.h"

#include "UObject/UObjectIterator.h"
#include "Editor.h"
#include "Engine/Engine.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "Components/TextBlock.h"
#include "Components/RichTextBlock.h"
#include "Components/PanelWidget.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

namespace
{
    UWorld* GetPIEWorld()
    {
        if (!GEditor) { return nullptr; }
        for (const FWorldContext& Ctx : GEditor->GetWorldContexts())
        {
            if (Ctx.WorldType == EWorldType::PIE && Ctx.World())
            {
                return Ctx.World();
            }
        }
        return nullptr;
    }

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
    UWorld* World = GetPIEWorld();
    if (!World)
    {
        return SerializeEmpty(false);
    }

    APlayerController* PC = World->GetFirstPlayerController();
    TArray<TSharedPtr<FJsonValue>> Texts;
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
        const bool bFocused = AncestorHasFocus(Widget, PC);

        TSharedRef<FJsonObject> Entry = MakeShared<FJsonObject>();
        Entry->SetStringField(TEXT("text"), Text);
        Entry->SetNumberField(TEXT("x"), Pos.X);
        Entry->SetNumberField(TEXT("y"), Pos.Y);
        Entry->SetBoolField(TEXT("focused"), bFocused);
        Texts.Add(MakeShared<FJsonValueObject>(Entry));

        if (bFocused && FocusedText.IsEmpty())
        {
            FocusedText = Text;
        }
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
