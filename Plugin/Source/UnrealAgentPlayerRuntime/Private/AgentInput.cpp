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
#include "Engine/Engine.h"
#include "InputKeyEventArgs.h"
#include "GenericPlatform/GenericPlatformInputDeviceMapper.h"
#include "HAL/PlatformTime.h"
#include "Containers/Ticker.h"
#include "Misc/App.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Serialization/JsonReader.h"

TSharedPtr<SWidget> FAgentInput::FindPIEViewportWidget()
{
    UGameViewportClient* GV = FAgentWorld::GetActiveGameViewport();
    return GV ? GV->GetGameViewportWidget() : nullptr;
}

namespace
{
    /**
     * Flat PIE with several clients in ONE process (PlayNumberOfClients > 1, the SchoolsOut default)
     * opens one OS window per client, all at the SAME screen rect. Slate hit-tests the topmost
     * window, so a click at a read-ui coordinate landed on a SIBLING client's viewport - which has
     * no menu open - and ended on its SPIEViewport / a border, while read-ui had read the ACTIVE
     * world's UI. Tabs never switched and the call still said clicked [17tm466z5j8].
     *
     * When the topmost window under Pos is a sibling PIE game window and the active game window
     * also covers Pos, return a path located inside the ACTIVE game window only. Returns false
     * (OutPath untouched) in every other case, so ordinary editor / single-client clicks are
     * unchanged.
     */
    bool UAPRedirectPathToActiveGameWindow(FVector2D Pos, int32 User, bool bIgnoreEnabledStatus, FWidgetPath& OutPath)
    {
        if (!FSlateApplication::IsInitialized() || !GEngine) { return false; }
        FSlateApplication& App = FSlateApplication::Get();
        UGameViewportClient* ActiveGV = FAgentWorld::GetActiveGameViewport();
        TSharedPtr<SWindow> Active = ActiveGV ? ActiveGV->GetWindow() : nullptr;
        if (!Active.IsValid() || !Active->GetRectInScreen().ContainsPoint(Pos)) { return false; }

        const FWidgetPath Top = App.LocateWindowUnderMouse(
            Pos, App.GetInteractiveTopLevelWindows(), bIgnoreEnabledStatus, User);
        if (!Top.IsValid() || !Top.TopLevelWindow.IsValid() || Top.TopLevelWindow == Active) { return false; }

        bool bTopIsSiblingPIE = false;
        for (const FWorldContext& Ctx : GEngine->GetWorldContexts())
        {
            UGameViewportClient* GV = (Ctx.WorldType == EWorldType::PIE && Ctx.World()) ? Ctx.World()->GetGameViewport() : nullptr;
            if (GV && GV->GetWindow() == Top.TopLevelWindow) { bTopIsSiblingPIE = true; break; }
        }
        if (!bTopIsSiblingPIE) { return false; }

        TArray<TSharedRef<SWindow>> OnlyActive;
        OnlyActive.Add(Active.ToSharedRef());
        OutPath = App.LocateWindowUnderMouse(Pos, OnlyActive, bIgnoreEnabledStatus, User);
        return OutPath.IsValid();
    }

    /** Windows to hit-test for Pos: only the active game window when a sibling PIE client covers it, else all. */
    TArray<TSharedRef<SWindow>> UAPWindowsForPoint(FVector2D Pos, int32 User)
    {
        FWidgetPath Redirected;
        if (UAPRedirectPathToActiveGameWindow(Pos, User, /*bIgnoreEnabledStatus*/ true, Redirected))
        {
            TArray<TSharedRef<SWindow>> One;
            One.Add(Redirected.TopLevelWindow.ToSharedRef());
            return One;
        }
        return FSlateApplication::Get().GetInteractiveTopLevelWindows();
    }
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
    // NOT App.SetCursorPos(NewPos): FSlateApplication::SetCursorPos writes the PLATFORM cursor
    // (Win32 SetCursorPos), i.e. it would drag the HUMAN's real mouse pointer across the screen
    // while they work. The event position below is what Slate routes by, which is all a
    // synthetic move needs. Owner rule 2026-11-11: uap never touches the OS cursor.
    FWidgetPath Redirected;
    if (UAPRedirectPathToActiveGameWindow(NewPos, User, /*bIgnoreEnabledStatus*/ false, Redirected))
    {
        return App.RoutePointerMoveEvent(Redirected, Evt, /*bIsSynthetic*/ false);
    }
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
    // A sibling PIE client covers this point (see UAPRedirectPathToActiveGameWindow): route the
    // event straight to the ACTIVE game window's widget path - ProcessMouseButton*Event would
    // re-locate the topmost window and hand the click to the sibling.
    FWidgetPath Redirected;
    if (UAPRedirectPathToActiveGameWindow(ScreenPos, User, /*bIgnoreEnabledStatus*/ false, Redirected))
    {
        const FReply Reply = bPressed ? App.RoutePointerDownEvent(Redirected, Evt)
                                      : App.RoutePointerUpEvent(Redirected, Evt);
        return Reply.IsEventHandled();
    }
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
        ScreenPos, UAPWindowsForPoint(ScreenPos, UserIndex), /*bIgnoreEnabledStatus*/ false, UserIndex);
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
        // The client (game world) this hold belongs to. A hold is re-asserted EVERY FRAME from a
        // core ticker, long after the call that started it returned, so the world it targets has
        // to be remembered here and made current again on each tick. Weak: a PIE world torn
        // down under a live hold drops the hold rather than dangling.
        TWeakObjectPtr<UWorld> World;
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
        // Go back out through the SAME client the hold went in on.
        FAgentWorld::FScope Scope(H.World.Get());
        if (H.bAnalog) { UAPHoldInject(H, 0.f); }
        else if (H.bStarted) { FAgentInput::InjectKey(H.Key, /*bPressed*/ false, /*bRepeat*/ false); }
    }

    bool UAPHoldTick(float /*DeltaTime*/)
    {
        const double Now = FPlatformTime::Seconds();
        for (int32 i = GHeldInputs.Num() - 1; i >= 0; --i)
        {
            FUAPHeldInput& H = GHeldInputs[i];
            // The client this hold was started on went away (PIE ended / that client closed),
            // or its viewport no longer takes input: nothing can be held there. Forget it
            // rather than spinning forever re-injecting into nothing.
            UWorld* HeldWorld = H.World.Get();
            if (!HeldWorld)
            {
                GHeldInputs.RemoveAt(i);
                continue;
            }
            FAgentWorld::FScope Scope(HeldWorld);
            if (!FAgentInput::HasLiveViewport())
            {
                GHeldInputs.RemoveAt(i);
                continue;
            }
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

    // One hold per (key, client). The same key held on two clients at once is two holds.
    FUAPHeldInput& UAPFindOrAddHold(FKey Key)
    {
        UWorld* World = FAgentWorld::GetActiveGameWorld();
        for (FUAPHeldInput& H : GHeldInputs)
        {
            if (H.Key == Key && H.World.Get() == World) { return H; }
        }
        FUAPHeldInput New;
        New.Key = Key;
        New.World = World;
        return GHeldInputs[GHeldInputs.Add(New)];
    }

    bool UAPIsHeld(FKey Key)
    {
        UWorld* World = FAgentWorld::GetActiveGameWorld();
        for (const FUAPHeldInput& H : GHeldInputs)
        {
            if (H.Key == Key && H.World.Get() == World) { return true; }
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

// --- Timed input sequences [17tm466zqax] -------------------------------------------------
// `hold` / `axis` start counting when the call ARRIVES, and the CLI-to-engine hop adds 0.2-0.6 s
// of jitter to that, so nothing under ~1.5 s could be timed from outside (vanishing platforms,
// obstacle gaps, a jump 0.85 s into a run). A sequence ships the WHOLE script in one call and the
// engine plays it against the target client's WORLD time from a core ticker, so the round trip
// stops mattering: every offset is relative to the call's arrival (or to an explicit start time).
// Steps are played with the same hold machinery as `input hold` (registry entry, IE_Repeat every
// frame, `input status`, `input release`); only their START and END come from the script clock.

namespace
{
    struct FUAPSeqStep
    {
        FKey   Key;
        bool   bAnalog = false;
        float  Value = 0.f;
        double Start = 0.0;          // scripted offset from T0, seconds
        double End = 0.0;
        bool   bStarted = false;
        bool   bEnded = false;
        FString Failure;
        double StartedRel = 0.0;     // world seconds after T0 when it actually fired
        double EndedRel = 0.0;
        double StartedReal = 0.0;    // FPlatformTime::Seconds() when it fired
        double EndedReal = 0.0;
        uint64 StartedFrame = 0;
        uint64 EndedFrame = 0;
        float  StartedDt = 0.f;      // world delta seconds of that frame (the error's yardstick)
        float  EndedDt = 0.f;
    };

    struct FUAPSeqEvent
    {
        double Time = 0.0;
        int32  Step = 0;
        bool   bStart = false;
    };

    struct FUAPSequence
    {
        int32   Id = 0;
        FString State;               // running | done | cancelled | aborted
        FString Note;
        TWeakObjectPtr<UWorld> World;
        int32   Client = INDEX_NONE;
        double  T0World = 0.0;       // world time the script's 0 is pinned to
        double  ArrivalWorld = 0.0;  // world time the call arrived at
        double  ArrivalReal = 0.0;
        uint64  ArrivalFrame = 0;
        double  TotalSeconds = 0.0;
        TArray<FUAPSeqStep> Steps;
        TArray<FUAPSeqEvent> Events;
        int32   Cursor = 0;
    };

    constexpr double UAPSeqGraceSeconds = 0.25;   // the registry hold outlives its script end by this
    constexpr int32  UAPSeqMaxSteps = 64;
    constexpr double UAPSeqMaxSeconds = 300.0;
    constexpr int32  UAPSeqHistory = 8;

    TArray<FUAPSequence> GSeqActive;
    TArray<FUAPSequence> GSeqHistory;
    int32 GSeqNextId = 1;
    FTSTicker::FDelegateHandle GSeqTicker;
    bool GSeqReleasing = false;   // the script itself is releasing a step: that must not cancel the script

    /** Keep the registry's own expiry just ahead of the script end, so `input status` reads true
        and a paused world (world time stops, real time does not) never lets the hold lapse. */
    void UAPSeqRefreshHold(const FUAPSeqStep& Step, UWorld* World, double RelNow)
    {
        for (FUAPHeldInput& H : GHeldInputs)
        {
            if (H.Key == Step.Key && H.World.Get() == World)
            {
                H.EndRealTime = FPlatformTime::Seconds() + FMath::Max(0.0, Step.End - RelNow) + UAPSeqGraceSeconds;
            }
        }
    }

    void UAPSeqStartStep(FUAPSequence& Seq, FUAPSeqStep& Step, UWorld* World, double Rel)
    {
        Step.bStarted = true;
        Step.StartedRel = Rel;
        Step.StartedReal = FPlatformTime::Seconds();
        Step.StartedFrame = GFrameCounter;
        Step.StartedDt = World->GetDeltaSeconds();
        const float HoldSeconds = (float)(FMath::Max(0.0, Step.End - Rel) + UAPSeqGraceSeconds);
        bool bOk = false;
        if (!FAgentInput::HasLiveViewport())
        {
            Step.Failure = TEXT("no live game viewport is accepting input when this step came due");
        }
        else
        {
            bOk = Step.bAnalog ? FAgentInput::HoldAxis(Step.Key, Step.Value, HoldSeconds, INDEX_NONE)
                               : FAgentInput::HoldKey(Step.Key, HoldSeconds);
            if (!bOk) { Step.Failure = TEXT("the game viewport rejected the input"); }
        }
        if (!bOk)
        {
            Step.bEnded = true;   // nothing to release, nothing to wait for
            Step.EndedRel = Rel;
            Seq.Note = TEXT("a step failed; see its failure field");
        }
    }

    void UAPSeqEndStep(FUAPSeqStep& Step, double Rel, UWorld* World)
    {
        Step.bEnded = true;
        Step.EndedRel = Rel;
        Step.EndedReal = FPlatformTime::Seconds();
        Step.EndedFrame = GFrameCounter;
        Step.EndedDt = World->GetDeltaSeconds();
        GSeqReleasing = true;
        FAgentInput::ReleaseHeld(Step.Key);   // inside the client scope: only this client's hold
        GSeqReleasing = false;
    }

    /** Fire everything that is due. True when the script has played out. */
    bool UAPSeqPump(FUAPSequence& Seq)
    {
        UWorld* World = Seq.World.Get();
        if (!World)
        {
            Seq.State = TEXT("aborted");
            Seq.Note = TEXT("the client's world went away mid-script");
            return true;
        }
        FAgentWorld::FScope Scope(World);
        const double Rel = World->GetTimeSeconds() - Seq.T0World;
        while (Seq.Cursor < Seq.Events.Num() && Seq.Events[Seq.Cursor].Time <= Rel)
        {
            const FUAPSeqEvent& E = Seq.Events[Seq.Cursor++];
            FUAPSeqStep& Step = Seq.Steps[E.Step];
            if (Step.bEnded && !Step.bStarted) { continue; }   // cancelled before it came due
            if (E.bStart) { UAPSeqStartStep(Seq, Step, World, Rel); }
            else if (Step.bStarted && !Step.bEnded) { UAPSeqEndStep(Step, Rel, World); }
        }
        for (const FUAPSeqStep& Step : Seq.Steps)
        {
            if (Step.bStarted && !Step.bEnded) { UAPSeqRefreshHold(Step, World, Rel); }
        }
        if (Seq.Cursor >= Seq.Events.Num())
        {
            Seq.State = TEXT("done");
            return true;
        }
        return false;
    }

    void UAPSeqRetire(int32 ActiveIndex)
    {
        GSeqHistory.Add(MoveTemp(GSeqActive[ActiveIndex]));
        GSeqActive.RemoveAt(ActiveIndex);
        while (GSeqHistory.Num() > UAPSeqHistory) { GSeqHistory.RemoveAt(0); }
    }

    bool UAPSeqTick(float /*DeltaTime*/)
    {
        for (int32 i = GSeqActive.Num() - 1; i >= 0; --i)
        {
            if (UAPSeqPump(GSeqActive[i])) { UAPSeqRetire(i); }
        }
        if (GSeqActive.Num() == 0)
        {
            GSeqTicker.Reset();
            return false;   // returning false unregisters this ticker
        }
        return true;
    }

    /** Stop scripted steps: those on Key (invalid Key = all of them), on OnlyWorld (nullptr = every
        world). A step already down is released by the caller (ReleaseHeld), not here. */
    int32 UAPSeqCancel(FKey Key, UWorld* OnlyWorld)
    {
        int32 Cancelled = 0;
        for (int32 i = GSeqActive.Num() - 1; i >= 0; --i)
        {
            FUAPSequence& Seq = GSeqActive[i];
            if (OnlyWorld && Seq.World.Get() != OnlyWorld) { continue; }
            bool bTouched = false;
            bool bAnyLeft = false;
            for (FUAPSeqStep& Step : Seq.Steps)
            {
                if (!Step.bEnded && (!Key.IsValid() || Step.Key == Key))
                {
                    Step.bEnded = true;
                    if (!Step.bStarted) { Step.Failure = TEXT("cancelled before it came due"); }
                    bTouched = true;
                }
                if (!Step.bEnded) { bAnyLeft = true; }
            }
            if (!bTouched) { continue; }
            ++Cancelled;
            if (!bAnyLeft)
            {
                Seq.State = TEXT("cancelled");
                Seq.Note = TEXT("cancelled by input release");
                UAPSeqRetire(i);
            }
        }
        return Cancelled;
    }

    bool UAPSeqNumber(const TSharedPtr<FJsonObject>& Obj, const TCHAR* Field, double& Out, FString& OutError)
    {
        if (!Obj->TryGetNumberField(Field, Out))
        {
            OutError = FString::Printf(TEXT("has no numeric '%s'"), Field);
            return false;
        }
        return true;
    }
}

FString FAgentInput::RunSequenceJson(const FString& ScriptJson, const FString& StartAt)
{
    if (!HasLiveViewport())
    {
        return UAPRefuse(TEXT("sequence"), TEXT("no live game viewport is accepting input -- start PIE first "
                                                "(uap pie start), and check the viewport is not ignoring input"));
    }
    TSharedPtr<FJsonObject> Root;
    {
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ScriptJson);
        if (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid())
        {
            return UAPRefuse(TEXT("sequence"), TEXT("the script is not a JSON object: {\"steps\":[{\"kind\":"
                                                    "\"hold\",\"key\":\"W\",\"start\":0,\"end\":2}, ...]}"));
        }
    }
    const TArray<TSharedPtr<FJsonValue>>* Raw = nullptr;
    if (!Root->TryGetArrayField(TEXT("steps"), Raw) || !Raw || Raw->Num() == 0)
    {
        return UAPRefuse(TEXT("sequence"), TEXT("the script has no steps"));
    }
    if (Raw->Num() > UAPSeqMaxSteps)
    {
        return UAPRefuse(TEXT("sequence"), FString::Printf(TEXT("%d steps; the limit is %d"), Raw->Num(), UAPSeqMaxSteps));
    }

    // Validate EVERYTHING before pressing anything: a refused script has zero side effects.
    FUAPSequence Seq;
    for (int32 i = 0; i < Raw->Num(); ++i)
    {
        const TSharedPtr<FJsonObject>* StepObj = nullptr;
        if (!(*Raw)[i]->TryGetObject(StepObj) || !StepObj)
        {
            return UAPRefuse(TEXT("sequence"), FString::Printf(TEXT("step %d is not an object"), i));
        }
        FUAPSeqStep Step;
        FString KeyName, Kind;
        (*StepObj)->TryGetStringField(TEXT("key"), KeyName);
        (*StepObj)->TryGetStringField(TEXT("kind"), Kind);
        Step.Key = FKey(*KeyName);
        if (KeyName.IsEmpty() || !Step.Key.IsValid())
        {
            return UAPRefuse(KeyName, FString::Printf(
                TEXT("step %d: no key named '%s' in this engine's FKey registry. Use the exact FKey "
                     "name (W, SpaceBar, LeftControl, Gamepad_LeftY, OculusTouch_Left_Thumbstick_Y)."),
                i, *KeyName));
        }
        Step.bAnalog = Kind.Equals(TEXT("axis"), ESearchCase::IgnoreCase);
        if (!Step.bAnalog && !Kind.Equals(TEXT("hold"), ESearchCase::IgnoreCase))
        {
            return UAPRefuse(KeyName, FString::Printf(TEXT("step %d: kind must be 'hold' or 'axis' (got '%s')"), i, *Kind));
        }
        if (Step.bAnalog && !Step.Key.IsAnalog())
        {
            return UAPRefuse(KeyName, FString::Printf(TEXT("step %d: '%s' is a digital key, not an analog axis"), i, *KeyName));
        }
        double Value = 1.0;
        (*StepObj)->TryGetNumberField(TEXT("value"), Value);
        Step.Value = (float)Value;
        FString NumError;
        if (!UAPSeqNumber(*StepObj, TEXT("start"), Step.Start, NumError) ||
            !UAPSeqNumber(*StepObj, TEXT("end"), Step.End, NumError))
        {
            return UAPRefuse(KeyName, FString::Printf(TEXT("step %d %s"), i, *NumError));
        }
        if (Step.Start < 0.0 || Step.End <= Step.Start)
        {
            return UAPRefuse(KeyName, FString::Printf(TEXT("step %d: needs 0 <= start < end (got %f..%f)"), i, Step.Start, Step.End));
        }
        if (Step.End > UAPSeqMaxSeconds)
        {
            return UAPRefuse(KeyName, FString::Printf(TEXT("step %d ends at %f s; the limit is %f"), i, Step.End, UAPSeqMaxSeconds));
        }
        Seq.TotalSeconds = FMath::Max(Seq.TotalSeconds, Step.End);
        Seq.Steps.Add(Step);
    }
    // The registry keeps ONE hold per key, so two steps on one key must not overlap in time:
    // the first one's release would cut the second. Touching (end == start) is fine.
    for (int32 a = 0; a < Seq.Steps.Num(); ++a)
    {
        for (int32 b = a + 1; b < Seq.Steps.Num(); ++b)
        {
            const FUAPSeqStep& A = Seq.Steps[a];
            const FUAPSeqStep& B = Seq.Steps[b];
            if (A.Key == B.Key && A.Start < B.End && B.Start < A.End)
            {
                return UAPRefuse(A.Key.ToString(), FString::Printf(
                    TEXT("steps %d and %d both drive %s over %f..%f s and %f..%f s; one key can be held "
                         "once at a time"), a, b, *A.Key.ToString(), A.Start, A.End, B.Start, B.End));
            }
        }
    }
    // A hold the caller already has running on one of these keys would be cut by the script.
    {
        TArray<FAgentHeldInputInfo> Held;
        GetHeld(Held);
        for (const FUAPSeqStep& Step : Seq.Steps)
        {
            for (const FAgentHeldInputInfo& H : Held)
            {
                if (H.Key == Step.Key.ToString() && H.RemainingSeconds > 0.f)
                {
                    return UAPRefuse(H.Key, FString::Printf(
                        TEXT("%s is already held (%.2f s left); end it with `input release %s` or wait"),
                        *H.Key, H.RemainingSeconds, *H.Key));
                }
            }
        }
    }

    UWorld* World = FAgentWorld::GetActiveGameWorld();
    if (!World) { return UAPRefuse(TEXT("sequence"), TEXT("no game world")); }

    Seq.World = World;
    Seq.Client = FAgentWorld::GetClientIndex(World);
    Seq.ArrivalWorld = World->GetTimeSeconds();
    Seq.ArrivalReal = FPlatformTime::Seconds();
    Seq.ArrivalFrame = GFrameCounter;
    Seq.T0World = Seq.ArrivalWorld;
    // StartAt: "" = the moment this call lands; "+x" = x seconds after it lands (world time,
    // pinned in-engine); "x" = absolute WORLD seconds of that client (read from a prior answer).
    const FString At = StartAt.TrimStartAndEnd();
    if (!At.IsEmpty())
    {
        const bool bRelative = At.StartsWith(TEXT("+"));
        if (!(bRelative ? At.Mid(1) : At).IsNumeric())
        {
            return UAPRefuse(TEXT("sequence"), FString::Printf(TEXT("bad start '%s': use \"\" (now), \"+1.5\" "
                             "(1.5 s after arrival) or an absolute world time like \"412.7\""), *At));
        }
        const double V = FCString::Atod(*At);
        Seq.T0World = bRelative ? Seq.ArrivalWorld + V : V;
    }

    for (int32 i = 0; i < Seq.Steps.Num(); ++i)
    {
        FUAPSeqEvent Begin;
        Begin.Time = Seq.Steps[i].Start;
        Begin.Step = i;
        Begin.bStart = true;
        FUAPSeqEvent Finish;
        Finish.Time = Seq.Steps[i].End;
        Finish.Step = i;
        Finish.bStart = false;
        Seq.Events.Add(Begin);
        Seq.Events.Add(Finish);
    }
    // Time order; at a tie an END comes before a START so a touching pair on one key hands over.
    Seq.Events.StableSort([](const FUAPSeqEvent& L, const FUAPSeqEvent& R)
    {
        if (L.Time != R.Time) { return L.Time < R.Time; }
        return !L.bStart && R.bStart;
    });
    Seq.Id = GSeqNextId++;
    Seq.State = TEXT("running");

    // Pump once NOW: a step at 0 goes out inside this call, on the frame it arrived, instead of
    // waiting for the next tick. The ticker takes over for everything later.
    const bool bFinishedAtOnce = UAPSeqPump(Seq);
    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetNumberField(TEXT("sequence"), Seq.Id);
    O->SetNumberField(TEXT("steps"), Seq.Steps.Num());
    O->SetNumberField(TEXT("total_seconds"), Seq.TotalSeconds);
    O->SetNumberField(TEXT("client"), Seq.Client);
    O->SetNumberField(TEXT("arrival_world_time"), Seq.ArrivalWorld);
    O->SetNumberField(TEXT("t0_world_time"), Seq.T0World);
    O->SetNumberField(TEXT("starts_in_seconds"), FMath::Max(0.0, Seq.T0World - Seq.ArrivalWorld));
    O->SetNumberField(TEXT("frame"), (double)Seq.ArrivalFrame);
    O->SetStringField(TEXT("clock"), TEXT("world"));
    O->SetStringField(TEXT("route"), TEXT("viewport"));
    O->SetStringField(TEXT("note"), TEXT("scripted in-engine against world time from here on; the CLI round "
                                          "trip no longer matters. `input sequence-status` reads when each "
                                          "step actually fired."));

    if (bFinishedAtOnce)
    {
        GSeqHistory.Add(MoveTemp(Seq));
        while (GSeqHistory.Num() > UAPSeqHistory) { GSeqHistory.RemoveAt(0); }
    }
    else
    {
        GSeqActive.Add(MoveTemp(Seq));
        if (!GSeqTicker.IsValid())
        {
            GSeqTicker = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateStatic(&UAPSeqTick), 0.f);
        }
    }
    return UAPJson(O);
}

FString FAgentInput::GetSequenceLogJson()
{
    UWorld* Only = FAgentWorld::GetOverrideWorld();
    auto Describe = [](const FUAPSequence& S) -> TSharedPtr<FJsonValue>
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetNumberField(TEXT("sequence"), S.Id);
        O->SetStringField(TEXT("state"), S.State);
        if (!S.Note.IsEmpty()) { O->SetStringField(TEXT("note"), S.Note); }
        O->SetNumberField(TEXT("client"), S.Client);
        O->SetNumberField(TEXT("arrival_world_time"), S.ArrivalWorld);
        O->SetNumberField(TEXT("t0_world_time"), S.T0World);
        O->SetNumberField(TEXT("arrival_frame"), (double)S.ArrivalFrame);
        TArray<TSharedPtr<FJsonValue>> Steps;
        double MaxErr = 0.0;
        for (int32 i = 0; i < S.Steps.Num(); ++i)
        {
            const FUAPSeqStep& St = S.Steps[i];
            TSharedRef<FJsonObject> J = MakeShared<FJsonObject>();
            J->SetNumberField(TEXT("step"), i);
            J->SetStringField(TEXT("key"), St.Key.ToString());
            J->SetStringField(TEXT("kind"), St.bAnalog ? TEXT("axis") : TEXT("hold"));
            if (St.bAnalog) { J->SetNumberField(TEXT("value"), St.Value); }
            J->SetNumberField(TEXT("start"), St.Start);
            J->SetNumberField(TEXT("end"), St.End);
            if (!St.Failure.IsEmpty()) { J->SetStringField(TEXT("failure"), St.Failure); }
            if (St.bStarted)
            {
                // error = how far past the scripted time the engine actually fired it. It cannot be
                // negative (a step never fires early) and is bounded by one frame (the next tick).
                const double StartErr = (St.StartedRel - St.Start) * 1000.0;
                J->SetNumberField(TEXT("started_at"), St.StartedRel);
                J->SetNumberField(TEXT("start_error_ms"), StartErr);
                J->SetNumberField(TEXT("start_frame"), (double)St.StartedFrame);
                J->SetNumberField(TEXT("start_frame_ms"), St.StartedDt * 1000.0);
                J->SetNumberField(TEXT("start_real"), St.StartedReal);
                MaxErr = FMath::Max(MaxErr, StartErr);
            }
            if (St.bStarted && St.EndedFrame > 0)
            {
                const double EndErr = (St.EndedRel - St.End) * 1000.0;
                J->SetNumberField(TEXT("ended_at"), St.EndedRel);
                J->SetNumberField(TEXT("end_error_ms"), EndErr);
                J->SetNumberField(TEXT("end_frame"), (double)St.EndedFrame);
                J->SetNumberField(TEXT("end_real"), St.EndedReal);
                MaxErr = FMath::Max(MaxErr, EndErr);
            }
            Steps.Add(MakeShared<FJsonValueObject>(J));
        }
        O->SetNumberField(TEXT("max_error_ms"), MaxErr);
        O->SetArrayField(TEXT("steps"), Steps);
        return MakeShared<FJsonValueObject>(O);
    };

    TArray<TSharedPtr<FJsonValue>> Out;
    for (const FUAPSequence& S : GSeqHistory)
    {
        if (!Only || S.World.Get() == Only) { Out.Add(Describe(S)); }
    }
    for (const FUAPSequence& S : GSeqActive)
    {
        if (!Only || S.World.Get() == Only) { Out.Add(Describe(S)); }
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetBoolField(TEXT("ok"), true);
    Root->SetStringField(TEXT("clock"), TEXT("world"));
    Root->SetArrayField(TEXT("sequences"), Out);
    return UAPJson(Root);
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
    // Inside a client scope, release only that client's holds. With no scope, release them all:
    // the recovery path must be able to clear every client without naming each one.
    UWorld* Only = FAgentWorld::GetOverrideWorld();
    // A scripted step that has not come due yet would press the key again after this release.
    if (!GSeqReleasing) { UAPSeqCancel(Key, Only); }
    int32 Count = 0;
    for (int32 i = GHeldInputs.Num() - 1; i >= 0; --i)
    {
        if (Key.IsValid() && GHeldInputs[i].Key != Key) { continue; }
        if (Only && GHeldInputs[i].World.Get() != Only) { continue; }
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
    // One client when scoped to one, every game world otherwise.
    TArray<UWorld*> Worlds;
    if (UWorld* Only = FAgentWorld::GetOverrideWorld()) { Worlds.Add(Only); }
    else { FAgentWorld::GetAllGameWorlds(Worlds); }
    int32 Count = 0;
    for (UWorld* World : Worlds)
    {
        for (FConstPlayerControllerIterator It = World->GetPlayerControllerIterator(); It; ++It)
        {
            if (APlayerController* PC = It->Get())
            {
                PC->FlushPressedKeys();
                ++Count;
            }
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
    UWorld* Only = FAgentWorld::GetOverrideWorld();
    for (const FUAPHeldInput& H : GHeldInputs)
    {
        if (Only && H.World.Get() != Only) { continue; }
        FAgentHeldInputInfo Info;
        Info.Client = FAgentWorld::GetClientIndex(H.World.Get());
        {
            // Engine ground truth is read on the hold's OWN client, not on the default one.
            FAgentWorld::FScope Scope(H.World.Get());
            Info.bDown = FAgentInput::IsKeyDown(H.Key);
        }
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
        O->SetNumberField(TEXT("client"), H.Client);
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
        O->SetBoolField(TEXT("down"), H.bDown);
        Arr.Add(MakeShared<FJsonValueObject>(O));
    }
    Root->SetBoolField(TEXT("ok"), true);
    Root->SetArrayField(TEXT("held"), Arr);
    return UAPJson(Root);
}

void FAgentInput::ShutdownHolds()
{
    GSeqActive.Reset();
    GSeqHistory.Reset();
    if (GSeqTicker.IsValid())
    {
        FTSTicker::RemoveTicker(GSeqTicker);
        GSeqTicker.Reset();
    }
    GHeldInputs.Reset();
    if (GHoldTicker.IsValid())
    {
        FTSTicker::RemoveTicker(GHoldTicker);
        GHoldTicker.Reset();
    }
}

// --- Client targeting -------------------------------------------------------------------
// See AgentWorld.h. These run a verb once per targeted client with that client's world current.

bool FAgentInput::ForEachClient(const FString& Client, FString& OutError,
                                TFunctionRef<bool(int32 ClientIndex)> Fn)
{
    TArray<UWorld*> Worlds;
    if (!FAgentWorld::ResolveClients(Client, Worlds, OutError)) { return false; }
    if (Worlds.Num() == 0)
    {
        // No selector: the historical default, no override.
        return Fn(FAgentWorld::GetClientIndex(FAgentWorld::GetActiveGameWorld()));
    }
    bool bAll = true;
    for (UWorld* W : Worlds)
    {
        FAgentWorld::FScope Scope(W);
        if (!Fn(FAgentWorld::GetClientIndex(W)))
        {
            bAll = false;
            if (OutError.IsEmpty())
            {
                OutError = FString::Printf(TEXT("client %d did not take the input (no live viewport, or "
                                                "its viewport is ignoring input)"),
                                           FAgentWorld::GetClientIndex(W));
            }
        }
    }
    return bAll;
}

bool FAgentInput::OnClients(const TCHAR* Verb, const FString& Client, TFunctionRef<bool()> Fn)
{
    FString Error;
    const bool bOk = ForEachClient(Client, Error, [&Fn](int32) { return Fn(); });
    if (!bOk && !Error.IsEmpty())
    {
        // A bool cannot carry a reason, so it goes to the log where `uap log since` finds it.
        UE_LOG(LogUAPRuntime, Error, TEXT("%s: %s"), Verb, *Error);
    }
    return bOk;
}

FString FAgentInput::ForEachClientJson(const FString& Client, TFunctionRef<FString()> Fn)
{
    auto Refuse = [](const FString& Error)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("ok"), false);
        O->SetBoolField(TEXT("pressed"), false);
        O->SetStringField(TEXT("error"), Error);
        return UAPJson(O);
    };

    TArray<UWorld*> Worlds;
    FString Error;
    if (!FAgentWorld::ResolveClients(Client, Worlds, Error)) { return Refuse(Error); }
    if (Worlds.Num() == 0) { return Fn(); }   // no selector: unchanged behaviour, unchanged JSON

    auto Decode = [](const FString& Json) -> TSharedPtr<FJsonObject>
    {
        TSharedPtr<FJsonObject> Obj;
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Json);
        if (!FJsonSerializer::Deserialize(Reader, Obj) || !Obj.IsValid()) { return nullptr; }
        return Obj;
    };

    TArray<TSharedPtr<FJsonValue>> Results;
    bool bAllOk = true;
    for (UWorld* W : Worlds)
    {
        FAgentWorld::FScope Scope(W);
        const FString One = Fn();
        TSharedPtr<FJsonObject> Obj = Decode(One);
        if (!Obj.IsValid())
        {
            Obj = MakeShared<FJsonObject>();
            Obj->SetBoolField(TEXT("ok"), false);
            Obj->SetStringField(TEXT("error"), TEXT("verb returned something other than JSON"));
        }
        bool bOk = true;
        Obj->TryGetBoolField(TEXT("ok"), bOk);
        bAllOk = bAllOk && bOk;
        Obj->SetNumberField(TEXT("client"), FAgentWorld::GetClientIndex(W));
        Results.Add(MakeShared<FJsonValueObject>(Obj.ToSharedRef()));
    }

    if (Results.Num() == 1)
    {
        // One client: return that client's own envelope (with "client" added), so a caller
        // reading ok / error / pressed / held does not need to know a fan-out exists.
        return UAPJson(Results[0]->AsObject().ToSharedRef());
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetBoolField(TEXT("ok"), bAllOk);
    Root->SetArrayField(TEXT("clients"), Results);
    return UAPJson(Root);
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
     * READ the GAME-side mouse cache (what GetMousePosition / GetHitResultUnderCursor return)
     * and report whether it agrees with the target. It is NOT written from here any more:
     * APlayerController::SetMouseLocation -> FSceneViewport::SetMouse ends in
     * FSlateApplication::SetCursorPos, which moves the human's REAL mouse pointer. The cache
     * is updated the legitimate way instead -- the pointer-move event we route to the viewport
     * widget runs FSceneViewport::UpdateCachedCursorPos. Reported separately from the Slate side
     * because the two can disagree and a caller chasing a gameplay trace needs to know which one
     * moved. Call this AFTER the move event has been delivered.
     */
    void UAPReadGameMouse(const TSharedRef<FJsonObject>& O, FVector2D Absolute)
    {
        UWorld* World = FAgentWorld::GetActiveGameWorld();
        APlayerController* PC = World ? World->GetFirstPlayerController() : nullptr;
        FVector2D Local;
        if (!PC || !UAPAbsoluteToViewportLocal(Absolute, Local))
        {
            O->SetBoolField(TEXT("game_mouse_set"), false);
            return;
        }
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
            ScreenPos, UAPWindowsForPoint(ScreenPos, User), /*bIgnoreEnabledStatus*/ true, User);
        if (!Ungated.IsValid() || Ungated.Widgets.Num() == 0) { return false; }

        FWidgetPath Gated = App.LocateWindowUnderMouse(
            ScreenPos, UAPWindowsForPoint(ScreenPos, User), /*bIgnoreEnabledStatus*/ false, User);
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

        // 2. The platform (OS) cursor is deliberately NOT moved: that is the human's real mouse
        //    pointer, and uap never touches it (owner rule 2026-11-11). It is only READ back, so
        //    a caller can see it stayed where the human left it.
        const FVector2D OsAfter = App.GetCursorPos();
        const bool bOsMoved = FVector2D::Distance(OsAfter, Target) <= 2.0;   // coincidence at most
        O->SetBoolField(TEXT("os_cursor_moved"), bOsMoved);
        O->SetBoolField(TEXT("os_cursor_untouched"), true);
        O->SetNumberField(TEXT("os_cursor_x"), OsAfter.X);
        O->SetNumberField(TEXT("os_cursor_y"), OsAfter.Y);

        // 3. A real move event at the target, so hover / OnMouseEnter fire like a user's.
        const TSet<FKey> NoButtons;   // named: FPointerEvent keeps a pointer to it
        FPointerEvent Move((uint32)User, /*PointerIndex*/ 0u, Target, Target, NoButtons,
                           EKeys::Invalid, 0.f, App.GetModifierKeys());
        FWidgetPath Redirected;
        O->SetBoolField(TEXT("hover_delivered"),
            UAPRedirectPathToActiveGameWindow(Target, User, /*bIgnoreEnabledStatus*/ false, Redirected)
                ? App.RoutePointerMoveEvent(Redirected, Move, /*bIsSynthetic*/ false)
                : App.ProcessMouseMoveEvent(Move));
        O->SetBoolField(TEXT("redirected_to_active_game_window"), Redirected.IsValid());

        // 4. The game-side cache (GetMousePosition / GetHitResultUnderCursor), which that move
        //    event just updated. Read back, not written -- see UAPReadGameMouse.
        UAPReadGameMouse(O, Target);

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
