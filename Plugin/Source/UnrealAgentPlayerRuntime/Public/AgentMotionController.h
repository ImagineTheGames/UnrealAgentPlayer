#pragma once

#include "CoreMinimal.h"
#include "IMotionController.h"
#include "UAPAgentTypes.h"
#include "Containers/Ticker.h"
#include "UObject/WeakObjectPtr.h"

class UActorComponent;
class UWorld;

/**
 * A fake IMotionController that returns agent-injected poses for Left/Right hands.
 * Registered as a modular feature so UMotionControllerComponent polls it like a real device.
 * When an override for a hand is cleared, GetControllerOrientationAndPosition returns false
 * for that source so real devices (if any) win.
 *
 * PER-CLIENT STATE [17tm466zwx5]. A modular feature is ONE object shared by every world in the
 * process, and every pawn's UMotionControllerComponent polls it with ControllerIndex 0, so a single
 * Hands[2] made `uap xr pose` move the hand on EVERY PIE client. State is now kept per PIE
 * instance. A poll is attributed to a client by UE::GetPlayInEditorID(), which the editor sets to
 * the world's PIE instance for the whole of that world's tick (the component's TickComponent runs
 * inside it). Callers that name a client use the same selector as every other `--client` verb
 * (FAgentWorld::ResolveClients); an EMPTY selector means "the default client", i.e. the first game
 * world (client 0), NOT "all" -- the old behaviour was the bug. "all" is explicit.
 * A poll made off the game thread (the render thread's late update) answers false: it cannot know
 * which client it serves, and the game-thread poll already placed the component.
 *
 * HEAD POSE. There is no registered feature for the HMD, so a PIE client's VR pawn never sends a
 * tracked head and the SERVER's copy of that player's head sits at the floor. EAgentXRHand::Head
 * (SetPose/ClearPose with Head) stores a head pose per client, and a core ticker writes it into that
 * client's local pawn the way a real HMD would: into the VRExpansion ReplicatedVRCameraComponent's
 * ReplicatedCameraTransform (found by class NAME through reflection, so this module needs no VRE
 * dependency) and through the component's own OnRep_ReplicatedCameraTransform, which places the
 * camera exactly as VRE does for a tracked HMD. VRE's own TickComponent then sends it to the server
 * (Server_SendCameraTransform) and replicates it to the other clients. Re-asserted every frame while
 * active; ClearPose(Head) stops asserting but leaves the head where it was last put. A head that
 * was applied to a client is DROPPED when that client's PIE session ends (a hand pose is sticky
 * across sessions, but a stale head would silently re-seat the next test's player); one set before
 * PIE has started waits for the first session.
 */
class UNREALAGENTPLAYERRUNTIME_API FAgentMotionController : public IMotionController
{
public:
    FAgentMotionController();
    virtual ~FAgentMotionController();

    void Register();
    void Unregister();

    // --- Per-client verbs. Client = "" (default client), "N" (PIE instance) or "all". ---------
    // A refusal (no such client) is logged to LogUAPRuntime (a bool cannot carry a reason) and
    // returns false; nothing is changed on ANY client in that case.
    // Hand may be EAgentXRHand::Head (see HEAD POSE above); the pose is then a TRACKING-SPACE head pose.
    bool SetPose(const FString& Client, EAgentXRHand Hand, const FAgentControllerPose& Pose);
    bool ClearPose(const FString& Client, EAgentXRHand Hand);

    // Resolve a Client selector to the state slots (PIE instance ids) it names. Public so the
    // subsystems and tests share one rule. Empty -> exactly one slot, the default client's.
    static bool ResolveSlots(const FString& Client, TArray<int32>& OutSlots, FString& OutError);

    // --- Aim-pose emulation (opt-in, OFF by default) ------------------------------------------
    // A real OpenXR runtime answers the "<Hand>Aim" motion source with a DIFFERENT pose from
    // "<Hand>"/"<Hand>Grip": on Quest the aim pose is the grip pose pitched -60 deg and offset
    // (7.35, 0, -6.73) cm in the grip frame (logged by a game's laser pointer as
    // "grip->aim loc=V(X=7.35, Y=0.00, Z=-6.73) rot=R(P=-60.00 ...)", both hands). With
    // emulation OFF this controller answers every source with the SAME pose, so aim == grip and
    // anything that reads the aim pose (a laser, a distance grab, a "does the nose point where
    // I point" check) sees a delta of zero -- the trap a grip-pose check fell into once already.
    // With emulation ON, "<Hand>Aim" answers GripToAim * grip. The delta is given for the RIGHT
    // hand; the left hand uses its mirror image (Y, yaw and roll negated), which for the Quest
    // delta (Y = 0, pitch only) is the same transform. Per client, like the poses.
    static FTransform QuestGripToAim();

    // JSON for the subsystems' SetXRAimEmulation / GetXRControllerState, so the editor and the
    // runtime subsystem cannot drift apart. A zero delta (location AND rotation) means "use the
    // Quest default": RemoteControl zero-initialises omitted arguments, and a zero delta is
    // exactly the aim == grip state emulation exists to remove. One client -> that client's
    // object with "client":N; several ("all") -> {"ok":true,"clients":[...]}.
    FString SetAimEmulationJson(const FString& Client, bool bEnabled, const FVector& GripToAimLocation, const FRotator& GripToAimRotation);
    FString StateJson(const FString& Client) const;

    // Test hooks: what THIS slot answers for a source, bypassing the "which client is polling"
    // lookup, so the per-client isolation can be unit-checked without a PIE.
    bool QueryForSlot(int32 Slot, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition) const;

    // True when ButtonKeyName names the OTHER hand (e.g. Hand=Right, "OculusTouch_Left_X_Click"),
    // and always for Head (a head has no buttons).
    // InjectXRButton used to ignore Hand entirely; a contradiction is now refused instead of
    // silently pressing the key the name says.
    static bool KeyContradictsHand(EAgentXRHand Hand, const FString& ButtonKeyName);

    // IMotionController
    virtual FName GetMotionControllerDeviceTypeName() const override;
    virtual bool GetControllerOrientationAndPosition(const int32 ControllerIndex, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition, float WorldToMetersScale) const override;
    virtual bool GetControllerOrientationAndPosition(const int32 ControllerIndex, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition, bool& OutbProvidedLinearVelocity, FVector& OutLinearVelocity, bool& OutbProvidedAngularVelocity, FVector& OutAngularVelocityAsAxisAndLength, bool& OutbProvidedLinearAcceleration, FVector& OutLinearAcceleration, float WorldToMetersScale) const override;
    virtual bool GetControllerOrientationAndPositionForTime(const int32 ControllerIndex, const FName MotionSource, FTimespan Time, bool& OutTimeWasUsed, FRotator& OutOrientation, FVector& OutPosition, bool& OutbProvidedLinearVelocity, FVector& OutLinearVelocity, bool& OutbProvidedAngularVelocity, FVector& OutAngularVelocityAsAxisAndLength, bool& OutbProvidedLinearAcceleration, FVector& OutLinearAcceleration, float WorldToMetersScale) const override;
    virtual ETrackingStatus GetControllerTrackingStatus(const int32 ControllerIndex, const FName MotionSource) const override;
    virtual void EnumerateSources(TArray<FMotionControllerSource>& SourcesOut) const override;
    virtual float GetCustomParameterValue(const FName MotionSource, FName ParameterName, bool& bOutValueFound) const override;
    virtual bool GetHandJointPosition(const FName MotionSource, int jointIndex, FVector& OutPosition) const override;

private:
    struct FHandState
    {
        bool bActive = false;
        FAgentControllerPose Pose;
        // Head only: what the ticker did with it.
        TWeakObjectPtr<UActorComponent> Target;   // the pawn's ReplicatedVRCameraComponent it last wrote
        int32 ApplyCount = 0;
        bool bEverApplied = false;                 // once true, losing the client's world ends the injection
        FString Note;                              // why it could not apply, if it could not
    };
    // One client's injected controllers + aim emulation.
    struct FClientState
    {
        FHandState Hands[2];
        FHandState Head;
        bool bAimEmulation = false;
        FTransform RightGripToAim = FTransform::Identity;
    };
    // Keyed by PIE instance id (INDEX_NONE for a standalone game world). Game thread only.
    TMap<int32, FClientState> Clients;

    static FHandState& HandFor(FClientState& S, EAgentXRHand Hand) { return Hand == EAgentXRHand::Head ? S.Head : S.Hands[(int32)Hand]; }
    bool TickHead(float DeltaTime);
    void ApplyHead(UWorld* World, FHandState& Head);
    FTSTicker::FDelegateHandle HeadTicker;

    static FTransform GripToAimFor(const FClientState& S, EAgentXRHand Hand);
    bool Query(const FClientState& S, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition) const;
    FString OneClientStateJson(int32 Slot) const;
    FString OneClientAimJson(int32 Slot, bool bEnabled, const FVector& GripToAimLocation, const FRotator& GripToAimRotation);

    static bool ResolveHand(const FName MotionSource, int32& OutIndex, bool& bOutAim);
    // The state slot of the client whose world is ticking right now (UE::GetPlayInEditorID()).
    static int32 PollingSlot();
    bool bRegistered = false;
};
