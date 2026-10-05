#pragma once

#include "CoreMinimal.h"
#include "InputCoreTypes.h"
#include "UAPAgentTypes.h"

class SWidget;

/** One entry of the held-input registry, as reported back to an agent. */
struct FAgentHeldInputInfo
{
    FString Key;
    bool    bAnalog = false;
    float   Value = 0.f;
    float   RemainingSeconds = 0.f;
    /** INDEX_NONE = re-asserted on the game-viewport route; >= 0 = on the Slate route as
        that user. Reported back so a caller can SEE which layer is being driven instead of
        assuming; a Slate pre-processor only ever sees the second kind. */
    int32   SlateUserIndex = INDEX_NONE;
};

class UNREALAGENTPLAYERRUNTIME_API FAgentInput
{
public:
    // NOTE ON RETURN VALUES: these report whether the event was DELIVERED to the game, not
    // whether the game did anything with it. UGameViewportClient::InputKey forwards
    // UPlayerInput::InputKey, which for IE_Pressed returns IsKeyHandledByAction() -- a check
    // against the LEGACY ActionMappings array only. Under Enhanced Input that is false for
    // essentially every key, so "handled" is useless as a success signal and treating it as
    // one is what leaked a permanently stuck key (see HoldKey).
    static bool InjectKey(FKey Key, bool bPressed, bool bRepeat);

    // NOTE ON UserIndex (the Slate-path injects below): Slate stamps every input event with a
    // USER index and handlers filter on it -- FAnalogCursor::IsRelevantInput() is literally
    // `GetOwnerUserIndex() == InputEvent.GetUserIndex()` (engine AnalogCursor.cpp:192). These
    // used to stamp 0 unconditionally, so an event aimed at a handler owned by any other user
    // was DISCARDED before that handler ran: no error, no warning, and it looks exactly like
    // the feature under test is broken. INDEX_NONE means "resolve it" (see
    // ResolveSlateUserIndex); an explicit index targets one user and REFUSES LOUDLY if Slate
    // has no such user, rather than throwing the event away.
    static bool InjectMouseMove(FVector2D Delta, bool bAbsolute, int32 UserIndex = INDEX_NONE);
    static bool InjectMouseButton(EAgentMouseButton Btn, bool bPressed, int32 UserIndex = INDEX_NONE);

    // --- Mouse position: the AGENT CURSOR ---------------------------------------------
    // A PIE viewport holds the mouse, and while it does there is NO way to move the pointer.
    // Win32 SetCursorPos is inert, and so is FSlateApplication::SetCursorPos, which is a thin
    // wrapper over the same platform cursor; FSlateUser then reads the position straight back
    // out of that cursor (engine SlateUser.cpp:445), so nothing engine-side can cache a
    // different one either. Measured against a live PIE session: SetCursorPos(1754, 989)
    // followed by GetCursorPos() still read 0,0, and InjectMouseMove returned true.
    //
    // What works is NOT MOVING THE CURSOR AT ALL. Slate routes a pointer event by the position
    // carried ON THE EVENT: ProcessMouseButtonDownEvent locates the widget path with
    // LocateWindowUnderMouse(MouseEvent.GetScreenSpacePosition(), ...) (engine
    // SlateApplication.cpp:5282), ProcessMouseMoveEvent does the same (:6300), and SButton's
    // click test is MyGeometry.IsUnderLocation(MouseEvent.GetScreenSpacePosition()). So the
    // agent cursor is a REMEMBERED POSITION that every injected pointer event is stamped with.
    //
    // This is exactly what the old InjectMouseButton did not do: it read App.GetCursorPos() for
    // its event position, so with the real cursor pinned at the origin every injected click
    // landed at 0,0, hit nothing, and reported ok:true. That is ClickUp 17tm466fbyj in full --
    // the click layer was right the whole time, the POSITION was 0,0.
    //
    // The GAME side is a separate cache and that one IS settable under capture:
    // APlayerController::SetMouseLocation -> FSceneViewport::SetMouse writes CachedCursorPos,
    // which is what PlayerController::GetMousePosition / GetHitResultUnderCursor read. Both are
    // set, and BOTH are reported with a read-back, because "asked for" is not "took".

    /** The position injected pointer events are stamped with. Falls back to the real cursor. */
    static FVector2D GetAgentCursorPos();

    /**
     * Move the agent cursor to an ABSOLUTE screen-space point -- the same space `read-ui`
     * reports (FGeometry::GetAbsolutePosition). Returns JSON reporting what each layer did:
     * which position injected events will now use, whether the OS cursor moved (usually NOT,
     * under capture), whether the game-side viewport mouse took, and WHAT SLATE WIDGET IS
     * UNDER THE POINT. The last one is the anti-false-positive: a move onto empty space or
     * onto SViewport is reported as such instead of a bare ok:true.
     */
    static FString SetMousePositionJson(float X, float Y);

    /**
     * Press+release a mouse button at the agent cursor, or at X,Y first if given. X/Y are
     * STRINGS for the RemoteControl reason documented on ResolveSlateUserParam: RC builds the
     * argument struct zero-initialised, so an omitted float would arrive as 0.0 -- a VALID
     * screen position -- and silently click the top-left corner, which is the exact failure
     * this verb exists to end. Empty means "use the agent cursor".
     *
     * REFUSES (ok:false, nothing injected, `disabled_widget` naming it) when a DISABLED widget
     * is in the way. Slate would neither skip it nor swallow the click: it truncates the hit
     * path at the disabled widget and routes the press to the parent container, which ignores
     * it, while ProcessMouseButtonDownEvent returns true regardless -- so clicking anyway
     * reports clicked/down_handled/up_handled and proves nothing. Use `read-ui`'s `enabled`
     * field to pick a target; the raw InjectMouseMove + InjectMouseButton primitives are
     * unchanged and remain the way to drive a disabled target deliberately. [17tm466g0jf]
     */
    static FString ClickMouseJson(EAgentMouseButton Btn, const FString& XStr, const FString& YStr);

    /**
     * Clear a Slate POINTER capture for the resolved user. Separate from ReleaseHeldJson on
     * purpose: a captor is not a held key, and the game viewport holds one legitimately during
     * normal play, so this is an explicit recovery rather than part of a routine input reset.
     * Reports the holder it released, or released:false when there was nothing to release.
     */
    static FString ReleaseSlatePointerCaptureJson();

    /** Pointer button event stamped with an EXPLICIT position instead of the real cursor's. */
    static bool InjectMouseButtonAt(EAgentMouseButton Btn, bool bPressed, FVector2D ScreenPos,
                                    int32 UserIndex = INDEX_NONE);

    /**
     * "SWindow > SViewport > ... > SButton" -- the widgets Slate finds under an absolute point,
     * via the SAME lookup ProcessMouseButtonDownEvent uses. Empty means a click there hits
     * nothing. This is the anti-false-positive: it is what turns "the events were delivered"
     * into "they were delivered TO SOMETHING".
     */
    static FString DescribeWidgetsAt(FVector2D ScreenPos, int32 UserIndex);
    static bool InjectGamepad(EAgentGamepadButton Btn, bool bPressed, float AnalogValue,
                              int32 UserIndex = INDEX_NONE);

    /**
     * Analog sample. UserIndex == INDEX_NONE keeps the historical routing: the game-viewport
     * route first (gameplay/Enhanced Input), Slate only when there is no viewport at all. An
     * explicit UserIndex FORCES the Slate route, because a user index is a Slate concept and
     * the viewport route cannot carry one -- asking for a user is asking for that layer.
     */
    static bool InjectAxis(FName AxisName, float Value, int32 UserIndex = INDEX_NONE);

    /** One analog sample for an axis FKey, routed through the game viewport (see .cpp). */
    static bool InjectAxisKey(FKey Key, float Value);

    /** One analog sample stamped for a Slate user, entering the pre-processor chain. */
    static bool InjectAxisSlate(FKey Key, float Value, int32 UserIndex);

    // --- Slate user index -------------------------------------------------------------
    /**
     * Resolve the Slate user an injected event should be stamped with. RequestedIndex of
     * INDEX_NONE resolves automatically: the user whose FOCUS PATH contains the game viewport
     * widget (that is the user whose input the game is actually routing), else the keyboard
     * user, else Slate's cursor user. Returns INDEX_NONE and fills OutError when the answer
     * cannot be used -- an index Slate has no user for cannot receive anything, and returning
     * it would put the discard back.
     */
    static int32 ResolveSlateUserIndex(int32 RequestedIndex, FString& OutError);

    /** "0 (focus: SViewport), 1" -- registered Slate users, for the refusal text. */
    static FString DescribeSlateUsers();

    /**
     * The RC/Blueprint-facing form of the above. The parameter is a STRING, not an int, on
     * purpose: RemoteControl builds the argument struct zero-initialised, so an int32 the
     * caller omitted would arrive as 0 -- a VALID user index -- and silently switch every
     * existing call onto the Slate route. Empty means "auto" and is the only value that
     * survives being omitted.
     *
     * ""        -> OutResolved = INDEX_NONE (unchanged routing), returns true
     * "0", "2"  -> resolved + validated Slate user index, or false + OutError
     * anything else -> false + OutError
     */
    static bool ResolveSlateUserParam(const FString& SlateUser, int32& OutResolved, FString& OutError);

    /** True when there is a game viewport that will accept injected input right now. */
    static bool HasLiveViewport();

    /** Engine ground truth: is this key currently down on the first local player? */
    static bool IsKeyDown(FKey Key);

    // --- Held input -------------------------------------------------------------------
    // A single injected event cannot drive sustained locomotion: an external CLI round-trip
    // is ~1s, and anything that calls APlayerController::FlushPressedKeys (input-mode change,
    // focus loss, PC recreation) silently drops a latched key with no signal to the caller.
    // Analog axes are worse -- a real stick re-sends its value every frame, so a one-shot
    // sample is not what the game sees. These re-assert the input every frame, in-engine,
    // until the duration expires, then release cleanly.
    //
    // The *Json entrypoints are what the subsystems expose: a refusal has to say WHY, because
    // a guessed error message ("unknown key name") sent a real investigation down the wrong
    // path. Every one of them validates fully BEFORE pressing anything, so a refused call has
    // no side effects, and unwinds anything it did press if a later step fails.

    static FString HoldKeyJson(const FString& KeyName, float Seconds);
    static FString HoldAxisJson(const FString& AxisKeyName, float Value, float Seconds,
                                const FString& SlateUser = FString());

    /**
     * Release input. Empty KeyName is the RECOVERY path: it releases every registry entry AND
     * flushes every key the engine still has down, so a key the registry lost track of can
     * always be cleared without restarting PIE. A named key is force-released whether or not
     * the registry knows about it.
     */
    static FString ReleaseHeldJson(const FString& KeyName);
    static FString GetHeldJson();

    /** Press Key now and hold it (IE_Repeat every frame) for Seconds, then release. */
    static bool HoldKey(FKey Key, float Seconds);

    /**
     * Drive an analog axis FKey at Value every frame for Seconds, then recentre to 0.
     * SlateUserIndex >= 0 re-asserts it on the SLATE route as that user (the only route a
     * Slate pre-processor -- an analog/virtual cursor -- can see); INDEX_NONE keeps the
     * game-viewport route.
     */
    static bool HoldAxis(FKey Key, float Value, float Seconds, int32 SlateUserIndex = INDEX_NONE);

    /** Release a held input early. An invalid Key releases everything. Returns count released. */
    static int32 ReleaseHeld(FKey Key);

    /** APlayerController::FlushPressedKeys on every local PC. Returns how many were flushed. */
    static int32 FlushAllPressedKeys();

    static void GetHeld(TArray<FAgentHeldInputInfo>& Out);

    /** Module shutdown: drop the ticker and forget every hold. */
    static void ShutdownHolds();

private:
    static TSharedPtr<SWidget> FindPIEViewportWidget();
    static FKey GamepadButtonToKey(EAgentGamepadButton Btn);
    static FKey MouseButtonToKey(EAgentMouseButton Btn);
};
