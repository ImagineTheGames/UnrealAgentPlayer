#pragma once

#include "CoreMinimal.h"

/**
 * What uap is doing RIGHT NOW, for the in-viewport overlay (ClickUp 17tm466gqfj).
 *
 * Watching an agent drive the editor is indistinguishable from watching nothing happen:
 * every verb arrives over RemoteControl / Python remote-exec and leaves no trace on screen.
 * The overlay fixes that, and this is the record it reads.
 *
 * A verb stamps itself on ENTRY and clears on EXIT via FUAPActivityScope. Begin/End nest
 * (CallTestHelper can call back into another verb), so only the OUTERMOST scope owns the
 * record -- otherwise an inner call would clear the outer verb while it is still running.
 *
 * The last FINISHED verb is kept too, with how long it took and when it ended. That is the
 * half that makes the overlay useful to watch: most verbs complete in well under a frame, so
 * an overlay that only showed live work would be blank almost always -- which is the exact
 * complaint this exists to answer.
 */
struct UNREALAGENTPLAYERRUNTIME_API FUAPActivitySnapshot
{
    /** True while a verb is executing in-engine. */
    bool    bActive = false;
    FString Verb;
    FString Detail;
    /** Seconds the live verb has been running. Zero when idle. */
    double  ElapsedSeconds = 0.0;

    /** The verb that finished most recently -- empty if none has run this session. */
    FString LastVerb;
    double  LastDurationSeconds = 0.0;
    double  LastEndedSecondsAgo = 0.0;
};

class UNREALAGENTPLAYERRUNTIME_API FUAPActivity
{
public:
    /** Enter a verb. Nested calls are counted, not stacked. */
    static void Begin(const FString& Verb, const FString& Detail);

    /** Leave a verb. The outermost End moves the record to "last finished". */
    static void End();

    static FUAPActivitySnapshot Read();

private:
    static FCriticalSection Lock;
    static int32   Depth;
    static FString CurrentVerb;
    static FString CurrentDetail;
    static double  CurrentStart;
    static FString LastVerb;
    static double  LastDuration;
    static double  LastEnd;
};

/** RAII stamp. Use the UAP_ACTIVITY macro rather than constructing this directly. */
class UNREALAGENTPLAYERRUNTIME_API FUAPActivityScope
{
public:
    FUAPActivityScope(const FString& Verb, const FString& Detail)
    {
        FUAPActivity::Begin(Verb, Detail);
    }
    ~FUAPActivityScope()
    {
        FUAPActivity::End();
    }
};

/**
 * One line at the top of a verb body. Detail is a SHORT argument summary -- it is drawn on
 * screen next to the verb name, so keep it to the argument that identifies the call.
 */
#define UAP_ACTIVITY(InVerb, InDetail) FUAPActivityScope UAPActivityScope_((InVerb), (InDetail))
