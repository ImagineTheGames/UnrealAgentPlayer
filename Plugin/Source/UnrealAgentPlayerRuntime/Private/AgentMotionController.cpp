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

FTransform FAgentMotionController::QuestGripToAim()
{
    // Measured on Quest through Meta's OpenXR runtime, both hands identical (see header).
    return FTransform(FRotator(-60.f, 0.f, 0.f), FVector(7.35f, 0.f, -6.73f));
}

void FAgentMotionController::SetAimEmulation(bool bEnabled, const FTransform& InRightGripToAim)
{
    bAimEmulation = bEnabled;
    RightGripToAim = InRightGripToAim;
}

FTransform FAgentMotionController::GetGripToAim(EAgentXRHand Hand) const
{
    if (Hand == EAgentXRHand::Right)
    {
        return RightGripToAim;
    }
    // Mirror across the controller's XZ plane: Y, yaw and roll change sign.
    const FRotator R = RightGripToAim.Rotator();
    const FVector L = RightGripToAim.GetLocation();
    return FTransform(FRotator(R.Pitch, -R.Yaw, -R.Roll), FVector(L.X, -L.Y, L.Z));
}

FName FAgentMotionController::GetMotionControllerDeviceTypeName() const { return GAgentMCDeviceType; }

bool FAgentMotionController::ResolveHand(const FName MotionSource, int32& OutIndex, bool& bOutAim) const
{
    const FString S = MotionSource.ToString();
    bOutAim = false;
    if (S.Equals(TEXT("Left"), ESearchCase::IgnoreCase) || S.Equals(TEXT("LeftHand"), ESearchCase::IgnoreCase) || S.Equals(TEXT("LeftGrip"), ESearchCase::IgnoreCase))
    {
        OutIndex = (int32)EAgentXRHand::Left; return true;
    }
    if (S.Equals(TEXT("LeftAim"), ESearchCase::IgnoreCase))
    {
        OutIndex = (int32)EAgentXRHand::Left; bOutAim = true; return true;
    }
    if (S.Equals(TEXT("Right"), ESearchCase::IgnoreCase) || S.Equals(TEXT("RightHand"), ESearchCase::IgnoreCase) || S.Equals(TEXT("RightGrip"), ESearchCase::IgnoreCase))
    {
        OutIndex = (int32)EAgentXRHand::Right; return true;
    }
    if (S.Equals(TEXT("RightAim"), ESearchCase::IgnoreCase))
    {
        OutIndex = (int32)EAgentXRHand::Right; bOutAim = true; return true;
    }
    return false;
}

bool FAgentMotionController::GetControllerOrientationAndPosition(const int32, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition, float) const
{
    int32 Idx;
    bool bAim;
    if (!ResolveHand(MotionSource, Idx, bAim) || !Hands[Idx].bActive) { return false; }
    OutOrientation = Hands[Idx].Pose.Orientation;
    OutPosition = Hands[Idx].Pose.Position;
    if (bAim && bAimEmulation)
    {
        // Aim = GripToAim expressed in the grip frame, then the grip pose (UE order: A * B applies A first).
        const FTransform Grip(OutOrientation, OutPosition);
        const FTransform Aim = GetGripToAim((EAgentXRHand)Idx) * Grip;
        OutOrientation = Aim.Rotator();
        OutPosition = Aim.GetLocation();
    }
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
    bool bAim;
    if (ResolveHand(MotionSource, Idx, bAim) && Hands[Idx].bActive && Hands[Idx].Pose.bTracked)
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

static FString UAPVecJson(const FVector& V)
{
    return FString::Printf(TEXT("[%.3f,%.3f,%.3f]"), V.X, V.Y, V.Z);
}

static FString UAPRotJson(const FRotator& R)
{
    // Named, because the order of a bare triple is exactly what gets confused (Python's
    // unreal.Rotator takes roll, pitch, yaw; FRotator takes pitch, yaw, roll).
    return FString::Printf(TEXT("{\"pitch\":%.3f,\"yaw\":%.3f,\"roll\":%.3f}"), R.Pitch, R.Yaw, R.Roll);
}

FString FAgentMotionController::SetAimEmulationJson(bool bEnabled, const FVector& GripToAimLocation, const FRotator& GripToAimRotation)
{
    const bool bDefault = GripToAimLocation.IsNearlyZero() && GripToAimRotation.IsNearlyZero();
    const FTransform Delta = bDefault ? QuestGripToAim() : FTransform(GripToAimRotation, GripToAimLocation);
    SetAimEmulation(bEnabled, Delta);
    return FString::Printf(TEXT("{\"ok\":true,\"aim_emulation\":%s,\"delta_source\":\"%s\",\"right_grip_to_aim\":{\"loc\":%s,\"rot\":%s},\"left_grip_to_aim\":{\"loc\":%s,\"rot\":%s}}"),
        bEnabled ? TEXT("true") : TEXT("false"),
        bDefault ? TEXT("quest_default") : TEXT("caller"),
        *UAPVecJson(Delta.GetLocation()), *UAPRotJson(Delta.Rotator()),
        *UAPVecJson(GetGripToAim(EAgentXRHand::Left).GetLocation()), *UAPRotJson(GetGripToAim(EAgentXRHand::Left).Rotator()));
}

FString FAgentMotionController::StateJson() const
{
    FString HandsJson;
    for (int32 I = 0; I < 2; ++I)
    {
        const FHandState& H = Hands[I];
        FString Entry = FString::Printf(TEXT("\"%s\":{\"active\":%s,\"tracked\":%s,\"grip\":{\"pos\":%s,\"rot\":%s}"),
            I == 0 ? TEXT("left") : TEXT("right"),
            H.bActive ? TEXT("true") : TEXT("false"),
            (H.bActive && H.Pose.bTracked) ? TEXT("true") : TEXT("false"),
            *UAPVecJson(H.Pose.Position), *UAPRotJson(H.Pose.Orientation));
        if (H.bActive)
        {
            // What THIS controller answers for "<Hand>Aim" right now (== grip when emulation is off).
            FRotator AimRot;
            FVector AimPos;
            GetControllerOrientationAndPosition(0, I == 0 ? FName(TEXT("LeftAim")) : FName(TEXT("RightAim")), AimRot, AimPos, 100.f);
            Entry += FString::Printf(TEXT(",\"aim\":{\"pos\":%s,\"rot\":%s}"), *UAPVecJson(AimPos), *UAPRotJson(AimRot));
        }
        Entry += TEXT("}");
        HandsJson += (I ? TEXT(",") : TEXT("")) + Entry;
    }
    const FTransform R = GetGripToAim(EAgentXRHand::Right);
    return FString::Printf(TEXT("{\"ok\":true,\"registered\":%s,\"aim_emulation\":%s,\"right_grip_to_aim\":{\"loc\":%s,\"rot\":%s},\"hands\":{%s},\"note\":\"%s\"}"),
        bRegistered ? TEXT("true") : TEXT("false"),
        bAimEmulation ? TEXT("true") : TEXT("false"),
        *UAPVecJson(R.GetLocation()), *UAPRotJson(R.Rotator()),
        *HandsJson,
        bAimEmulation
            ? TEXT("aim pose EMULATED: <Hand>Aim = grip_to_aim * grip")
            : TEXT("aim pose NOT emulated: <Hand>Aim == grip (a real runtime differs; Quest is pitch -60)"));
}

bool FAgentMotionController::KeyContradictsHand(EAgentXRHand Hand, const FString& ButtonKeyName)
{
    const bool bNamesLeft = ButtonKeyName.Contains(TEXT("_Left_"), ESearchCase::IgnoreCase);
    const bool bNamesRight = ButtonKeyName.Contains(TEXT("_Right_"), ESearchCase::IgnoreCase);
    return (Hand == EAgentXRHand::Right && bNamesLeft) || (Hand == EAgentXRHand::Left && bNamesRight);
}
