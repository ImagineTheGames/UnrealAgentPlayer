#include "UAPReachProbe.h"

#include "Engine/World.h"
#include "EngineUtils.h"
#include "GameFramework/Pawn.h"
#include "GameFramework/PlayerController.h"
#include "Components/SceneComponent.h"
#include "Components/PrimitiveComponent.h"
#include "Components/SphereComponent.h"
#include "Kismet/GameplayStatics.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

namespace
{
    FString UAPReachProbeWrite(const TSharedRef<FJsonObject>& O)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(O, W);
        return Out;
    }

    FString UAPReachProbeFail(const FString& Msg)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("ok"), false);
        O->SetStringField(TEXT("error"), Msg);
        return UAPReachProbeWrite(O);
    }

    TArray<TSharedPtr<FJsonValue>> UAPReachProbeVec(double X, double Y, double Z)
    {
        TArray<TSharedPtr<FJsonValue>> A;
        A.Add(MakeShared<FJsonValueNumber>(X));
        A.Add(MakeShared<FJsonValueNumber>(Y));
        A.Add(MakeShared<FJsonValueNumber>(Z));
        return A;
    }

    TArray<TSharedPtr<FJsonValue>> UAPReachProbeVec(const FVector& V) { return UAPReachProbeVec(V.X, V.Y, V.Z); }

    // Exactly one actor whose name contains Spec (an exact name match wins), else null + the hits.
    AActor* UAPReachProbeFindActor(UWorld* World, const FString& Spec, TArray<FString>& OutHits)
    {
        TArray<AActor*> Exact, Part;
        for (TActorIterator<AActor> It(World); It; ++It)
        {
            const FString Name = It->GetName();
            if (Name.Equals(Spec, ESearchCase::IgnoreCase)) { Exact.Add(*It); }
            else if (Name.Contains(Spec, ESearchCase::IgnoreCase)) { Part.Add(*It); }
        }
        const TArray<AActor*>& Hits = Exact.Num() ? Exact : Part;
        if (Hits.Num() != 1)
        {
            for (int32 i = 0; i < Hits.Num() && i < 8; ++i) { OutHits.Add(Hits[i]->GetName()); }
            return nullptr;
        }
        return Hits[0];
    }
}

FString UAPReachProbeJson(UWorld* World, const FString& HandIn, const FString& Target, const FString& OffsetCsv)
{
    if (!World) { return UAPReachProbeFail(TEXT("no active game world")); }
    APlayerController* PC = UGameplayStatics::GetPlayerController(World, 0);
    APawn* Pawn = PC ? PC->GetPawn() : nullptr;
    if (!Pawn)
    {
        return UAPReachProbeFail(TEXT("client has no controlled pawn (no PIE, or the VR pawn is not possessed)"));
    }
    const FString Hand = HandIn.ToLower();

    bool bHasOffset = false;
    FVector Offset = FVector::ZeroVector;
    if (!OffsetCsv.TrimStartAndEnd().IsEmpty())
    {
        TArray<FString> P;
        OffsetCsv.ParseIntoArray(P, TEXT(","), true);
        if (P.Num() != 3) { return UAPReachProbeFail(TEXT("offset wants X,Y,Z")); }
        Offset = FVector(FCString::Atod(*P[0]), FCString::Atod(*P[1]), FCString::Atod(*P[2]));
        bHasOffset = true;
    }

    FString ActorSpec = Target;
    FString CompSpec;
    Target.Split(TEXT("."), &ActorSpec, &CompSpec);
    TArray<FString> Hits;
    AActor* Act = UAPReachProbeFindActor(World, ActorSpec, Hits);
    if (!Act)
    {
        return UAPReachProbeFail(FString::Printf(TEXT("target actor '%s' matched %d actors: %s"),
            *ActorSpec, Hits.Num(), *FString::Join(Hits, TEXT(", "))));
    }

    USceneComponent* Comp = nullptr;
    if (!CompSpec.IsEmpty())
    {
        TInlineComponentArray<USceneComponent*> Cs(Act);
        for (USceneComponent* C : Cs)
        {
            if (C->GetName().Equals(CompSpec, ESearchCase::IgnoreCase)) { Comp = C; break; }
        }
        if (!Comp)
        {
            return UAPReachProbeFail(FString::Printf(TEXT("actor %s has no component '%s'"), *Act->GetName(), *CompSpec));
        }
    }
    else
    {
        Comp = Act->FindComponentByClass<UPrimitiveComponent>();
    }

    FVector TargetPoint = FVector::ZeroVector;
    bool bHaveTarget = false;
    FString How = TEXT("actor location");
    if (Comp)
    {
        for (const FName& S : Comp->GetAllSocketNames())
        {
            const FString L = S.ToString().ToLower();
            if (L.Contains(TEXT("grip")) || L.Contains(TEXT("hand")) || L.Contains(TEXT("grab")))
            {
                TargetPoint = Comp->GetSocketLocation(S);
                bHaveTarget = true;
                How = TEXT("socket ") + S.ToString();
                break;
            }
        }
    }
    if (!bHaveTarget && !CompSpec.IsEmpty() && Comp)
    {
        TargetPoint = Comp->Bounds.Origin;
        bHaveTarget = true;
        How = TEXT("component bounds centre");
    }
    if (bHasOffset)
    {
        const FTransform Base = Comp ? Comp->GetComponentTransform() : Act->GetActorTransform();
        TargetPoint = Base.TransformPosition(Offset);
        bHaveTarget = true;
        How = FString::Printf(TEXT("component local offset (%g, %g, %g)"), Offset.X, Offset.Y, Offset.Z);
    }
    if (!bHaveTarget) { TargetPoint = Act->GetActorLocation(); }

    USceneComponent* Ctrl = nullptr;
    TArray<FString> Names;
    {
        TInlineComponentArray<USceneComponent*> Cs(Pawn);
        for (USceneComponent* C : Cs)
        {
            if (C->GetClass()->GetName() != TEXT("GripMotionControllerComponent")) { continue; }
            const FString N = C->GetName();
            Names.Add(N);
            if (N.ToLower().Contains(Hand) && !N.ToLower().Contains(TEXT("aim"))) { Ctrl = C; }
        }
    }
    if (!Ctrl)
    {
        return UAPReachProbeFail(FString::Printf(TEXT("no %s GripMotionControllerComponent on pawn %s (found %s)"),
            *Hand, *Pawn->GetName(), *FString::Join(Names, TEXT(", "))));
    }

    USceneComponent* Grab = Ctrl;
    FString GrabName = Ctrl->GetName();
    double Radius = 0.0;
    {
        TArray<USceneComponent*> Kids;
        Ctrl->GetChildrenComponents(true, Kids);
        for (USceneComponent* K : Kids)
        {
            if (K->GetClass()->GetName().Contains(TEXT("SphereComponent")) && K->GetName().ToLower().Contains(TEXT("grab")))
            {
                Grab = K;
                GrabName = K->GetName();
                if (const USphereComponent* Sp = Cast<USphereComponent>(K)) { Radius = Sp->GetUnscaledSphereRadius(); }
                break;
            }
        }
    }

    const USceneComponent* Par = Ctrl->GetAttachParent();
    const FTransform ParTf = Par ? Par->GetComponentTransform() : FTransform::Identity;
    const FVector GrabPos = Grab->GetComponentTransform().GetTranslation();
    const FVector T1 = ParTf.InverseTransformPosition(TargetPoint);
    const FVector G1 = ParTf.InverseTransformPosition(GrabPos);
    const FVector D = T1 - G1;
    const FVector RL = Ctrl->GetRelativeLocation();
    const FRotator RR = Ctrl->GetRelativeRotation();

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("actor"), Act->GetName());
    O->SetStringField(TEXT("target_how"), How);
    O->SetArrayField(TEXT("target"), UAPReachProbeVec(TargetPoint));
    O->SetArrayField(TEXT("grab"), UAPReachProbeVec(GrabPos));
    O->SetStringField(TEXT("grab_component"), GrabName);
    O->SetNumberField(TEXT("grab_radius"), Radius);
    O->SetStringField(TEXT("controller"), Ctrl->GetName());
    O->SetArrayField(TEXT("delta_tracking"), UAPReachProbeVec(D));
    O->SetArrayField(TEXT("ctrl_rel_loc"), UAPReachProbeVec(RL));
    O->SetArrayField(TEXT("ctrl_rel_rot"), UAPReachProbeVec(RR.Pitch, RR.Yaw, RR.Roll));
    return UAPReachProbeWrite(O);
}
