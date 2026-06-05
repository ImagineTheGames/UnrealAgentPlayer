#include "USchoolsOutTestHelpers.h"

#include "Engine/World.h"
#include "GameFramework/PlayerController.h"

FVector USchoolsOutTestHelpers::GetPlayerLocation()
{
    if (GWorld)
    {
        if (APlayerController* PC = GWorld->GetFirstPlayerController())
        {
            if (APawn* P = PC->GetPawn())
            {
                return P->GetActorLocation();
            }
        }
    }
    return FVector::ZeroVector;
}

float USchoolsOutTestHelpers::GetPlayerHealth()
{
    return -1.0f;
}

bool USchoolsOutTestHelpers::IsDoorOpen(FName DoorTag)
{
    return false;
}

bool USchoolsOutTestHelpers::HasCompletedTutorial()
{
    return false;
}
