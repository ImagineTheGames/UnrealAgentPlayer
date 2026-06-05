#pragma once

#include "CoreMinimal.h"
#include "InputCoreTypes.h"
#include "UAPAgentTypes.h"

class SWidget;

class FAgentInput
{
public:
    static bool InjectKey(FKey Key, bool bPressed, bool bRepeat);
    static bool InjectMouseMove(FVector2D Delta, bool bAbsolute);
    static bool InjectMouseButton(EAgentMouseButton Btn, bool bPressed);
    static bool InjectAxis(FName AxisName, float Value);
    static bool InjectGamepad(EAgentGamepadButton Btn, bool bPressed, float AnalogValue);

private:
    static TSharedPtr<SWidget> FindPIEViewportWidget();
    static FKey GamepadButtonToKey(EAgentGamepadButton Btn);
    static FKey MouseButtonToKey(EAgentMouseButton Btn);
};
