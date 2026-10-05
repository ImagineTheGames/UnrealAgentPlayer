#include "AgentActivity.h"
#include "HAL/PlatformTime.h"

FCriticalSection FUAPActivity::Lock;
int32   FUAPActivity::Depth = 0;
FString FUAPActivity::CurrentVerb;
FString FUAPActivity::CurrentDetail;
double  FUAPActivity::CurrentStart = 0.0;
FString FUAPActivity::LastVerb;
double  FUAPActivity::LastDuration = 0.0;
double  FUAPActivity::LastEnd = 0.0;

void FUAPActivity::Begin(const FString& Verb, const FString& Detail)
{
    FScopeLock Guard(&Lock);
    if (Depth++ == 0)
    {
        CurrentVerb = Verb;
        CurrentDetail = Detail;
        CurrentStart = FPlatformTime::Seconds();
    }
}

void FUAPActivity::End()
{
    FScopeLock Guard(&Lock);
    if (Depth > 0 && --Depth == 0)
    {
        const double Now = FPlatformTime::Seconds();
        LastVerb = CurrentDetail.IsEmpty() ? CurrentVerb : (CurrentVerb + TEXT(" ") + CurrentDetail);
        LastDuration = Now - CurrentStart;
        LastEnd = Now;
        CurrentVerb.Reset();
        CurrentDetail.Reset();
        CurrentStart = 0.0;
    }
}

FUAPActivitySnapshot FUAPActivity::Read()
{
    FScopeLock Guard(&Lock);
    const double Now = FPlatformTime::Seconds();

    FUAPActivitySnapshot Out;
    Out.bActive = Depth > 0;
    Out.Verb = CurrentVerb;
    Out.Detail = CurrentDetail;
    Out.ElapsedSeconds = Out.bActive ? (Now - CurrentStart) : 0.0;
    Out.LastVerb = LastVerb;
    Out.LastDurationSeconds = LastDuration;
    Out.LastEndedSecondsAgo = LastEnd > 0.0 ? (Now - LastEnd) : 0.0;
    return Out;
}
