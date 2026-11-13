#include "AgentMotionController.h"
#include "Features/IModularFeatures.h"
#include "CoreGlobals.h"
#include "AgentWorld.h"
#include "UnrealAgentPlayerRuntimeModule.h"
#include "Engine/World.h"

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

int32 FAgentMotionController::PollingSlot()
{
    // The editor sets this to the PIE instance of the world it is ticking (SetPlayInEditorWorld),
    // thread-locally, for the whole tick of that world; INDEX_NONE in a standalone game.
    return UE::GetPlayInEditorID();
}

bool FAgentMotionController::ResolveSlots(const FString& Client, TArray<int32>& OutSlots, FString& OutError)
{
    OutSlots.Reset();
    OutError.Reset();
    if (Client.TrimStartAndEnd().IsEmpty())
    {
        // The DEFAULT client, not "all": the first game world, as every other verb treats it.
        // No game world yet (a pose set before PIE starts) -> client 0, which is what the first
        // PIE client will be.
        // A standalone (-game) world has no PIE instance: its slot is INDEX_NONE, which is also
        // what UE::GetPlayInEditorID() answers there.
        UWorld* W = FAgentWorld::GetActiveGameWorld();
        OutSlots.Add(W ? FAgentWorld::GetClientIndex(W) : 0);
        return true;
    }
    TArray<UWorld*> Worlds;
    if (!FAgentWorld::ResolveClients(Client, Worlds, OutError)) { return false; }
    for (UWorld* W : Worlds)
    {
        OutSlots.AddUnique(FAgentWorld::GetClientIndex(W));
    }
    return OutSlots.Num() > 0;
}

bool FAgentMotionController::SetPose(const FString& Client, EAgentXRHand Hand, const FAgentControllerPose& Pose)
{
    TArray<int32> Slots;
    FString Error;
    if (!ResolveSlots(Client, Slots, Error))
    {
        UE_LOG(LogUAPRuntime, Error, TEXT("InjectXRControllerPose: %s"), *Error);
        return false;
    }
    for (int32 Slot : Slots)
    {
        FHandState& S = Clients.FindOrAdd(Slot).Hands[(int32)Hand];
        S.bActive = true;
        S.Pose = Pose;
    }
    return true;
}

bool FAgentMotionController::ClearPose(const FString& Client, EAgentXRHand Hand)
{
    TArray<int32> Slots;
    FString Error;
    if (!ResolveSlots(Client, Slots, Error))
    {
        UE_LOG(LogUAPRuntime, Error, TEXT("ClearXRControllerOverride: %s"), *Error);
        return false;
    }
    for (int32 Slot : Slots)
    {
        if (FClientState* S = Clients.Find(Slot)) { S->Hands[(int32)Hand].bActive = false; }
    }
    return true;
}

FTransform FAgentMotionController::QuestGripToAim()
{
    // Measured on Quest through Meta's OpenXR runtime, both hands identical (see header).
    return FTransform(FRotator(-60.f, 0.f, 0.f), FVector(7.35f, 0.f, -6.73f));
}

FTransform FAgentMotionController::GripToAimFor(const FClientState& S, EAgentXRHand Hand)
{
    if (Hand == EAgentXRHand::Right)
    {
        return S.RightGripToAim;
    }
    // Mirror across the controller's XZ plane: Y, yaw and roll change sign.
    const FRotator R = S.RightGripToAim.Rotator();
    const FVector L = S.RightGripToAim.GetLocation();
    return FTransform(FRotator(R.Pitch, -R.Yaw, -R.Roll), FVector(L.X, -L.Y, L.Z));
}

FName FAgentMotionController::GetMotionControllerDeviceTypeName() const { return GAgentMCDeviceType; }

bool FAgentMotionController::ResolveHand(const FName MotionSource, int32& OutIndex, bool& bOutAim)
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

bool FAgentMotionController::Query(const FClientState& State, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition) const
{
    int32 Idx;
    bool bAim;
    if (!ResolveHand(MotionSource, Idx, bAim) || !State.Hands[Idx].bActive) { return false; }
    OutOrientation = State.Hands[Idx].Pose.Orientation;
    OutPosition = State.Hands[Idx].Pose.Position;
    if (bAim && State.bAimEmulation)
    {
        // Aim = GripToAim expressed in the grip frame, then the grip pose (UE order: A * B applies A first).
        const FTransform Grip(OutOrientation, OutPosition);
        const FTransform Aim = GripToAimFor(State, (EAgentXRHand)Idx) * Grip;
        OutOrientation = Aim.Rotator();
        OutPosition = Aim.GetLocation();
    }
    return State.Hands[Idx].Pose.bTracked;
}

bool FAgentMotionController::QueryForSlot(int32 Slot, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition) const
{
    const FClientState* S = Clients.Find(Slot);
    return S && Query(*S, MotionSource, OutOrientation, OutPosition);
}

bool FAgentMotionController::GetControllerOrientationAndPosition(const int32, const FName MotionSource, FRotator& OutOrientation, FVector& OutPosition, float) const
{
    // Off the game thread (the render thread's late update) there is no way to tell which client
    // is asking, and answering with another client's pose would move the wrong hand on screen.
    // Answer false: the component keeps the pose its game-thread poll already gave it.
    if (!IsInGameThread()) { return false; }
    return QueryForSlot(PollingSlot(), MotionSource, OutOrientation, OutPosition);
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
    if (!ResolveHand(MotionSource, Idx, bAim)) { return ETrackingStatus::NotTracked; }
    if (!IsInGameThread())
    {
        // The render thread cannot name its client and must not touch the game-thread-owned
        // Clients map. It only ever polls a component whose game-thread poll SUCCEEDED (so that
        // component is tracked), and its position poll above answers false, so Tracked is the
        // truthful value to leave in CurrentTrackingStatus.
        return ETrackingStatus::Tracked;
    }
    const FClientState* S = Clients.Find(PollingSlot());
    if (S && S->Hands[Idx].bActive && S->Hands[Idx].Pose.bTracked)
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

// One client -> its own object (already carries "client"); several -> {"ok":true,"clients":[...]}.
static FString UAPPerClientJson(const TArray<int32>& Slots, TFunctionRef<FString(int32)> One)
{
    if (Slots.Num() == 1) { return One(Slots[0]); }
    FString Joined;
    for (int32 I = 0; I < Slots.Num(); ++I)
    {
        Joined += (I ? TEXT(",") : TEXT("")) + One(Slots[I]);
    }
    return FString::Printf(TEXT("{\"ok\":true,\"clients\":[%s]}"), *Joined);
}

static FString UAPXRRefusalJson(const FString& Error)
{
    FString Esc = Error.Replace(TEXT("\\"), TEXT("\\\\")).Replace(TEXT("\""), TEXT("\\\""));
    return FString::Printf(TEXT("{\"ok\":false,\"error\":\"%s\"}"), *Esc);
}

FString FAgentMotionController::OneClientAimJson(int32 Slot, bool bEnabled, const FVector& GripToAimLocation, const FRotator& GripToAimRotation)
{
    const bool bDefault = GripToAimLocation.IsNearlyZero() && GripToAimRotation.IsNearlyZero();
    const FTransform Delta = bDefault ? QuestGripToAim() : FTransform(GripToAimRotation, GripToAimLocation);
    FClientState& S = Clients.FindOrAdd(Slot);
    S.bAimEmulation = bEnabled;
    S.RightGripToAim = Delta;
    const FTransform L = GripToAimFor(S, EAgentXRHand::Left);
    return FString::Printf(TEXT("{\"ok\":true,\"client\":%d,\"aim_emulation\":%s,\"delta_source\":\"%s\",\"right_grip_to_aim\":{\"loc\":%s,\"rot\":%s},\"left_grip_to_aim\":{\"loc\":%s,\"rot\":%s}}"),
        Slot,
        bEnabled ? TEXT("true") : TEXT("false"),
        bDefault ? TEXT("quest_default") : TEXT("caller"),
        *UAPVecJson(Delta.GetLocation()), *UAPRotJson(Delta.Rotator()),
        *UAPVecJson(L.GetLocation()), *UAPRotJson(L.Rotator()));
}

FString FAgentMotionController::SetAimEmulationJson(const FString& Client, bool bEnabled, const FVector& GripToAimLocation, const FRotator& GripToAimRotation)
{
    TArray<int32> Slots;
    FString Error;
    if (!ResolveSlots(Client, Slots, Error)) { return UAPXRRefusalJson(Error); }
    return UAPPerClientJson(Slots, [&](int32 Slot) { return OneClientAimJson(Slot, bEnabled, GripToAimLocation, GripToAimRotation); });
}

FString FAgentMotionController::OneClientStateJson(int32 Slot) const
{
    static const FClientState Empty;
    const FClientState* Found = Clients.Find(Slot);
    const FClientState& State = Found ? *Found : Empty;
    FString HandsJson;
    for (int32 I = 0; I < 2; ++I)
    {
        const FHandState& H = State.Hands[I];
        FString Entry = FString::Printf(TEXT("\"%s\":{\"active\":%s,\"tracked\":%s,\"grip\":{\"pos\":%s,\"rot\":%s}"),
            I == 0 ? TEXT("left") : TEXT("right"),
            H.bActive ? TEXT("true") : TEXT("false"),
            (H.bActive && H.Pose.bTracked) ? TEXT("true") : TEXT("false"),
            *UAPVecJson(H.Pose.Position), *UAPRotJson(H.Pose.Orientation));
        if (H.bActive)
        {
            // What THIS client's controller answers for "<Hand>Aim" right now (== grip when emulation is off).
            FRotator AimRot;
            FVector AimPos;
            Query(State, I == 0 ? FName(TEXT("LeftAim")) : FName(TEXT("RightAim")), AimRot, AimPos);
            Entry += FString::Printf(TEXT(",\"aim\":{\"pos\":%s,\"rot\":%s}"), *UAPVecJson(AimPos), *UAPRotJson(AimRot));
        }
        Entry += TEXT("}");
        HandsJson += (I ? TEXT(",") : TEXT("")) + Entry;
    }
    const FTransform R = GripToAimFor(State, EAgentXRHand::Right);
    return FString::Printf(TEXT("{\"ok\":true,\"client\":%d,\"registered\":%s,\"aim_emulation\":%s,\"right_grip_to_aim\":{\"loc\":%s,\"rot\":%s},\"hands\":{%s},\"note\":\"%s\"}"),
        Slot,
        bRegistered ? TEXT("true") : TEXT("false"),
        State.bAimEmulation ? TEXT("true") : TEXT("false"),
        *UAPVecJson(R.GetLocation()), *UAPRotJson(R.Rotator()),
        *HandsJson,
        State.bAimEmulation
            ? TEXT("aim pose EMULATED: <Hand>Aim = grip_to_aim * grip")
            : TEXT("aim pose NOT emulated: <Hand>Aim == grip (a real runtime differs; Quest is pitch -60)"));
}

FString FAgentMotionController::StateJson(const FString& Client) const
{
    TArray<int32> Slots;
    FString Error;
    if (!ResolveSlots(Client, Slots, Error)) { return UAPXRRefusalJson(Error); }
    return UAPPerClientJson(Slots, [this](int32 Slot) { return OneClientStateJson(Slot); });
}

bool FAgentMotionController::KeyContradictsHand(EAgentXRHand Hand, const FString& ButtonKeyName)
{
    const bool bNamesLeft = ButtonKeyName.Contains(TEXT("_Left_"), ESearchCase::IgnoreCase);
    const bool bNamesRight = ButtonKeyName.Contains(TEXT("_Right_"), ESearchCase::IgnoreCase);
    return (Hand == EAgentXRHand::Right && bNamesLeft) || (Hand == EAgentXRHand::Left && bNamesRight);
}
