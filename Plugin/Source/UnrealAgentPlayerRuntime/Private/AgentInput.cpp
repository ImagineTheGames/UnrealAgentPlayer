#include "AgentInput.h"

#include "UnrealAgentPlayerRuntimeModule.h"
#include "AgentWorld.h"
#include "Framework/Application/SlateApplication.h"
#include "Framework/Application/SlateUser.h"
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

namespace
{
    /**
     * The refusal for "this event would be stamped with a Slate user index nothing is listening
     * on". Three parts, per the house rule in agent-testing/agentplayertest.md ("Adding a verb?
     * What a good failure message contains"): name the mismatch, say why the plausible substitute
     * is wrong, give the exact remedy.
     *
     * It exists because the alternative -- what this tool did before -- is a SILENT discard.
     * Slate throws the event away before any handler runs and the call still reports success, so
     * it reads as a broken feature: a stick drive against a Slate analog cursor produced
     * pixel-identical before/after screenshots and an unchanged GetMousePosition(), and nearly
     * became a bug filed against a fix that was working.
     */
    FString UAPUserIndexRefusal(int32 Requested)
    {
        return FString::Printf(
            TEXT("Slate has no registered user %d, so an event stamped with that index is ")
            TEXT("DISCARDED before any handler runs -- FAnalogCursor::IsRelevantInput() is ")
            TEXT("GetOwnerUserIndex() == InputEvent.GetUserIndex() (engine AnalogCursor.cpp:192), ")
            TEXT("and every Slate handler that filters by user does the same. Registered Slate ")
            TEXT("users right now: %s. Do NOT just drop the user index and retry: with no index ")
            TEXT("this call takes the game-viewport route, which never enters the Slate ")
            TEXT("pre-processor chain at all, so an analog/virtual cursor still sees nothing and ")
            TEXT("the call still reports ok. Re-run with --user <N> using an index from that list ")
            TEXT("-- the one that OWNS the pre-processor you are driving (for a single local ")
            TEXT("player that is 0)."),
            Requested, *FAgentInput::DescribeSlateUsers());
    }
}

FString FAgentInput::DescribeSlateUsers()
{
    if (!FSlateApplication::IsInitialized()) { return TEXT("(Slate is not initialised)"); }

    TArray<FString> Parts;
    FSlateApplication::Get().ForEachUser([&Parts](FSlateUser& User)
    {
        FString Focus = TEXT("no focus");
        if (TSharedPtr<SWidget> Focused = User.GetFocusedWidget())
        {
            Focus = FString::Printf(TEXT("focus: %s"), *Focused->GetType().ToString());
        }
        Parts.Add(FString::Printf(TEXT("%d (%s)"), User.GetUserIndex(), *Focus));
    }, /*bIncludeVirtualUsers*/ false);

    return Parts.Num() > 0 ? FString::Join(Parts, TEXT(", ")) : TEXT("(none)");
}

bool FAgentInput::ResolveSlateUserParam(const FString& SlateUser, int32& OutResolved,
                                        FString& OutError)
{
    OutResolved = INDEX_NONE;
    if (SlateUser.IsEmpty()) { return true; }   // omitted == auto; see the header note

    if (!SlateUser.IsNumeric())
    {
        OutError = FString::Printf(
            TEXT("SlateUser must be a Slate user INDEX (e.g. \"0\") or empty for automatic ")
            TEXT("resolution; got '%s'. It is not a player name, a controller id or a pawn -- ")
            TEXT("it is the index Slate stamps on input events and that handlers filter on ")
            TEXT("(FAnalogCursor::IsRelevantInput, engine AnalogCursor.cpp:192). Registered ")
            TEXT("Slate users right now: %s."), *SlateUser, *DescribeSlateUsers());
        return false;
    }

    const int32 Requested = FCString::Atoi(*SlateUser);
    OutResolved = ResolveSlateUserIndex(Requested, OutError);
    return OutResolved != INDEX_NONE;
}

int32 FAgentInput::ResolveSlateUserIndex(int32 RequestedIndex, FString& OutError)
{
    if (!FSlateApplication::IsInitialized())
    {
        OutError = TEXT("Slate is not initialised in this process, so there is no Slate user to "
                        "stamp and no pre-processor chain to reach. A user index only exists on "
                        "the Slate layer -- the game-viewport route cannot carry one, so there is "
                        "no substitute here. Run this against a live editor / PIE session "
                        "(uap pie start), not a headless commandlet.");
        return INDEX_NONE;
    }
    FSlateApplication& App = FSlateApplication::Get();

    if (RequestedIndex != INDEX_NONE)
    {
        // An index Slate has no user for cannot receive anything. Returning it anyway is
        // exactly the silent discard this function exists to stop, so refuse instead.
        if (RequestedIndex < 0 || !App.GetUser(RequestedIndex).IsValid())
        {
            OutError = UAPUserIndexRefusal(RequestedIndex);
            return INDEX_NONE;
        }
        return RequestedIndex;
    }

    // Auto. First choice: the user whose FOCUS PATH contains the game viewport widget. That is
    // the user whose input the game is actually routing, and it is the only one of the three
    // that stays right under splitscreen / a second Slate user.
    int32 Resolved = INDEX_NONE;
    if (TSharedPtr<SWidget> Viewport = FindPIEViewportWidget())
    {
        TSharedPtr<const SWidget> ViewportConst = Viewport;
        App.ForEachUser([&Resolved, &ViewportConst](FSlateUser& User)
        {
            if (Resolved == INDEX_NONE && User.IsWidgetInFocusPath(ViewportConst))
            {
                Resolved = User.GetUserIndex();
            }
        }, /*bIncludeVirtualUsers*/ false);
    }

    // Then the keyboard user (what UUAPAgentSubsystem::NavigateUI already uses), then whatever
    // user index 0 is -- Slate's cursor user, guaranteed to exist while Slate is up. Every step
    // is VALIDATED against GetUser() so a fallback can never hand back an index nothing owns.
    if (Resolved == INDEX_NONE)
    {
        const int32 Keyboard = App.GetUserIndexForKeyboard();
        if (Keyboard >= 0 && App.GetUser(Keyboard).IsValid()) { Resolved = Keyboard; }
    }
    if (Resolved == INDEX_NONE && App.GetUser((int32)0).IsValid())
    {
        Resolved = 0;   // FSlateApplication::CursorUserIndex
    }

    if (Resolved == INDEX_NONE)
    {
        OutError = FString::Printf(
            TEXT("Slate is up but has NO registered users, so every Slate-layer event is ")
            TEXT("discarded whatever index it carries -- there is no index that works. Do not ")
            TEXT("retry on the viewport route instead: it cannot reach a Slate pre-processor at ")
            TEXT("all, so it would report ok and change nothing. Start a play session first ")
            TEXT("(uap pie start) and re-run. Registered Slate users: %s."),
            *DescribeSlateUsers());
    }
    return Resolved;
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

bool FAgentInput::InjectAxisSlate(FKey Key, float Value, int32 UserIndex)
{
    // The OTHER analog route: into FSlateApplication, where the input pre-processor chain runs.
    // This is the only route an FAnalogCursor / virtual cursor can see -- the viewport route
    // above enters BELOW the pre-processors. Stamping the wrong user here is not a soft failure:
    // the event is dropped before the pre-processor is asked, so resolve or refuse.
    FString UserError;
    const int32 User = ResolveSlateUserIndex(UserIndex, UserError);
    if (User == INDEX_NONE)
    {
        UE_LOG(LogUAPRuntime, Error, TEXT("InjectAxis (Slate route, %s): %s"),
               *Key.ToString(), *UserError);
        return false;
    }

    FSlateApplication& App = FSlateApplication::Get();
    FAnalogInputEvent Evt(Key, App.GetModifierKeys(), (uint32)User, /*bIsRepeat*/ false, 0, 0, Value);
    // Discarding "handled" for the same reason InjectKey does: a pre-processor that returns
    // false has still SEEN the event and let it fall through. What the caller needs is whether
    // it was delivered to the right user, and by here it was.
    App.ProcessAnalogInputEvent(Evt);
    return true;
}


bool FAgentInput::InjectMouseMove(FVector2D Delta, bool bAbsolute, int32 UserIndex)
{
    TSharedPtr<SWidget> Target = FindPIEViewportWidget();
    if (!Target.IsValid()) { return false; }

    FString UserError;
    const int32 User = ResolveSlateUserIndex(UserIndex, UserError);
    if (User == INDEX_NONE)
    {
        UE_LOG(LogUAPRuntime, Error, TEXT("InjectMouseMove: %s"), *UserError);
        return false;
    }
    FSlateApplication& App = FSlateApplication::Get();

    FVector2D CursorPos = App.GetCursorPos();
    FVector2D NewPos = bAbsolute ? Delta : CursorPos + Delta;

    // The 7-arg FPointerEvent ctor hardcodes the user index to 0 in the ENGINE
    // (FInputEvent(InModifierKeys, 0, false) -- SlateCore Events.h:730), so "not passing a
    // user" was never neutral: it silently meant user 0. The 8-arg overload takes one.
    const TSet<FKey> NoButtons;   // named: FPointerEvent keeps a pointer to this (see below)
    FPointerEvent Evt(
        /*UserIndex*/ (uint32)User,
        /*PointerIndex*/ 0u,
        /*ScreenSpacePosition*/ NewPos,
        /*LastScreenSpacePosition*/ CursorPos,
        /*PressedButtons*/ NoButtons,
        /*EffectingButton*/ EKeys::Invalid,
        /*WheelDelta*/ 0.f,
        /*ModifierKeys*/ App.GetModifierKeys()
    );
    App.SetCursorPos(NewPos);
    return App.ProcessMouseMoveEvent(Evt);
}

bool FAgentInput::InjectMouseButton(EAgentMouseButton Btn, bool bPressed, int32 UserIndex)
{
    FKey Key = MouseButtonToKey(Btn);
    if (!Key.IsValid()) { return false; }

    FString UserError;
    const int32 User = ResolveSlateUserIndex(UserIndex, UserError);
    if (User == INDEX_NONE)
    {
        UE_LOG(LogUAPRuntime, Error, TEXT("InjectMouseButton: %s"), *UserError);
        return false;
    }
    FSlateApplication& App = FSlateApplication::Get();
    FVector2D CursorPos = App.GetCursorPos();

    // Named, not a temporary: FPointerEvent stores a POINTER to this set
    // (PressedButtons(&InPressedButtons) -- SlateCore Events.h), so a temporary would dangle
    // by the time the event is processed on the next line.
    const TSet<FKey> Pressed{Key};
    FPointerEvent Evt(
        (uint32)User, /*PointerIndex*/ 0u, CursorPos, CursorPos,
        Pressed, Key, 0.f, App.GetModifierKeys()
    );
    if (bPressed) { return App.ProcessMouseButtonDownEvent(nullptr, Evt); }
    return App.ProcessMouseButtonUpEvent(Evt);
}

bool FAgentInput::InjectAxis(FName AxisName, float Value, int32 UserIndex)
{
    // UE 5.6 removed APlayerController::InputAxis, so there are two routes and they reach
    // DIFFERENT layers -- see InjectAxisKey (game viewport, below Slate) and InjectAxisSlate
    // (into the pre-processor chain).
    FKey Key(AxisName);
    if (!Key.IsValid()) { return false; }

    if (UserIndex != INDEX_NONE)
    {
        // An explicit user is an explicit LAYER: only the Slate route carries a user index.
        // Deliberately NOT falling through to the viewport route on failure -- that route
        // cannot reach the handler the caller named, so "succeeding" there would be a silent
        // wrong answer wearing a success.
        return InjectAxisSlate(Key, Value, UserIndex);
    }
    if (InjectAxisKey(Key, Value)) { return true; }
    return InjectAxisSlate(Key, Value, INDEX_NONE);
}

bool FAgentInput::InjectGamepad(EAgentGamepadButton Btn, bool bPressed, float AnalogValue,
                                int32 UserIndex)
{
    FKey Key = GamepadButtonToKey(Btn);
    if (!Key.IsValid()) { return false; }

    if (Btn >= EAgentGamepadButton::LeftStickX && Btn <= EAgentGamepadButton::RightStickY)
    {
        // Sticks are analog: see InjectAxis for which route each UserIndex selects.
        return InjectAxis(Key.GetFName(), AnalogValue, UserIndex);
    }
    // Buttons stay on the Slate path deliberately: a gamepad face/DPad press is also how UMG
    // focus navigation is driven, and Slate is where that is handled. Use `uap input hold` /
    // InjectKey for a gamepad button that must reach gameplay input directly.
    FString UserError;
    const int32 User = ResolveSlateUserIndex(UserIndex, UserError);
    if (User == INDEX_NONE)
    {
        UE_LOG(LogUAPRuntime, Error, TEXT("InjectGamepad (%s): %s"), *Key.ToString(), *UserError);
        return false;
    }
    FSlateApplication& App = FSlateApplication::Get();
    FKeyEvent Evt(Key, App.GetModifierKeys(), (uint32)User, false, 0, 0);
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
        // Which ROUTE to re-assert on, and as whom. INDEX_NONE = game viewport (below Slate);
        // >= 0 = the Slate route stamped for that user, the only one a pre-processor sees.
        // Held on the entry so the release at the end goes back out the SAME way it went in:
        // recentring an analog axis on the other route leaves the first one latched.
        int32  SlateUserIndex = INDEX_NONE;
    };

    TArray<FUAPHeldInput> GHeldInputs;
    FTSTicker::FDelegateHandle GHoldTicker;

    void UAPHoldInject(const FUAPHeldInput& H, float Value)
    {
        if (H.SlateUserIndex != INDEX_NONE)
        {
            FAgentInput::InjectAxisSlate(H.Key, Value, H.SlateUserIndex);
        }
        else
        {
            FAgentInput::InjectAxisKey(H.Key, Value);
        }
    }

    void UAPReleaseOne(const FUAPHeldInput& H)
    {
        if (H.bAnalog) { UAPHoldInject(H, 0.f); }
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
                UAPHoldInject(H, H.Value);
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
    H.SlateUserIndex = INDEX_NONE;   // digital keys go out the viewport route only
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

bool FAgentInput::HoldAxis(FKey Key, float Value, float Seconds, int32 SlateUserIndex)
{
    if (!Key.IsValid() || Seconds <= 0.f || !HasLiveViewport()) { return false; }

    FUAPHeldInput& H = UAPFindOrAddHold(Key);
    H.bAnalog = true;
    H.Value = Value;
    H.bStarted = true;
    H.SlateUserIndex = SlateUserIndex;
    H.EndRealTime = FPlatformTime::Seconds() + Seconds;
    UAPEnsureHoldTicker();

    const bool bInjected = (SlateUserIndex != INDEX_NONE)
        ? InjectAxisSlate(Key, Value, SlateUserIndex)
        : InjectAxisKey(Key, Value);
    if (!bInjected)
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
    O->SetStringField(TEXT("route"), TEXT("viewport"));
    return UAPJson(O);
}

FString FAgentInput::HoldAxisJson(const FString& AxisKeyName, float Value, float Seconds,
                                  const FString& SlateUser)
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

    // Resolve the Slate user BEFORE anything is injected, like every other refusal reason here:
    // a refused call must have zero side effects. This is also the one refusal that would
    // otherwise not exist at all -- Slate discards a mis-stamped event in silence, so without
    // this check the caller gets ok:true and no movement, which reads as a broken feature.
    int32 SlateUserIdx = INDEX_NONE;
    {
        FString UserError;
        if (!ResolveSlateUserParam(SlateUser, SlateUserIdx, UserError))
        {
            return UAPRefuse(AxisKeyName, UserError);
        }
    }

    if (!HoldAxis(Key, Value, Seconds, SlateUserIdx))
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
    // Which LAYER this hold is actually driving. Reported always, not only on request: the
    // difference between these two is invisible from outside and is what made the original
    // defect look like a product bug.
    O->SetStringField(TEXT("route"), SlateUserIdx != INDEX_NONE ? TEXT("slate") : TEXT("viewport"));
    if (SlateUserIdx != INDEX_NONE) { O->SetNumberField(TEXT("user_index"), SlateUserIdx); }
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
        Info.SlateUserIndex = H.SlateUserIndex;
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
        // Which layer this hold is being re-asserted on. A Slate pre-processor (analog or
        // virtual cursor) only ever sees route "slate"; "viewport" reaching nothing is the
        // shape of the original silent defect, and this is how you see it without guessing.
        O->SetStringField(TEXT("route"),
                          H.SlateUserIndex != INDEX_NONE ? TEXT("slate") : TEXT("viewport"));
        if (H.SlateUserIndex != INDEX_NONE)
        {
            O->SetNumberField(TEXT("user_index"), H.SlateUserIndex);
        }
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
