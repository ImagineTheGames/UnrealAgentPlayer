#include "AgentHelperDiscovery.h"
#include "AgentSchemaWalker.h"

#include "UObject/UnrealType.h"
#include "UObject/Class.h"
#include "UObject/UObjectIterator.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

bool FAgentHelperDiscovery::IsHelperFunction(const UFunction* Func)
{
    if (!Func) { return false; }
#if WITH_EDITORONLY_DATA
    return Func->HasMetaData(TEXT("AgentTestHelper"));
#else
    return false;
#endif
}

static FString SerializeJson(TSharedPtr<FJsonObject> Obj)
{
    if (!Obj.IsValid()) { return TEXT("null"); }
    FString Out;
    TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
    FJsonSerializer::Serialize(Obj.ToSharedRef(), W);
    return Out;
}

void FAgentHelperDiscovery::RescanAll(TArray<FAgentHelperDescriptor>& OutList)
{
    OutList.Reset();
    for (TObjectIterator<UClass> It; It; ++It)
    {
        UClass* Cls = *It;
        if (Cls->HasAnyClassFlags(CLASS_Abstract | CLASS_Deprecated)) { continue; }
        for (TFieldIterator<UFunction> FIt(Cls, EFieldIteratorFlags::ExcludeSuper); FIt; ++FIt)
        {
            UFunction* F = *FIt;
            if (!IsHelperFunction(F)) { continue; }

            FAgentHelperDescriptor D;
            D.Name = FString::Printf(TEXT("%s::%s"), *Cls->GetName(), *F->GetName());
#if WITH_EDITORONLY_DATA
            D.Category         = F->GetMetaData(TEXT("Category"));
            D.Tooltip          = F->GetMetaData(TEXT("ToolTip"));
            D.PhaseRequirement = F->GetMetaData(TEXT("Phase"));
            if (D.PhaseRequirement.IsEmpty()) { D.PhaseRequirement = TEXT("Any"); }
#endif
            bool bArgsOK = true, bRetOK = true;
            FString ArgReason, RetReason;
            auto ArgSchema = UAP::Schema::BuildArgSchema(F, bArgsOK, ArgReason);
            auto RetSchema = UAP::Schema::BuildReturnSchema(F, bRetOK, RetReason);
            D.bSupported = bArgsOK && bRetOK;
            D.UnsupportedReason = !bArgsOK ? ArgReason : (!bRetOK ? RetReason : TEXT(""));
            D.ArgSchemaJson    = SerializeJson(ArgSchema);
            D.ReturnSchemaJson = SerializeJson(RetSchema);
            OutList.Add(D);
        }
    }
}

static TSharedPtr<FJsonValue> ParseJsonValue(const FString& Text)
{
    // ArgSchemaJson/ReturnSchemaJson are already JSON documents; re-embed them as real JSON
    // rather than as escaped strings, so an agent can read the schema without double-decoding.
    if (Text.IsEmpty() || Text == TEXT("null")) { return MakeShared<FJsonValueNull>(); }
    TSharedPtr<FJsonObject> Obj;
    TSharedRef<TJsonReader<>> R = TJsonReaderFactory<>::Create(Text);
    if (FJsonSerializer::Deserialize(R, Obj) && Obj.IsValid())
    {
        return MakeShared<FJsonValueObject>(Obj);
    }
    return MakeShared<FJsonValueString>(Text);
}

FString FAgentHelperDiscovery::ToJson(const TArray<FAgentHelperDescriptor>& List)
{
    TArray<TSharedPtr<FJsonValue>> Arr;
    Arr.Reserve(List.Num());
    for (const FAgentHelperDescriptor& D : List)
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("name"), D.Name);
        O->SetStringField(TEXT("category"), D.Category);
        O->SetStringField(TEXT("tooltip"), D.Tooltip);
        O->SetStringField(TEXT("phase"), D.PhaseRequirement);
        O->SetField(TEXT("arg_schema"), ParseJsonValue(D.ArgSchemaJson));
        O->SetField(TEXT("return_schema"), ParseJsonValue(D.ReturnSchemaJson));
        O->SetBoolField(TEXT("supported"), D.bSupported);
        O->SetStringField(TEXT("unsupported_reason"), D.UnsupportedReason);
        Arr.Add(MakeShared<FJsonValueObject>(O));
    }
    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetArrayField(TEXT("helpers"), Arr);

    FString Out;
    TSharedRef<TJsonWriter<>> W = TJsonWriterFactory<>::Create(&Out);
    FJsonSerializer::Serialize(Root, W);
    return Out;
}

UFunction* FAgentHelperDiscovery::Resolve(const FString& FullName, UClass*& OutClass)
{
    int32 SepIdx;
    if (!FullName.FindChar(TEXT(':'), SepIdx)) { return nullptr; }
    if (SepIdx + 1 >= FullName.Len() || FullName[SepIdx + 1] != TEXT(':')) { return nullptr; }
    FString ClassName = FullName.Left(SepIdx);
    FString FnName    = FullName.Mid(SepIdx + 2);

    for (TObjectIterator<UClass> It; It; ++It)
    {
        if (It->GetName() == ClassName)
        {
            if (UFunction* F = It->FindFunctionByName(*FnName))
            {
                if (IsHelperFunction(F)) { OutClass = *It; return F; }
            }
        }
    }
    OutClass = nullptr;
    return nullptr;
}
