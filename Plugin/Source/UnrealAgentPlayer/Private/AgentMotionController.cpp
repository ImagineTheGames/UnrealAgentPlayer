#include "AgentMotionController.h"
#include "Features/IModularFeatures.h"

static const FName GAgentMCDeviceType = TEXT("UAPAgentController");

FAgentMotionController::FAgentMotionController() {}
FAgentMotionController::~FAgentMotionController() { Unregister(); }

void FAgentMotionController::Register()
{
    if (!bRegistered)
    {
        IModularFeatures::Get().RegisterModularFeature(IMotionController::GetModularFeatureName(), this);
        bRegistered = true;
    }
}

void FAgentMotionController::Unregister()
{
    if (bRegistered)
    {
        IModularFeatures::Get().UnregisterModularFeature(IMotionController::GetModularFeatureName(), this);
        bRegistered = false;
    }
}

void FAgentMotionController::SetPose(EAgentXRHand Hand, const FAgentControllerPose& Pose)
{
    FHandState& S = Hands[(int32)Hand];
    S.bActive = true;
    S.Pose = Pose;
}

void FAgentMotionController::ClearPose(EAgentXRHand Hand)
{
    Hands[(int32)Hand].bActive = false;
}

FName FAgentMotionController::GetMotionControllerDeviceTypeName() const { return GAgentMCDeviceType; }

bool FAgentMotionController::ResolveHand(const FName MotionSource, int32& OutIndex) const
{
    const FString S = MotionSource.ToString();
    if (S.Equals(TEXT("Left"), ESearchCase::IgnoreCase) || S.Equals(TEXT("LeftHand"), ESearchCase::IgnoreCase) || S.Equals(TEXT("LeftAim"), ESearchCase::IgnoreCase) || S.Equals(TEXT("LeftGrip"), ESearchCase::IgnoreCase))
    {
        OutIndex = (int32)EAgentXRHand::Left; return true;
    }
    if (S.Equals(TEXT("Right"), ESearchCase::IgnoreCase) || S.Equals(TEXT("RightHand"), ESearchCase::IgnoreCase) || S.Equals(TEXT("RightAim"), ESearchCase::IgnoreCase) || S.Equals(TEXT("RightGrip"), ESearchCase::IgnoreCase))
    {
        OutIndex = (int32)EAgentXRHand::Right; return true;
    }
    return false;
}

bool FAgentMotionController::GetControllerOrientationAndPosition(const int32, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition, float) const
{
    int32 Idx;
    if (!ResolveHand(MotionSource, Idx) || !Hands[Idx].bActive) { return false; }
    OutOrientation = Hands[Idx].Pose.Orientation;
    OutPosition = Hands[Idx].Pose.Position;
    return Hands[Idx].Pose.bTracked;
}

bool FAgentMotionController::GetControllerOrientationAndPosition(const int32 ControllerIndex, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition, bool& OutbProvidedLinearVelocity, FVector& OutLinearVelocity, bool& OutbProvidedAngularVelocity, FVector& OutAngularVelocityAsAxisAndLength, bool& OutbProvidedLinearAcceleration, FVector& OutLinearAcceleration, float WorldToMetersScale) const
{
    OutbProvidedLinearVelocity = false;
    OutbProvidedAngularVelocity = false;
    OutbProvidedLinearAcceleration = false;
    OutLinearVelocity = FVector::ZeroVector;
    OutAngularVelocityAsAxisAndLength = FVector::ZeroVector;
    OutLinearAcceleration = FVector::ZeroVector;
    return GetControllerOrientationAndPosition(ControllerIndex, MotionSource, OutOrientation, OutPosition, WorldToMetersScale);
}

bool FAgentMotionController::GetControllerOrientationAndPositionForTime(const int32 ControllerIndex, const FName MotionSource, FTimespan, bool& OutTimeWasUsed, FRotator& OutOrientation, FVector& OutPosition, bool& OutbProvidedLinearVelocity, FVector& OutLinearVelocity, bool& OutbProvidedAngularVelocity, FVector& OutAngularVelocityAsAxisAndLength, bool& OutbProvidedLinearAcceleration, FVector& OutLinearAcceleration, float WorldToMetersScale) const
{
    OutTimeWasUsed = false;
    return GetControllerOrientationAndPosition(ControllerIndex, MotionSource, OutOrientation, OutPosition, OutbProvidedLinearVelocity, OutLinearVelocity, OutbProvidedAngularVelocity, OutAngularVelocityAsAxisAndLength, OutbProvidedLinearAcceleration, OutLinearAcceleration, WorldToMetersScale);
}

ETrackingStatus FAgentMotionController::GetControllerTrackingStatus(const int32, const FName MotionSource) const
{
    int32 Idx;
    if (ResolveHand(MotionSource, Idx) && Hands[Idx].bActive && Hands[Idx].Pose.bTracked)
    {
        return ETrackingStatus::Tracked;
    }
    return ETrackingStatus::NotTracked;
}

void FAgentMotionController::EnumerateSources(TArray<FMotionControllerSource>& SourcesOut) const
{
    SourcesOut.Add(FMotionControllerSource(TEXT("Left")));
    SourcesOut.Add(FMotionControllerSource(TEXT("Right")));
}

float FAgentMotionController::GetCustomParameterValue(const FName, FName, bool& bOutValueFound) const
{
    bOutValueFound = false;
    return 0.f;
}

bool FAgentMotionController::GetHandJointPosition(const FName, int, FVector&) const
{
    return false;
}
