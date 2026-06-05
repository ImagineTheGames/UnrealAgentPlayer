#include "AgentInput.h"

#include "UnrealAgentPlayerModule.h"
#include "Editor.h"
#include "Editor/EditorEngine.h"
#include "Framework/Application/SlateApplication.h"
#include "GenericPlatform/GenericApplication.h"
#include "Engine/GameViewportClient.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "GameFramework/PlayerInput.h"
#include "Widgets/SViewport.h"

TSharedPtr<SWidget> FAgentInput::FindPIEViewportWidget()
{
    if (!GEditor) { return nullptr; }
    const FWorldContext* PIEContext = nullptr;
    for (const FWorldContext& Ctx : GEditor->GetWorldContexts())
    {
        if (Ctx.WorldType == EWorldType::PIE && Ctx.World())
        {
            PIEContext = &Ctx;
            break;
        }
    }
    if (!PIEContext || !PIEContext->GameViewport) { return nullptr; }
    return PIEContext->GameViewport->GetGameViewportWidget();
}

FKey FAgentInput::MouseButtonToKey(EAgentMouseButton Btn)
{
    switch (Btn)
    {
        case EAgentMouseButton::Left:     return EKeys::LeftMouseButton;
        case EAgentMouseButton::Right:    return EKeys::RightMouseButton;
        case EAgentMouseButton::Middle:   return EKeys::MiddleMouseButton;
        case EAgentMouseButton::XButton1: return EKeys::ThumbMouseButton;
        case EAgentMouseButton::XButton2: return EKeys::ThumbMouseButton2;
    }
    return EKeys::Invalid;
}

FKey FAgentInput::GamepadButtonToKey(EAgentGamepadButton Btn)
{
    switch (Btn)
    {
        case EAgentGamepadButton::FaceBottom:    return EKeys::Gamepad_FaceButton_Bottom;
        case EAgentGamepadButton::FaceRight:     return EKeys::Gamepad_FaceButton_Right;
        case EAgentGamepadButton::FaceLeft:      return EKeys::Gamepad_FaceButton_Left;
        case EAgentGamepadButton::FaceTop:       return EKeys::Gamepad_FaceButton_Top;
        case EAgentGamepadButton::ShoulderLeft:  return EKeys::Gamepad_LeftShoulder;
        case EAgentGamepadButton::ShoulderRight: return EKeys::Gamepad_RightShoulder;
        case EAgentGamepadButton::TriggerLeft:   return EKeys::Gamepad_LeftTrigger;
        case EAgentGamepadButton::TriggerRight:  return EKeys::Gamepad_RightTrigger;
        case EAgentGamepadButton::ThumbLeft:     return EKeys::Gamepad_LeftThumbstick;
        case EAgentGamepadButton::ThumbRight:    return EKeys::Gamepad_RightThumbstick;
        case EAgentGamepadButton::DPadUp:        return EKeys::Gamepad_DPad_Up;
        case EAgentGamepadButton::DPadDown:      return EKeys::Gamepad_DPad_Down;
        case EAgentGamepadButton::DPadLeft:      return EKeys::Gamepad_DPad_Left;
        case EAgentGamepadButton::DPadRight:     return EKeys::Gamepad_DPad_Right;
        case EAgentGamepadButton::Start:         return EKeys::Gamepad_Special_Right;
        case EAgentGamepadButton::Back:          return EKeys::Gamepad_Special_Left;
        case EAgentGamepadButton::Special:       return EKeys::Gamepad_Special_Right;
        case EAgentGamepadButton::LeftStickX:    return EKeys::Gamepad_LeftX;
        case EAgentGamepadButton::LeftStickY:    return EKeys::Gamepad_LeftY;
        case EAgentGamepadButton::RightStickX:   return EKeys::Gamepad_RightX;
        case EAgentGamepadButton::RightStickY:   return EKeys::Gamepad_RightY;
    }
    return EKeys::Invalid;
}

bool FAgentInput::InjectKey(FKey Key, bool bPressed, bool bRepeat)
{
    TSharedPtr<SWidget> Target = FindPIEViewportWidget();
    if (!Target.IsValid()) { return false; }
    FSlateApplication& App = FSlateApplication::Get();

    // ProcessKeyDownEvent routes down the CURRENT Slate keyboard-focus path, not to
    // Target. Force focus onto the PIE viewport first so the key reaches
    // SViewport->FSceneViewport->GameViewportClient::InputKey even when this editor is
    // not the OS-foreground window. This is fully in-process (no Win32 foreground lock),
    // so two editors in separate processes can each receive injected keys independently.
    App.SetAllUserFocus(Target.ToSharedRef(), EFocusCause::SetDirectly);

    FKeyEvent Evt(
        Key, App.GetModifierKeys(),
        /*UserIndex*/ 0,
        /*bIsRepeat*/ bRepeat,
        /*CharacterCode*/ 0,
        /*KeyCode*/ 0
    );

    if (bPressed) { return App.ProcessKeyDownEvent(Evt); }
    return App.ProcessKeyUpEvent(Evt);
}

bool FAgentInput::InjectMouseMove(FVector2D Delta, bool bAbsolute)
{
    TSharedPtr<SWidget> Target = FindPIEViewportWidget();
    if (!Target.IsValid()) { return false; }
    FSlateApplication& App = FSlateApplication::Get();

    FVector2D CursorPos = App.GetCursorPos();
    FVector2D NewPos = bAbsolute ? Delta : CursorPos + Delta;

    FPointerEvent Evt(
        /*PointerIndex*/ 0,
        /*ScreenSpacePosition*/ NewPos,
        /*LastScreenSpacePosition*/ CursorPos,
        /*PressedButtons*/ TSet<FKey>(),
        /*EffectingButton*/ EKeys::Invalid,
        /*WheelDelta*/ 0.f,
        /*ModifierKeys*/ App.GetModifierKeys()
    );
    App.SetCursorPos(NewPos);
    return App.ProcessMouseMoveEvent(Evt);
}

bool FAgentInput::InjectMouseButton(EAgentMouseButton Btn, bool bPressed)
{
    FKey Key = MouseButtonToKey(Btn);
    if (!Key.IsValid()) { return false; }
    FSlateApplication& App = FSlateApplication::Get();
    FVector2D CursorPos = App.GetCursorPos();

    FPointerEvent Evt(
        0, CursorPos, CursorPos, TSet<FKey>{Key}, Key, 0.f, App.GetModifierKeys()
    );
    if (bPressed) { return App.ProcessMouseButtonDownEvent(nullptr, Evt); }
    return App.ProcessMouseButtonUpEvent(Evt);
}

bool FAgentInput::InjectAxis(FName AxisName, float Value)
{
    // UE 5.6 removed APlayerController::InputAxis. Route axis injection via Slate
    // analog input event; works for any FKey-resolvable axis (gamepad sticks,
    // mouse axes, plus named axes that have been promoted to FKeys).
    FKey Key(AxisName);
    if (!Key.IsValid()) { return false; }
    FSlateApplication& App = FSlateApplication::Get();
    FAnalogInputEvent Evt(Key, App.GetModifierKeys(), 0, false, 0, 0, Value);
    return App.ProcessAnalogInputEvent(Evt);
}

bool FAgentInput::InjectGamepad(EAgentGamepadButton Btn, bool bPressed, float AnalogValue)
{
    FKey Key = GamepadButtonToKey(Btn);
    if (!Key.IsValid()) { return false; }

    FSlateApplication& App = FSlateApplication::Get();
    if (Btn >= EAgentGamepadButton::LeftStickX && Btn <= EAgentGamepadButton::RightStickY)
    {
        FAnalogInputEvent Evt(Key, App.GetModifierKeys(), 0, false, 0, 0, AnalogValue);
        return App.ProcessAnalogInputEvent(Evt);
    }
    FKeyEvent Evt(Key, App.GetModifierKeys(), 0, false, 0, 0);
    if (bPressed) { return App.ProcessKeyDownEvent(Evt); }
    return App.ProcessKeyUpEvent(Evt);
}
