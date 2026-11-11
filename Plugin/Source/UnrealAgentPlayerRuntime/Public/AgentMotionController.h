#pragma once

#include "CoreMinimal.h"
#include "IMotionController.h"
#include "UAPAgentTypes.h"

/**
 * A fake IMotionController that returns agent-injected poses for Left/Right hands.
 * Registered as a modular feature so UMotionControllerComponent polls it like a real device.
 * When an override for a hand is cleared, GetControllerOrientationAndPosition returns false
 * for that source so real devices (if any) win.
 */
class UNREALAGENTPLAYERRUNTIME_API FAgentMotionController : public IMotionController
{
public:
    FAgentMotionController();
    virtual ~FAgentMotionController();

    void Register();
    void Unregister();

    void SetPose(EAgentXRHand Hand, const FAgentControllerPose& Pose);
    void ClearPose(EAgentXRHand Hand);

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
    // delta (Y = 0, pitch only) is the same transform.
    void SetAimEmulation(bool bEnabled, const FTransform& InRightGripToAim);
    bool IsAimEmulationEnabled() const { return bAimEmulation; }
    FTransform GetGripToAim(EAgentXRHand Hand) const;
    static FTransform QuestGripToAim();

    // JSON for the subsystems' SetXRAimEmulation / GetXRControllerState, so the editor and the
    // runtime subsystem cannot drift apart. A zero delta (location AND rotation) means "use the
    // Quest default": RemoteControl zero-initialises omitted arguments, and a zero delta is
    // exactly the aim == grip state emulation exists to remove.
    FString SetAimEmulationJson(bool bEnabled, const FVector& GripToAimLocation, const FRotator& GripToAimRotation);
    FString StateJson() const;

    // True when ButtonKeyName names the OTHER hand (e.g. Hand=Right, "OculusTouch_Left_X_Click").
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
    };
    FHandState Hands[2];

    bool ResolveHand(const FName MotionSource, int32& OutIndex, bool& bOutAim) const;
    bool bRegistered = false;
    bool bAimEmulation = false;
    FTransform RightGripToAim = FTransform::Identity;
};
