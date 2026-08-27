#include "AgentInput.h"

#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentWorld.h"
#include "Framework/Application/SlateApplication.h"
#include "GenericPlatform/GenericApplication.h"
#include "Engine/GameViewportClient.h"
#include "Engine/World.h"
#include "GameFramework/PlayerController.h"
#include "GameFramework/PlayerInput.h"
#include "Widgets/SViewport.h"
#include "InputKeyEventArgs.h"
#include "GenericPlatform/GenericPlatformInputDeviceMapper.h"
#include "HAL/PlatformTime.h"
#include "Containers/Ticker.h"
#include "Misc/App.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

TSharedPtr<SWidget> FAgentInput::FindPIEViewportWidget()
{
    UGameViewportClient* GV = FAgentWorld::GetActiveGameViewport();
    return GV ? GV->GetGameViewportWidget() : nullptr;
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

bool FAgentInput::HasLiveViewport()
{
    UGameViewportClient* GV = FAgentWorld::GetActiveGameViewport();
    return GV && GV->Viewport && !GV->IgnoreInput();
}

bool FAgentInput::IsKeyDown(FKey Key)
{
    UWorld* World = FAgentWorld::GetActiveGameWorld();
    APlayerController* PC = World ? World->GetFirstPlayerController() : nullptr;
    return PC ? PC->IsInputKeyDown(Key) : false;
}

bool FAgentInput::InjectKey(FKey Key, bool bPressed, bool bRepeat)
{
    // Route the key straight through the game viewport client -> PlayerController ->
    // (Enhanced)Input -- the same call a focused SViewport makes on a real keypress, but
    // invoked directly so it does NOT depend on Slate keyboard focus or this editor being
    // the OS-foreground window. The old path (SetAllUserFocus + ProcessKeyDownEvent) routed
    // down the Slate focus path, which silently dropped the key whenever the PIE viewport
    // was not in the focus path -- i.e. whenever the editor was backgrounded or PIE played
    // inside the level viewport. This path works whether or not the editor is foreground,
    // so headless / multi-editor auto-testing reaches the game. (Still fully in-process; two
    // editors in separate processes each drive their own game independently.)
    if (!HasLiveViewport()) { return false; }
    UGameViewportClient* GV = FAgentWorld::GetActiveGameViewport();

    // bRepeat sends IE_Repeat instead of IE_Pressed. That is what a real held key does every
    // frame, and it is not cosmetic: UPlayerInput::InputKey re-latches a key it sees repeat on
    // (bAutoReconcilePressedEventsOnFirstRepeat), so a repeat stream survives a FlushPressedKeys
    // that would otherwise have silently dropped the hold. Sending only one IE_Pressed and never
    // a repeat is why an injected key could move the player exactly once and then die.
    const EInputEvent Evt = bPressed ? (bRepeat ? IE_Repeat : IE_Pressed) : IE_Released;

    FInputKeyEventArgs Args(
        GV->Viewport,
        IPlatformInputDeviceMapper::Get().GetDefaultInputDevice(),
        Key,
        Evt,
        /*AmountDepressed*/ bPressed ? 1.0f : 0.0f,
        /*bIsTouchEvent*/ false,
        /*EventTimestamp*/ FPlatformTime::Cycles64());

    // DELIBERATELY discarding InputKey's return value. It forwards UPlayerInput::InputKey,
    // which for IE_Pressed returns IsKeyHandledByAction(Key) -- a lookup in the LEGACY
    // ActionMappings array only. A project on Enhanced Input has no legacy mappings, so a
    // perfectly delivered keypress reports false. Treating that as failure is exactly what
    // leaked a stuck key: `hold C` pressed C (the pawn crouched), read false, bailed out
    // before registering the hold, and nothing ever released it. What the caller needs to
    // know is whether the event was DELIVERED, which is what we return.
    GV->InputKey(Args);
    return true;
}

bool FAgentInput::InjectAxisKey(FKey Key, float Value)
{
    // Analog sample routed through the game viewport (UGameViewportClient::InputAxis ->
    // PlayerController -> (Enhanced)Input), for the same reason InjectKey does: the Slate
    // path (ProcessAnalogInputEvent) only lands when the PIE viewport is in the keyboard
    // focus path, so it silently dropped VR thumbstick / gamepad stick injection whenever
    // the editor was backgrounded or PIE played inside the level viewport.
    if (!HasLiveViewport()) { return false; }
    UGameViewportClient* GV = FAgentWorld::GetActiveGameViewport();

    FInputKeyEventArgs Args(
        GV->Viewport,
        IPlatformInputDeviceMapper::Get().GetDefaultInputDevice(),
        Key,
        /*Delta*/ Value,
        /*DeltaTime*/ (float)FApp::GetDeltaTime(),
        /*NumSamples*/ 1,
        /*EventTimestamp*/ FPlatformTime::Cycles64());
    GV->InputAxis(Args);   // return value is "handled", not "delivered" -- see InjectKey.
    return true;
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
    // UE 5.6 removed APlayerController::InputAxis. Route the sample through the game
    // viewport (InjectAxisKey); fall back to the Slate analog path only when there is no
    // game viewport at all (e.g. driving editor-only widgets).
    FKey Key(AxisName);
    if (!Key.IsValid()) { return false; }
    if (InjectAxisKey(Key, Value)) { return true; }
    FSlateApplication& App = FSlateApplication::Get();
    FAnalogInputEvent Evt(Key, App.GetModifierKeys(), 0, false, 0, 0, Value);
    return App.ProcessAnalogInputEvent(Evt);
}

bool FAgentInput::InjectGamepad(EAgentGamepadButton Btn, bool bPressed, float AnalogValue)
{
    FKey Key = GamepadButtonToKey(Btn);
    if (!Key.IsValid()) { return false; }

    if (Btn >= EAgentGamepadButton::LeftStickX && Btn <= EAgentGamepadButton::RightStickY)
    {
        // Sticks are analog: go through the viewport path (see InjectAxisKey).
        return InjectAxis(Key.GetFName(), AnalogValue);
    }
    // Buttons stay on the Slate path deliberately: a gamepad face/DPad press is also how UMG
    // focus navigation is driven, and Slate is where that is handled. Use `uap input hold` /
    // InjectKey for a gamepad button that must reach gameplay input directly.
    FSlateApplication& App = FSlateApplication::Get();
    FKeyEvent Evt(Key, App.GetModifierKeys(), 0, false, 0, 0);
    if (bPressed) { return App.ProcessKeyDownEvent(Evt); }
    return App.ProcessKeyUpEvent(Evt);
}


// --- Held input -----------------------------------------------------------------------

namespace
{
    struct FUAPHeldInput
    {
        FKey   Key;
        bool   bAnalog = false;
        float  Value = 0.f;
        double EndRealTime = 0.0;
        bool   bStarted = false;   // digital: IE_Pressed sent once, then IE_Repeat
    };

    TArray<FUAPHeldInput> GHeldInputs;
    FTSTicker::FDelegateHandle GHoldTicker;

    void UAPReleaseOne(const FUAPHeldInput& H)
    {
        if (H.bAnalog) { FAgentInput::InjectAxisKey(H.Key, 0.f); }
        else if (H.bStarted) { FAgentInput::InjectKey(H.Key, /*bPressed*/ false, /*bRepeat*/ false); }
    }

    bool UAPHoldTick(float /*DeltaTime*/)
    {
        // No game viewport (PIE ended / not started) -- nothing can be held; forget everything
        // rather than spinning forever re-injecting into nothing.
        if (!FAgentInput::HasLiveViewport())
        {
            GHeldInputs.Reset();
        }

        const double Now = FPlatformTime::Seconds();
        for (int32 i = GHeldInputs.Num() - 1; i >= 0; --i)
        {
            FUAPHeldInput& H = GHeldInputs[i];
            if (Now >= H.EndRealTime)
            {
                UAPReleaseOne(H);
                GHeldInputs.RemoveAt(i);
                continue;
            }
            if (H.bAnalog)
            {
                FAgentInput::InjectAxisKey(H.Key, H.Value);
            }
            else
            {
                FAgentInput::InjectKey(H.Key, /*bPressed*/ true, /*bRepeat*/ H.bStarted);
                H.bStarted = true;
            }
        }

        if (GHeldInputs.Num() == 0)
        {
            GHoldTicker.Reset();
            return false;   // returning false unregisters this ticker
        }
        return true;
    }

    void UAPEnsureHoldTicker()
    {
        if (!GHoldTicker.IsValid())
        {
            // Core ticker runs once per frame on the game thread, before the world tick --
            // so the value is in place by the time UPlayerInput::ProcessInputStack reads it.
            GHoldTicker = FTSTicker::GetCoreTicker().AddTicker(
                FTickerDelegate::CreateStatic(&UAPHoldTick), 0.f);
        }
    }

    FUAPHeldInput& UAPFindOrAddHold(FKey Key)
    {
        for (FUAPHeldInput& H : GHeldInputs)
        {
            if (H.Key == Key) { return H; }
        }
        FUAPHeldInput New;
        New.Key = Key;
        return GHeldInputs[GHeldInputs.Add(New)];
    }

    bool UAPIsHeld(FKey Key)
    {
        for (const FUAPHeldInput& H : GHeldInputs)
        {
            if (H.Key == Key) { return true; }
        }
        return false;
    }

    FString UAPJson(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, W);
        return Out;
    }

    /**
     * Refusal envelope. "pressed":false is part of the contract, not decoration: a refused
     * call must have had ZERO side effects, and this is how a caller (and a test) can assert
     * that no key was left down. "error" says what actually went wrong -- a guessed message
     * ("unknown key name") sent a real investigation after a nonexistent validation table.
     */
    FString UAPRefuse(const FString& KeyName, const FString& Error)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("ok"), false);
        O->SetStringField(TEXT("key"), KeyName);
        O->SetStringField(TEXT("error"), Error);
        O->SetBoolField(TEXT("pressed"), false);
        return UAPJson(O);
    }

    /**
     * Validate a key name and a duration BEFORE anything is injected. FKey::IsValid consults
     * the engine own FKey registry (EKeys), so every real key -- C, LeftControl, the
     * OculusTouch_* set -- is accepted; there is no hand-maintained allow-list to drift.
     */
    bool UAPValidateHold(const FString& KeyName, float Seconds, FKey& OutKey, FString& OutError)
    {
        OutKey = FKey(*KeyName);
        if (KeyName.IsEmpty() || !OutKey.IsValid())
        {
            OutError = FString::Printf(
                TEXT("no key named '%s' in this engine's FKey registry. Use the exact FKey name ")
                TEXT("(e.g. W, C, LeftControl, SpaceBar, Gamepad_LeftY, ")
                TEXT("OculusTouch_Left_Thumbstick_Y)."), *KeyName);
            return false;
        }
        if (Seconds <= 0.f)
        {
            OutError = FString::Printf(TEXT("Seconds must be > 0 (got %f)"), Seconds);
            return false;
        }
        if (!FAgentInput::HasLiveViewport())
        {
            OutError = TEXT("no live game viewport is accepting input -- start PIE first "
                            "(uap pie start), and check the viewport is not ignoring input");
            return false;
        }
        return true;
    }
}

bool FAgentInput::HoldKey(FKey Key, float Seconds)
{
    if (!Key.IsValid() || Seconds <= 0.f || !HasLiveViewport()) { return false; }

    // Register BEFORE pressing. If anything below goes wrong the ticker still owns the key
    // and will release it, so a press can never escape the registry's knowledge -- the exact
    // failure that left a key stuck down while `input status` reported nothing held.
    FUAPHeldInput& H = UAPFindOrAddHold(Key);
    H.bAnalog = false;
    H.Value = 1.f;
    H.bStarted = true;
    H.EndRealTime = FPlatformTime::Seconds() + Seconds;
    UAPEnsureHoldTicker();

    // Press immediately so the caller sees the effect without waiting a frame; the ticker
    // then keeps it alive with IE_Repeat until the duration expires.
    if (!InjectKey(Key, /*bPressed*/ true, /*bRepeat*/ false))
    {
        ReleaseHeld(Key);   // unwind: never leave a half-started hold behind
        return false;
    }
    return true;
}

bool FAgentInput::HoldAxis(FKey Key, float Value, float Seconds)
{
    if (!Key.IsValid() || Seconds <= 0.f || !HasLiveViewport()) { return false; }

    FUAPHeldInput& H = UAPFindOrAddHold(Key);
    H.bAnalog = true;
    H.Value = Value;
    H.bStarted = true;
    H.EndRealTime = FPlatformTime::Seconds() + Seconds;
    UAPEnsureHoldTicker();

    if (!InjectAxisKey(Key, Value))
    {
        ReleaseHeld(Key);
        return false;
    }
    return true;
}

FString FAgentInput::HoldKeyJson(const FString& KeyName, float Seconds)
{
    FKey Key;
    FString Error;
    if (!UAPValidateHold(KeyName, Seconds, Key, Error)) { return UAPRefuse(KeyName, Error); }

    if (!HoldKey(Key, Seconds))
    {
        return UAPRefuse(KeyName, TEXT("the game viewport rejected the press (input became "
                                       "unavailable between validation and injection)"));
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("key"), Key.ToString());
    O->SetNumberField(TEXT("seconds"), Seconds);
    O->SetBoolField(TEXT("pressed"), true);
    return UAPJson(O);
}

FString FAgentInput::HoldAxisJson(const FString& AxisKeyName, float Value, float Seconds)
{
    FKey Key;
    FString Error;
    if (!UAPValidateHold(AxisKeyName, Seconds, Key, Error)) { return UAPRefuse(AxisKeyName, Error); }
    if (!Key.IsAnalog())   // engine's own predicate: IsAxis1D() || IsAxis2D() || IsAxis3D()
    {
        return UAPRefuse(AxisKeyName, FString::Printf(
            TEXT("'%s' is a digital key, not an analog axis -- use `input hold` for it. Axis ")
            TEXT("keys look like Gamepad_LeftY or OculusTouch_Left_Thumbstick_Y."), *AxisKeyName));
    }

    if (!HoldAxis(Key, Value, Seconds))
    {
        return UAPRefuse(AxisKeyName, TEXT("the game viewport rejected the axis sample (input "
                                           "became unavailable between validation and injection)"));
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("key"), Key.ToString());
    O->SetNumberField(TEXT("value"), Value);
    O->SetNumberField(TEXT("seconds"), Seconds);
    O->SetBoolField(TEXT("pressed"), true);
    return UAPJson(O);
}

int32 FAgentInput::ReleaseHeld(FKey Key)
{
    int32 Count = 0;
    for (int32 i = GHeldInputs.Num() - 1; i >= 0; --i)
    {
        if (Key.IsValid() && GHeldInputs[i].Key != Key) { continue; }
        UAPReleaseOne(GHeldInputs[i]);
        GHeldInputs.RemoveAt(i);
        ++Count;
    }
    return Count;
}

int32 FAgentInput::FlushAllPressedKeys()
{
    // APlayerController::FlushPressedKeys sends IE_Released for every key it still has down
    // and clears the key-state map. This is the recovery hatch for a key the registry lost
    // track of -- without it, one stuck key silently corrupts every later test in the same
    // PIE session and only a PIE restart clears it.
    UWorld* World = FAgentWorld::GetActiveGameWorld();
    if (!World) { return 0; }
    int32 Count = 0;
    for (FConstPlayerControllerIterator It = World->GetPlayerControllerIterator(); It; ++It)
    {
        if (APlayerController* PC = It->Get())
        {
            PC->FlushPressedKeys();
            ++Count;
        }
    }
    return Count;
}

FString FAgentInput::ReleaseHeldJson(const FString& KeyName)
{
    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);

    if (KeyName.IsEmpty())
    {
        // Recovery path: clear the registry properly, then flush anything the engine still
        // holds down that the registry never knew about.
        const int32 Released = ReleaseHeld(FKey());
        const int32 Flushed = FlushAllPressedKeys();
        O->SetNumberField(TEXT("released"), Released);
        O->SetNumberField(TEXT("controllers_flushed"), Flushed);
        O->SetBoolField(TEXT("flushed"), Flushed > 0);
        return UAPJson(O);
    }

    const FKey Key(*KeyName);
    if (!Key.IsValid())
    {
        return UAPRefuse(KeyName, FString::Printf(
            TEXT("no key named '%s' in this engine's FKey registry."), *KeyName));
    }

    // Force-release: send IE_Released whether or not the registry knows about this key, so a
    // key that leaked outside the registry can still be cleared by name.
    const bool bWasHeld = UAPIsHeld(Key);
    const bool bDownBefore = IsKeyDown(Key);
    const int32 Released = ReleaseHeld(Key);
    if (!bWasHeld) { InjectKey(Key, /*bPressed*/ false, /*bRepeat*/ false); }

    O->SetStringField(TEXT("key"), Key.ToString());
    O->SetNumberField(TEXT("released"), Released);
    O->SetBoolField(TEXT("was_held"), bWasHeld);
    O->SetBoolField(TEXT("forced"), !bWasHeld);
    O->SetBoolField(TEXT("down_before"), bDownBefore);
    return UAPJson(O);
}

void FAgentInput::GetHeld(TArray<FAgentHeldInputInfo>& Out)
{
    const double Now = FPlatformTime::Seconds();
    Out.Reset(GHeldInputs.Num());
    for (const FUAPHeldInput& H : GHeldInputs)
    {
        FAgentHeldInputInfo Info;
        Info.Key = H.Key.ToString();
        Info.bAnalog = H.bAnalog;
        Info.Value = H.Value;
        Info.RemainingSeconds = (float)FMath::Max(0.0, H.EndRealTime - Now);
        Out.Add(Info);
    }
}

FString FAgentInput::GetHeldJson()
{
    TArray<FAgentHeldInputInfo> Held;
    GetHeld(Held);

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    TArray<TSharedPtr<FJsonValue>> Arr;
    for (const FAgentHeldInputInfo& H : Held)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("key"), H.Key);
        O->SetBoolField(TEXT("analog"), H.bAnalog);
        O->SetNumberField(TEXT("value"), H.Value);
        O->SetNumberField(TEXT("remaining_seconds"), H.RemainingSeconds);
        // Engine ground truth alongside the registry's view: if these ever disagree, the
        // registry has lost track of a key and `input release` (no key) is the recovery.
        O->SetBoolField(TEXT("down"), FAgentInput::IsKeyDown(FKey(*H.Key)));
        Arr.Add(MakeShared<FJsonValueObject>(O));
    }
    Root->SetBoolField(TEXT("ok"), true);
    Root->SetArrayField(TEXT("held"), Arr);
    return UAPJson(Root);
}

void FAgentInput::ShutdownHolds()
{
    GHeldInputs.Reset();
    if (GHoldTicker.IsValid())
    {
        FTSTicker::RemoveTicker(GHoldTicker);
        GHoldTicker.Reset();
    }
}
