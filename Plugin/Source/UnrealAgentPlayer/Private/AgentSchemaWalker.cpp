#include "AgentSchemaWalker.h"

#include "UObject/UnrealType.h"
#include "UObject/EnumProperty.h"
#include "UObject/TextProperty.h"

namespace UAP::Schema
{

static TSharedPtr<FJsonObject> SimpleType(const TCHAR* Ty)
{
    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("type"), Ty);
    return O;
}

TSharedPtr<FJsonObject> PropertyToSchema(const FProperty* Prop, bool& bOutSupported, FString& OutReason)
{
    if (!Prop) { bOutSupported = false; OutReason = TEXT("null property"); return nullptr; }

    if (Prop->IsA<FBoolProperty>())    return SimpleType(TEXT("boolean"));
    if (Prop->IsA<FIntProperty>())     return SimpleType(TEXT("integer"));
    if (Prop->IsA<FInt64Property>())   return SimpleType(TEXT("integer"));
    if (Prop->IsA<FFloatProperty>())   return SimpleType(TEXT("number"));
    if (Prop->IsA<FDoubleProperty>())  return SimpleType(TEXT("number"));
    if (Prop->IsA<FStrProperty>())     return SimpleType(TEXT("string"));
    if (Prop->IsA<FNameProperty>())    return SimpleType(TEXT("string"));
    if (Prop->IsA<FTextProperty>())    return SimpleType(TEXT("string"));

    if (const FEnumProperty* EnumProp = CastField<FEnumProperty>(Prop))
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("type"), TEXT("string"));
        TArray<TSharedPtr<FJsonValue>> Values;
        const UEnum* E = EnumProp->GetEnum();
        for (int32 i = 0; i < E->NumEnums() - 1; ++i)
        {
            Values.Add(MakeShared<FJsonValueString>(E->GetNameStringByIndex(i)));
        }
        O->SetArrayField(TEXT("enum"), Values);
        return O;
    }

    if (const FStructProperty* StructProp = CastField<FStructProperty>(Prop))
    {
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("type"), TEXT("object"));
        TSharedRef<FJsonObject> Props = MakeShared<FJsonObject>();
        for (TFieldIterator<FProperty> It(StructProp->Struct); It; ++It)
        {
            bool bSub = true; FString Reason;
            TSharedPtr<FJsonObject> Sub = PropertyToSchema(*It, bSub, Reason);
            if (!bSub) { bOutSupported = false; OutReason = FString::Printf(TEXT("unsupported field %s: %s"), *It->GetName(), *Reason); return nullptr; }
            Props->SetObjectField(It->GetName(), Sub);
        }
        O->SetObjectField(TEXT("properties"), Props);
        return O;
    }

    if (const FArrayProperty* ArrayProp = CastField<FArrayProperty>(Prop))
    {
        bool bSub = true; FString Reason;
        TSharedPtr<FJsonObject> Inner = PropertyToSchema(ArrayProp->Inner, bSub, Reason);
        if (!bSub) { bOutSupported = false; OutReason = Reason; return nullptr; }
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("type"), TEXT("array"));
        O->SetObjectField(TEXT("items"), Inner);
        return O;
    }

    if (const FMapProperty* MapProp = CastField<FMapProperty>(Prop))
    {
        bool bSub = true; FString Reason;
        TSharedPtr<FJsonObject> Inner = PropertyToSchema(MapProp->ValueProp, bSub, Reason);
        if (!bSub) { bOutSupported = false; OutReason = Reason; return nullptr; }
        TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
        O->SetStringField(TEXT("type"), TEXT("object"));
        O->SetObjectField(TEXT("additionalProperties"), Inner);
        return O;
    }

    if (Prop->IsA<FObjectProperty>())  return SimpleType(TEXT("string"));

    bOutSupported = false;
    OutReason = FString::Printf(TEXT("unsupported property type %s"), *Prop->GetClass()->GetName());
    return nullptr;
}

TSharedPtr<FJsonObject> BuildArgSchema(const UFunction* Func, bool& bOutSupported, FString& OutReason)
{
    TSharedRef<FJsonObject> O = MakeShared<FJsonObject>();
    O->SetStringField(TEXT("type"), TEXT("object"));
    TSharedRef<FJsonObject> Props = MakeShared<FJsonObject>();
    TArray<TSharedPtr<FJsonValue>> Required;

    for (TFieldIterator<FProperty> It(Func); It; ++It)
    {
        if (It->HasAnyPropertyFlags(CPF_ReturnParm)) { continue; }
        if (It->HasAnyPropertyFlags(CPF_OutParm) && !It->HasAnyPropertyFlags(CPF_ReferenceParm)) { continue; }
        TSharedPtr<FJsonObject> Sub = PropertyToSchema(*It, bOutSupported, OutReason);
        if (!bOutSupported) { return nullptr; }
        Props->SetObjectField(It->GetName(), Sub);
        Required.Add(MakeShared<FJsonValueString>(It->GetName()));
    }
    O->SetObjectField(TEXT("properties"), Props);
    O->SetArrayField(TEXT("required"), Required);
    O->SetBoolField(TEXT("additionalProperties"), false);
    return O;
}

TSharedPtr<FJsonObject> BuildReturnSchema(const UFunction* Func, bool& bOutSupported, FString& OutReason)
{
    FProperty* Ret = Func->GetReturnProperty();
    if (!Ret) { return nullptr; }
    return PropertyToSchema(Ret, bOutSupported, OutReason);
}

} // namespace UAP::Schema
