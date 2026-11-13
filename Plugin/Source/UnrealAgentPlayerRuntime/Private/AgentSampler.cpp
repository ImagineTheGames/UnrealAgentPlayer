#include "AgentSampler.h"

#include "AgentWorld.h"
#include "UnrealAgentPlayerRuntimeModule.h"
#include "Containers/Ticker.h"
#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Engine/World.h"
#include "EngineUtils.h"
#include "GameFramework/Actor.h"
#include "GameFramework/PlayerController.h"
#include "Camera/PlayerCameraManager.h"
#include "Components/SceneComponent.h"
#include "HAL/PlatformTime.h"
#include "JsonObjectConverter.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "UObject/UnrealType.h"

namespace
{
    struct FUAPSeries
    {
        TWeakObjectPtr<UObject> Object;
        FString ObjectLabel;
        FString PropertyPath;
        double  StartRealTime = 0.0;
        double  EndRealTime = 0.0;
        int32   MaxSamples = 0;
        bool    bActive = false;
        FString LastError;
        TArray<double> Times;
        TArray<TSharedPtr<FJsonValue>> Values;
    };

    /** Series by name. The empty name is the legacy single series (`sample start` with no --name). */
    TMap<FString, FUAPSeries> GSeries;
    FTSTicker::FDelegateHandle GSampleTicker;
    constexpr int32 GMaxSeries = 16;

    FString UAPToJsonString(const TSharedRef<FJsonObject>& Obj)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Obj, W);
        return Out;
    }

    FString UAPError(const FString& Message)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetBoolField(TEXT("ok"), false);
        O->SetStringField(TEXT("error"), Message);
        return UAPToJsonString(O);
    }

    TSharedPtr<FJsonValue> UAPVector(const FVector& V)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetNumberField(TEXT("x"), V.X);
        O->SetNumberField(TEXT("y"), V.Y);
        O->SetNumberField(TEXT("z"), V.Z);
        return MakeShared<FJsonValueObject>(O);
    }

    TSharedPtr<FJsonValue> UAPRotator(const FRotator& R)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetNumberField(TEXT("pitch"), R.Pitch);
        O->SetNumberField(TEXT("yaw"), R.Yaw);
        O->SetNumberField(TEXT("roll"), R.Roll);
        return MakeShared<FJsonValueObject>(O);
    }

    /** Computed leaf values that are not plain FProperties but are what tests actually measure. */
    bool UAPComputedValue(UObject* Obj, const FString& Name, TSharedPtr<FJsonValue>& Out)
    {
        AActor* Actor = Cast<AActor>(Obj);
        USceneComponent* Comp = Cast<USceneComponent>(Obj);
        if (!Actor && !Comp) { return false; }

        const FTransform Xf = Actor ? Actor->GetActorTransform() : Comp->GetComponentTransform();
        if (Name == TEXT("WorldLocation"))  { Out = UAPVector(Xf.GetLocation()); return true; }
        if (Name == TEXT("WorldRotation"))  { Out = UAPRotator(Xf.Rotator()); return true; }
        if (Name == TEXT("WorldScale"))     { Out = UAPVector(Xf.GetScale3D()); return true; }
        if (Name == TEXT("ForwardVector"))  { Out = UAPVector(Xf.GetUnitAxis(EAxis::X)); return true; }
        if (Name == TEXT("WorldTransform"))
        {
            TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
            O->SetField(TEXT("location"), UAPVector(Xf.GetLocation()));
            O->SetField(TEXT("rotation"), UAPRotator(Xf.Rotator()));
            O->SetField(TEXT("scale"), UAPVector(Xf.GetScale3D()));
            Out = MakeShared<FJsonValueObject>(O);
            return true;
        }
        if (Name == TEXT("Velocity"))
        {
            const FVector V = Actor ? Actor->GetVelocity() : Comp->GetComponentVelocity();
            Out = UAPVector(V);
            return true;
        }
        return false;
    }

    UObject* UAPResolveObject(const FString& Path, FString& OutError)
    {
        UWorld* World = FAgentWorld::GetActiveGameWorld();

        if (Path.Equals(TEXT("PlayerPawn"), ESearchCase::IgnoreCase)
            || Path.Equals(TEXT("PlayerController"), ESearchCase::IgnoreCase)
            || Path.Equals(TEXT("PlayerCameraManager"), ESearchCase::IgnoreCase))
        {
            if (!World)
            {
                OutError = TEXT("no live game world (start PIE first)");
                return nullptr;
            }
            APlayerController* PC = World->GetFirstPlayerController();
            if (!PC) { OutError = TEXT("no player controller in the live world"); return nullptr; }
            if (Path.Equals(TEXT("PlayerController"), ESearchCase::IgnoreCase)) { return PC; }
            if (Path.Equals(TEXT("PlayerCameraManager"), ESearchCase::IgnoreCase)) { return PC->PlayerCameraManager; }
            return PC->GetPawn();
        }

        if (Path.StartsWith(TEXT("/")))
        {
            if (UObject* Found = StaticFindObject(UObject::StaticClass(), nullptr, *Path))
            {
                return Found;
            }
            OutError = FString::Printf(TEXT("no object at path '%s'"), *Path);
            return nullptr;
        }

        if (!World)
        {
            OutError = TEXT("no live game world (start PIE first)");
            return nullptr;
        }
        AActor* Substring = nullptr;
        for (TActorIterator<AActor> It(World); It; ++It)
        {
            const FString Name = It->GetName();
            if (Name.Equals(Path, ESearchCase::IgnoreCase)) { return *It; }
            if (!Substring && Name.Contains(Path)) { Substring = *It; }
        }
        if (Substring) { return Substring; }
        OutError = FString::Printf(TEXT("no actor named/containing '%s' in the live world"), *Path);
        return nullptr;
    }

    /** Walk a dot-separated property path from Root and read the leaf as JSON. */
    bool UAPReadPath(UObject* Root, const FString& Path, TSharedPtr<FJsonValue>& Out, FString& OutError)
    {
        if (!Root) { OutError = TEXT("object no longer valid"); return false; }

        TArray<FString> Segments;
        Path.ParseIntoArray(Segments, TEXT("."), /*CullEmpty*/ true);
        if (Segments.Num() == 0) { OutError = TEXT("empty property path"); return false; }

        UObject* CurObj = Root;
        void* StructMem = nullptr;
        UStruct* StructType = nullptr;

        for (int32 i = 0; i < Segments.Num(); ++i)
        {
            const FString& Seg = Segments[i];
            const bool bLast = (i == Segments.Num() - 1);

            UStruct* Owner = CurObj ? (UStruct*)CurObj->GetClass() : StructType;
            void* Container = CurObj ? (void*)CurObj : StructMem;
            if (!Owner || !Container) { OutError = TEXT("property path walked off the end"); return false; }

            if (bLast && CurObj && UAPComputedValue(CurObj, Seg, Out)) { return true; }

            FProperty* Prop = Owner->FindPropertyByName(FName(*Seg));
            if (!Prop && CurObj)
            {
                // Allow a component name as a step: "CameraComponent.RelativeLocation".
                if (AActor* Actor = Cast<AActor>(CurObj))
                {
                    for (UActorComponent* C : Actor->GetComponents())
                    {
                        if (C && C->GetName().Equals(Seg, ESearchCase::IgnoreCase))
                        {
                            CurObj = C;
                            StructMem = nullptr;
                            StructType = nullptr;
                            Prop = nullptr;
                            break;
                        }
                    }
                    if (CurObj != Actor)
                    {
                        if (bLast)
                        {
                            OutError = FString::Printf(
                                TEXT("'%s' is a component, not a value -- append a property "
                                     "(e.g. .WorldLocation)"), *Seg);
                            return false;
                        }
                        continue;
                    }
                }
            }
            if (!Prop)
            {
                OutError = FString::Printf(TEXT("no property '%s' on %s"), *Seg, *Owner->GetName());
                return false;
            }

            void* ValuePtr = Prop->ContainerPtrToValuePtr<void>(Container);
            if (bLast)
            {
                Out = FJsonObjectConverter::UPropertyToJsonValue(Prop, ValuePtr);
                if (!Out.IsValid())
                {
                    OutError = FString::Printf(TEXT("property '%s' is not JSON-serializable"), *Seg);
                    return false;
                }
                return true;
            }

            if (FObjectPropertyBase* ObjProp = CastField<FObjectPropertyBase>(Prop))
            {
                CurObj = ObjProp->GetObjectPropertyValue(ValuePtr);
                StructMem = nullptr;
                StructType = nullptr;
                if (!CurObj)
                {
                    OutError = FString::Printf(TEXT("'%s' is null"), *Seg);
                    return false;
                }
                continue;
            }
            if (FStructProperty* StructProp = CastField<FStructProperty>(Prop))
            {
                CurObj = nullptr;
                StructMem = ValuePtr;
                StructType = StructProp->Struct;
                continue;
            }
            OutError = FString::Printf(TEXT("'%s' is not an object or struct; cannot descend"), *Seg);
            return false;
        }
        OutError = TEXT("property path did not resolve to a value");
        return false;
    }

    bool UAPSampleTick(float /*DeltaTime*/)
    {
        const double Now = FPlatformTime::Seconds();
        bool bAnyActive = false;
        for (TPair<FString, FUAPSeries>& Pair : GSeries)
        {
            FUAPSeries& Series = Pair.Value;
            if (!Series.bActive) { continue; }
            TSharedPtr<FJsonValue> Value;
            FString Error;
            if (UAPReadPath(Series.Object.Get(), Series.PropertyPath, Value, Error))
            {
                Series.Times.Add(Now - Series.StartRealTime);
                Series.Values.Add(Value);
            }
            else
            {
                Series.LastError = Error;
            }

            if (Now >= Series.EndRealTime || Series.Values.Num() >= Series.MaxSamples)
            {
                Series.bActive = false;
            }
            else
            {
                bAnyActive = true;
            }
        }
        if (!bAnyActive)
        {
            GSampleTicker.Reset();
            return false;
        }
        return true;
    }

    FString UAPNameLabel(const FString& SeriesName)
    {
        return SeriesName.IsEmpty() ? TEXT("(default)") : SeriesName;
    }
}

FString FAgentSampler::Start(const FString& ObjectPath, const FString& PropertyPath,
                             float Seconds, int32 MaxSamples, const FString& SeriesName)
{
    if (Seconds <= 0.f) { return UAPError(TEXT("Seconds must be > 0")); }
    // Bounded on purpose: this holds every sample in memory and is meant for short windows.
    const float ClampedSeconds = FMath::Min(Seconds, 60.f);
    const int32 ClampedMax = FMath::Clamp(MaxSamples <= 0 ? 5000 : MaxSamples, 1, 20000);

    FString Error;
    UObject* Obj = UAPResolveObject(ObjectPath, Error);
    if (!Obj) { return UAPError(Error); }

    TSharedPtr<FJsonValue> Probe;
    if (!UAPReadPath(Obj, PropertyPath, Probe, Error)) { return UAPError(Error); }

    // Only a series of the SAME name is replaced; any other named series keeps recording.
    const bool bReplaced = GSeries.Contains(SeriesName);
    if (!bReplaced && GSeries.Num() >= GMaxSeries)
    {
        return UAPError(FString::Printf(
            TEXT("%d series already held; stop one (`sample stop --name X`) or stop all (`--all`) first"),
            GMaxSeries));
    }

    FUAPSeries& Series = GSeries.FindOrAdd(SeriesName);
    Series = FUAPSeries();
    Series.Object = Obj;
    Series.ObjectLabel = Obj->GetName();
    Series.PropertyPath = PropertyPath;
    Series.StartRealTime = FPlatformTime::Seconds();
    Series.EndRealTime = Series.StartRealTime + ClampedSeconds;
    Series.MaxSamples = ClampedMax;
    Series.bActive = true;

    if (!GSampleTicker.IsValid())
    {
        GSampleTicker = FTSTicker::GetCoreTicker().AddTicker(
            FTickerDelegate::CreateStatic(&UAPSampleTick), 0.f);
    }

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("name"), SeriesName);
    O->SetBoolField(TEXT("replaced"), bReplaced);
    O->SetStringField(TEXT("object"), Series.ObjectLabel);
    O->SetStringField(TEXT("property"), Series.PropertyPath);
    O->SetNumberField(TEXT("seconds"), ClampedSeconds);
    O->SetNumberField(TEXT("max_samples"), ClampedMax);
    O->SetNumberField(TEXT("series_held"), GSeries.Num());
    return UAPToJsonString(O);
}

FString FAgentSampler::Read(const FString& SeriesName)
{
    const FUAPSeries* Found = GSeries.Find(SeriesName);
    if (!Found)
    {
        TArray<FString> Names;
        GSeries.GetKeys(Names);
        Names.Sort();
        TSharedRef<FJsonObject> E = MakeShared<FJsonObject>();
        E->SetBoolField(TEXT("ok"), false);
        E->SetStringField(TEXT("error"), FString::Printf(
            TEXT("no sample series named '%s' (never started, or the editor restarted)"),
            *UAPNameLabel(SeriesName)));
        TArray<TSharedPtr<FJsonValue>> NameArr;
        for (const FString& N : Names) { NameArr.Add(MakeShared<FJsonValueString>(N)); }
        E->SetArrayField(TEXT("series"), NameArr);
        return UAPToJsonString(E);
    }
    const FUAPSeries& Series = *Found;

    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetBoolField(TEXT("ok"), true);
    O->SetStringField(TEXT("name"), SeriesName);
    O->SetBoolField(TEXT("active"), Series.bActive);
    O->SetStringField(TEXT("object"), Series.ObjectLabel);
    O->SetStringField(TEXT("property"), Series.PropertyPath);
    O->SetNumberField(TEXT("count"), Series.Values.Num());
    if (!Series.LastError.IsEmpty()) { O->SetStringField(TEXT("last_error"), Series.LastError); }

    TArray<TSharedPtr<FJsonValue>> Arr;
    Arr.Reserve(Series.Values.Num());
    for (int32 i = 0; i < Series.Values.Num(); ++i)
    {
        TSharedRef<FJsonObject> S = MakeShared<FJsonObject>();
        S->SetNumberField(TEXT("t"), Series.Times[i]);
        S->SetField(TEXT("v"), Series.Values[i]);
        Arr.Add(MakeShared<FJsonValueObject>(S));
    }
    O->SetArrayField(TEXT("samples"), Arr);
    return UAPToJsonString(O);
}

void FAgentSampler::Stop(const FString& SeriesName)
{
    // "*" stops every series, keeping what each collected. Any other name stops just that one.
    if (SeriesName == TEXT("*"))
    {
        for (TPair<FString, FUAPSeries>& Pair : GSeries) { Pair.Value.bActive = false; }
    }
    else if (FUAPSeries* Found = GSeries.Find(SeriesName))
    {
        Found->bActive = false;
    }
    // The ticker retires itself on its next tick once nothing is active.
}

void FAgentSampler::Shutdown()
{
    if (GSampleTicker.IsValid())
    {
        FTSTicker::RemoveTicker(GSampleTicker);
        GSampleTicker.Reset();
    }
    GSeries.Empty();
}
