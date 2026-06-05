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
class FAgentMotionController : public IMotionController
{
public:
    FAgentMotionController();
    virtual ~FAgentMotionController();

    void Register();
    void Unregister();

    void SetPose(EAgentXRHand Hand, const FAgentControllerPose& Pose);
    void ClearPose(EAgentXRHand Hand);

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

    bool ResolveHand(const FName MotionSource, int32& OutIndex) const;
    bool bRegistered = false;
};
