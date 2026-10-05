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
#include "Widgets/SWindow.h"
#include "Layout/WidgetPath.h"
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


// The agent cursor. See the long note in AgentInput.h: under viewport capture this remembered
// position is the ONLY thing that decides where an injected pointer event lands, because the
// real cursor cannot be moved and Slate routes by the position carried on the event.
namespace
{
    FVector2D GAgentCursorPos = FVector2D::ZeroVector;
    bool      GAgentCursorSet = false;
}

FVector2D FAgentInput::GetAgentCursorPos()
{
    if (GAgentCursorSet) { return GAgentCursorPos; }
    // Never set -- fall back to the real cursor so behaviour without a `mouse move` is
    // unchanged from before this existed.
    return FSlateApplication::IsInitialized() ? FSlateApplication::Get().GetCursorPos()
                                              : FVector2D::ZeroVector;
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

    // The AGENT cursor, not App.GetCursorPos(): a relative move has to compose with where the
    // last injected move put us, and under capture the real cursor never went there.
    FVector2D CursorPos = GetAgentCursorPos();
    FVector2D NewPos = bAbsolute ? Delta : CursorPos + Delta;
    GAgentCursorPos = NewPos;
    GAgentCursorSet = true;

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
    // GetAgentCursorPos(), NOT App.GetCursorPos(). Under viewport capture the real cursor is
    // pinned (see the agent-cursor note in AgentInput.h), so reading it back here is what made
    // every injected click land at 0,0 while reporting ok:true -- ClickUp 17tm466fbyj.
    return InjectMouseButtonAt(Btn, bPressed, GetAgentCursorPos(), UserIndex);
}

bool FAgentInput::InjectMouseButtonAt(EAgentMouseButton Btn, bool bPressed, FVector2D ScreenPos,
                                      int32 UserIndex)
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

    // Named, not a temporary: FPointerEvent stores a POINTER to this set
    // (PressedButtons(&InPressedButtons) -- SlateCore Events.h), so a temporary would dangle
    // by the time the event is processed on the next line.
    const TSet<FKey> Pressed{Key};
    FPointerEvent Evt(
        (uint32)User, /*PointerIndex*/ 0u, ScreenPos, ScreenPos,
        Pressed, Key, 0.f, App.GetModifierKeys()
    );
    if (bPressed) { return App.ProcessMouseButtonDownEvent(nullptr, Evt); }
    return App.ProcessMouseButtonUpEvent(Evt);
}

FString FAgentInput::DescribeWidgetsAt(FVector2D ScreenPos, int32 UserIndex)
{
    if (!FSlateApplication::IsInitialized()) { return FString(); }
    FSlateApplication& App = FSlateApplication::Get();

    // The same lookup ProcessMouseButtonDownEvent performs for a real click
    // (SlateApplication.cpp:5282), so what this reports is what the click will actually hit.
    FWidgetPath Path = App.LocateWindowUnderMouse(
        ScreenPos, App.GetInteractiveTopLevelWindows(), /*bIgnoreEnabledStatus*/ false, UserIndex);
    if (!Path.IsValid() || Path.Widgets.Num() == 0) { return FString(); }

    // Leafmost few only: the full path is a 40-deep chain of layout panels nobody reads.
    TArray<FString> Types;
    const int32 First = FMath::Max(0, Path.Widgets.Num() - 5);
    for (int32 i = First; i < Path.Widgets.Num(); ++i)
    {
        Types.Add(Path.Widgets[i].Widget->GetType().ToString());
    }
    return FString::Join(Types, TEXT(" > "));
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

// --- Mouse position ---------------------------------------------------------------------
// Read the design note at the top of AgentInput.h before changing any of this. The short
// version: the real cursor cannot be moved while the PIE viewport holds the mouse, so these
// do not try to move it -- they stamp a position onto the injected events instead, and report
// a read-back of every layer so "asked for" is never mistaken for "took".

namespace
{
    /** Absolute screen point -> the PIE viewport's local pixels, for SetMouseLocation. */
    bool UAPAbsoluteToViewportLocal(FVector2D Absolute, FVector2D& OutLocal)
    {
        TSharedPtr<SWidget> VP = FAgentWorld::GetActiveGameViewport()
            ? FAgentWorld::GetActiveGameViewport()->GetGameViewportWidget() : nullptr;
        if (!VP.IsValid()) { return false; }
        const UE::Slate::FDeprecateVector2DResult Local =
            VP->GetCachedGeometry().AbsoluteToLocal(Absolute);
        OutLocal = FVector2D(Local.X, Local.Y);
        return true;
    }

    /**
     * Set the GAME-side mouse cache and read it back. This one is genuinely settable under
     * capture -- APlayerController::SetMouseLocation goes to FSceneViewport::SetMouse, which
     * writes CachedCursorPos, and that is what GetMousePosition / GetHitResultUnderCursor
     * read. It is reported separately from the Slate side because the two can disagree and a
     * caller chasing a gameplay trace needs to know which one moved.
     */
    void UAPSetGameMouse(const TSharedRef<FJsonObject>& O, FVector2D Absolute)
    {
        UWorld* World = FAgentWorld::GetActiveGameWorld();
        APlayerController* PC = World ? World->GetFirstPlayerController() : nullptr;
        FVector2D Local;
        if (!PC || !UAPAbsoluteToViewportLocal(Absolute, Local))
        {
            O->SetBoolField(TEXT("game_mouse_set"), false);
            return;
        }
        PC->SetMouseLocation(FMath::RoundToInt(Local.X), FMath::RoundToInt(Local.Y));

        float MX = 0.f, MY = 0.f;
        const bool bRead = PC->GetMousePosition(MX, MY);
        O->SetBoolField(TEXT("game_mouse_set"),
                        bRead && FMath::Abs(MX - Local.X) <= 2.f && FMath::Abs(MY - Local.Y) <= 2.f);
        if (bRead)
        {
            O->SetNumberField(TEXT("game_mouse_x"), MX);
            O->SetNumberField(TEXT("game_mouse_y"), MY);
        }
    }

    FString UAPDescribeSlateWidget(const TSharedRef<SWidget>& W)
    {
        const FName Tag = W->GetTag();
        return Tag.IsNone() ? W->GetType().ToString()
                            : FString::Printf(TEXT("%s[%s]"), *W->GetType().ToString(),
                                              *Tag.ToString());
    }

    /**
     * Is a DISABLED widget in the way of a click at this point?
     *
     * Slate does not refuse a click on a disabled widget and it does not skip past it to
     * whatever is behind. FHittestGrid::GetBubblePath (engine
     * SlateCore/Private/Input/HittestGrid.cpp:228) TRUNCATES the hit path at the outermost
     * widget whose IsEnabled() is false, so the down/up are routed to that widget's PARENT
     * container. The container ignores them, nothing happens, and
     * FSlateApplication::ProcessMouseButtonDownEvent has no unhandled exit -- it returns true
     * unconditionally (SlateApplication.cpp:5193-5308) -- so the caller is told the event was
     * handled. That is why a click on a greyed Equip reported
     * clicked/down_handled/up_handled and did nothing [17tm466g0jf].
     *
     * Detected by doing the lookup BOTH ways: with the enabled gate the path is shorter, and
     * the first widget the gate dropped IS the disabled one.
     */
    bool UAPFindDisabledBlocker(FVector2D ScreenPos, int32 User, FString& OutBlocker,
                                FString& OutUngatedLeaf)
    {
        OutBlocker.Reset();
        OutUngatedLeaf.Reset();
        if (!FSlateApplication::IsInitialized()) { return false; }
        FSlateApplication& App = FSlateApplication::Get();

        FWidgetPath Ungated = App.LocateWindowUnderMouse(
            ScreenPos, App.GetInteractiveTopLevelWindows(), /*bIgnoreEnabledStatus*/ true, User);
        if (!Ungated.IsValid() || Ungated.Widgets.Num() == 0) { return false; }

        FWidgetPath Gated = App.LocateWindowUnderMouse(
            ScreenPos, App.GetInteractiveTopLevelWindows(), /*bIgnoreEnabledStatus*/ false, User);
        const int32 GatedNum = (Gated.IsValid() ? Gated.Widgets.Num() : 0);
        if (GatedNum >= Ungated.Widgets.Num()) { return false; }

        OutBlocker = UAPDescribeSlateWidget(Ungated.Widgets[GatedNum].Widget);
        OutUngatedLeaf = UAPDescribeSlateWidget(Ungated.Widgets.Last().Widget);
        return true;
    }

    /**
     * Report an EXISTING pointer capture. While a captor is held,
     * ProcessMouseButtonDownEvent takes the HasCapture branch and routes to the captor path
     * instead of hit-testing under the cursor (SlateApplication.cpp:5236), so a click goes
     * wherever the captor is regardless of where it was aimed -- and still reports true.
     * Reported rather than refused: a legitimate drag holds capture too.
     */
    void UAPReportPointerCapture(int32 User, const TSharedRef<FJsonObject>& O)
    {
        if (!FSlateApplication::IsInitialized()) { return; }
        TSharedPtr<FSlateUser> SlateUser = FSlateApplication::Get().GetUser(User);
        if (!SlateUser.IsValid() || !SlateUser->HasAnyCapture()) { return; }

        TArray<FWidgetPath> Captors = SlateUser->GetCaptorPaths();
        FString Holder = TEXT("<unresolved>");
        for (const FWidgetPath& P : Captors)
        {
            if (P.IsValid() && P.Widgets.Num() > 0)
            {
                Holder = UAPDescribeSlateWidget(P.Widgets.Last().Widget);
                break;
            }
        }
        O->SetStringField(TEXT("pointer_capture_holder"), Holder);
        O->SetStringField(TEXT("pointer_capture_warning"), FString::Printf(
            TEXT("Slate user %d already holds a POINTER CAPTURE ('%s'), so this event is routed ")
            TEXT("to the captor instead of to whatever is under the cursor -- and it will still ")
            TEXT("report handled. If UI clicks have gone inert, this is why. `uap input release` ")
            TEXT("does NOT clear a Slate captor (measured: it reports released 0 and the captor ")
            TEXT("survives); use `uap rc ReleaseSlatePointerCapture`."), User, *Holder));
    }

    /** Shared body of `mouse move` and the implicit move inside `mouse click`. */
    FString UAPMoveAgentCursor(FVector2D Target, int32 User, const TSharedRef<FJsonObject>& O)
    {
        FSlateApplication& App = FSlateApplication::Get();

        // 1. The agent cursor. This is the one that decides where a click lands.
        GAgentCursorPos = Target;
        GAgentCursorSet = true;
        O->SetNumberField(TEXT("x"), Target.X);
        O->SetNumberField(TEXT("y"), Target.Y);

        // 2. Ask the platform cursor as well, then READ IT BACK. Under capture this does
        //    nothing, and saying so is the point: a caller who needs the real pointer (an OS
        //    drag, a native tooltip) has to know it did not move, and a caller who only needs
        //    a click can see that it does not matter.
        App.SetCursorPos(Target);
        const FVector2D OsAfter = App.GetCursorPos();
        const bool bOsMoved = FVector2D::Distance(OsAfter, Target) <= 2.0;
        O->SetBoolField(TEXT("os_cursor_moved"), bOsMoved);
        O->SetNumberField(TEXT("os_cursor_x"), OsAfter.X);
        O->SetNumberField(TEXT("os_cursor_y"), OsAfter.Y);

        // 3. The game-side cache (GetMousePosition / GetHitResultUnderCursor).
        UAPSetGameMouse(O, Target);

        // 4. A real move event at the target, so hover / OnMouseEnter fire like a user's.
        const TSet<FKey> NoButtons;   // named: FPointerEvent keeps a pointer to it
        FPointerEvent Move((uint32)User, /*PointerIndex*/ 0u, Target, Target, NoButtons,
                           EKeys::Invalid, 0.f, App.GetModifierKeys());
        O->SetBoolField(TEXT("hover_delivered"), App.ProcessMouseMoveEvent(Move));

        // 5. WHAT IS ACTUALLY THERE. The same lookup the click will do. An empty path, or one
        //    ending at SViewport, means the click will hit the 3D scene and no UI -- report it
        //    rather than let a bare ok:true read as "the element was clicked".
        const FString Hit = FAgentInput::DescribeWidgetsAt(Target, User);
        O->SetStringField(TEXT("hit"), Hit);

        // WHY the chain ends where it does. A path that stops at a layout panel because
        // something disabled sits in front of it looks identical to a path that simply has no
        // button there, and that ambiguity is the whole of 17tm466g0jf.
        FString Blocker, UngatedLeaf;
        if (UAPFindDisabledBlocker(Target, User, Blocker, UngatedLeaf))
        {
            O->SetStringField(TEXT("disabled_widget"), Blocker);
            O->SetStringField(TEXT("would_hit_if_enabled"), UngatedLeaf);
        }
        return Hit;
    }

    /** "" -> unset; otherwise a number. Strings, because RC zero-init makes 0.0 indistinguishable
        from "omitted" and 0,0 is the top-left corner -- the exact wrong answer this verb ends. */
    bool UAPParseOptionalCoord(const FString& In, float& Out, bool& bPresent, FString& OutError)
    {
        bPresent = false;
        if (In.TrimStartAndEnd().IsEmpty()) { return true; }
        if (!In.IsNumeric())
        {
            OutError = FString::Printf(
                TEXT("'%s' is not a number. X and Y are ABSOLUTE screen pixels, the same space ")
                TEXT("`read-ui` reports. Give both or neither -- with neither, the click lands ")
                TEXT("at the agent cursor set by `input mouse move`."), *In);
            return false;
        }
        Out = FCString::Atof(*In);
        bPresent = true;
        return true;
    }

    FString UAPMouseRefuse(const FString& Error)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("ok"), false);
        O->SetStringField(TEXT("error"), Error);
        O->SetBoolField(TEXT("clicked"), false);
        return UAPJson(O);
    }
}

FString FAgentInput::SetMousePositionJson(float X, float Y)
{
    if (!FSlateApplication::IsInitialized())
    {
        return UAPMouseRefuse(TEXT("Slate is not initialised in this process, so there is no "
                                   "pointer layer to position. Run this against a live editor / "
                                   "PIE session (uap pie start), not a headless commandlet."));
    }
    FString UserError;
    const int32 User = ResolveSlateUserIndex(INDEX_NONE, UserError);
    if (User == INDEX_NONE) { return UAPMouseRefuse(UserError); }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    UAPReportPointerCapture(User, O);
    const FString Hit = UAPMoveAgentCursor(FVector2D(X, Y), User, O);
    O->SetNumberField(TEXT("user_index"), User);
    if (Hit.IsEmpty())
    {
        O->SetStringField(TEXT("warning"), FString::Printf(
            TEXT("no Slate widget at %.0f,%.0f -- a click there will hit nothing. Coordinates ")
            TEXT("are ABSOLUTE screen pixels (what `read-ui` reports), not viewport-local."),
            X, Y));
    }
    return UAPJson(O);
}

FString FAgentInput::ClickMouseJson(EAgentMouseButton Btn, const FString& XStr, const FString& YStr)
{
    if (!FSlateApplication::IsInitialized())
    {
        return UAPMouseRefuse(TEXT("Slate is not initialised in this process, so there is no "
                                   "pointer layer to click. Run this against a live editor / "
                                   "PIE session (uap pie start), not a headless commandlet."));
    }
    FKey Key = MouseButtonToKey(Btn);
    if (!Key.IsValid()) { return UAPMouseRefuse(TEXT("unknown mouse button")); }

    // Validate BEFORE anything is injected, like every other refusal here: a refused call must
    // have zero side effects, so it can never leave a button down.
    float PX = 0.f, PY = 0.f;
    bool bHasX = false, bHasY = false;
    FString ParseError;
    if (!UAPParseOptionalCoord(XStr, PX, bHasX, ParseError)) { return UAPMouseRefuse(ParseError); }
    if (!UAPParseOptionalCoord(YStr, PY, bHasY, ParseError)) { return UAPMouseRefuse(ParseError); }
    if (bHasX != bHasY)
    {
        return UAPMouseRefuse(TEXT("give BOTH X and Y or neither. One coordinate alone would "
                                   "pair a real value with a zero-initialised 0, i.e. click the "
                                   "top-left corner -- the exact silent miss this verb exists "
                                   "to end."));
    }
    FString UserError;
    const int32 User = ResolveSlateUserIndex(INDEX_NONE, UserError);
    if (User == INDEX_NONE) { return UAPMouseRefuse(UserError); }

    // A DISABLED TARGET IS A REFUSAL, and it is checked here -- before the cursor is moved and
    // before anything is injected -- so a refused call has zero side effects, like every other
    // refusal in this file. Clicking anyway is strictly worse than refusing: the events land on
    // the disabled widget's parent container, nothing happens, and the result says
    // clicked/down_handled/up_handled because ProcessMouseButtonDownEvent always returns true.
    // See UAPFindDisabledBlocker for the engine mechanism. [17tm466g0jf]
    const FVector2D Aim = (bHasX ? FVector2D(PX, PY) : GetAgentCursorPos());
    FString Blocker, UngatedLeaf;
    if (UAPFindDisabledBlocker(Aim, User, Blocker, UngatedLeaf))
    {
        TSharedRef<FJsonObject> R = MakeShared<FJsonObject>();
        R->SetBoolField(TEXT("ok"), false);
        R->SetBoolField(TEXT("clicked"), false);
        R->SetStringField(TEXT("disabled_widget"), Blocker);
        R->SetStringField(TEXT("would_hit_if_enabled"), UngatedLeaf);
        R->SetNumberField(TEXT("x"), Aim.X);
        R->SetNumberField(TEXT("y"), Aim.Y);
        R->SetNumberField(TEXT("user_index"), User);
        R->SetStringField(TEXT("error"), FString::Printf(
            TEXT("refusing to click %.0f,%.0f: a DISABLED widget ('%s') is in the way of '%s'. ")
            TEXT("Slate would not skip it and would not swallow the click -- it truncates the ")
            TEXT("hit path at the disabled widget and routes the press to its PARENT container, ")
            TEXT("which ignores it, while the call still reports clicked/down_handled/up_handled. ")
            TEXT("Nothing was injected. Read `enabled` on each `read-ui` entry and aim at an ")
            TEXT("enabled one; to drive it anyway for diagnosis, call InjectMouseMove then ")
            TEXT("InjectMouseButton directly."),
            Aim.X, Aim.Y, *Blocker, *UngatedLeaf));
        return UAPJson(R);
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("button"), Key.ToString());
    UAPReportPointerCapture(User, O);

    FString Hit;
    if (bHasX)
    {
        Hit = UAPMoveAgentCursor(FVector2D(PX, PY), User, O);
    }
    else
    {
        const FVector2D Where = GetAgentCursorPos();
        O->SetNumberField(TEXT("x"), Where.X);
        O->SetNumberField(TEXT("y"), Where.Y);
        Hit = DescribeWidgetsAt(Where, User);
        O->SetStringField(TEXT("hit"), Hit);
    }

    const FVector2D At = GetAgentCursorPos();
    // Down then up at the SAME point. SButton captures the pointer on the down and fires
    // OnClicked from the up, whose own test is MyGeometry.IsUnderLocation(event position) --
    // so the position on the event is what makes this a click and not a no-op.
    const bool bDown = InjectMouseButtonAt(Btn, /*bPressed*/ true, At, User);
    const bool bUp   = InjectMouseButtonAt(Btn, /*bPressed*/ false, At, User);
    O->SetBoolField(TEXT("down_handled"), bDown);
    O->SetBoolField(TEXT("up_handled"), bUp);
    O->SetBoolField(TEXT("clicked"), true);
    O->SetNumberField(TEXT("user_index"), User);

    // The two ways this reports success while having done nothing, named explicitly. Neither
    // is an error -- the events WERE delivered -- but a caller must not read them as a click.
    if (Hit.IsEmpty())
    {
        O->SetStringField(TEXT("warning"), FString::Printf(
            TEXT("no Slate widget at %.0f,%.0f, so the click hit nothing. Coordinates are "
                 "ABSOLUTE screen pixels (what `read-ui` reports). Verify with a second "
                 "`read-ui` that the UI actually changed."), At.X, At.Y));
    }
    else if (!bDown && !bUp)
    {
        O->SetStringField(TEXT("warning"), FString::Printf(
            TEXT("delivered to '%s' but NOTHING HANDLED either event, so this probably did not "
                 "activate anything. Verify with a second `read-ui` that the UI changed."),
            *Hit));
    }
    return UAPJson(O);
}

FString FAgentInput::ReleaseSlatePointerCaptureJson()
{
    if (!FSlateApplication::IsInitialized())
    {
        return UAPMouseRefuse(TEXT("Slate is not initialised in this process, so there is no "
                                   "pointer layer holding a capture."));
    }
    FString UserError;
    const int32 User = ResolveSlateUserIndex(INDEX_NONE, UserError);
    if (User == INDEX_NONE) { return UAPMouseRefuse(UserError); }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetNumberField(TEXT("user_index"), User);

    TSharedPtr<FSlateUser> SlateUser = FSlateApplication::Get().GetUser(User);
    if (!SlateUser.IsValid() || !SlateUser->HasAnyCapture())
    {
        O->SetBoolField(TEXT("released"), false);
        O->SetStringField(TEXT("holder"), FString());
        return UAPJson(O);
    }

    FString Holder = TEXT("<unresolved>");
    for (const FWidgetPath& P : SlateUser->GetCaptorPaths())
    {
        if (P.IsValid() && P.Widgets.Num() > 0)
        {
            Holder = UAPDescribeSlateWidget(P.Widgets.Last().Widget);
            break;
        }
    }
    SlateUser->ReleaseAllCapture();
    O->SetBoolField(TEXT("released"), true);
    O->SetStringField(TEXT("holder"), Holder);
    O->SetBoolField(TEXT("still_captured"), SlateUser->HasAnyCapture());
    return UAPJson(O);
}
