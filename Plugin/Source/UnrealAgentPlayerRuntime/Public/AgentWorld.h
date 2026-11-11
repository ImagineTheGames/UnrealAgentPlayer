#pragma once
#include "CoreMinimal.h"
#include "UObject/WeakObjectPtr.h"
class UWorld;
class UGameViewportClient;

/**
 * Which game world the agent verbs act on.
 *
 * With several PIE clients in ONE process (PlayNumberOfClients > 1) there are several game
 * worlds, and every verb used to act on the FIRST one -- so a net client could never be driven
 * [17tm466z4a6]. A caller now names the client with a `Client` selector:
 *
 *   ""      the historical default: the first PIE world, else a standalone game world
 *   "N"     the PIE world whose FWorldContext::PIEInstance is N (see `uap input clients`)
 *   "all"   every game world, in turn
 *
 * The selector is resolved by ResolveClients, and the resolved world is made current for the
 * duration of a call by FScope. Everything below GetActiveGameWorld() -- the viewport routes,
 * the click redirect to the active game window, the UI reader -- then works unmodified.
 *
 * In-engine only: nothing here touches an OS window, the OS cursor, keyboard focus or the
 * foreground window, so a client is driven whether or not its window is in front.
 */
class UNREALAGENTPLAYERRUNTIME_API FAgentWorld
{
public:
    /** The override world while an FScope is live, else the first PIE / game world. */
    static UWorld* GetActiveGameWorld();
    static UGameViewportClient* GetActiveGameViewport();

    /** The world an FScope has made current, or nullptr when none is. */
    static UWorld* GetOverrideWorld();

    /** Every game world (PIE contexts first, ordered by PIEInstance, then standalone Game). */
    static void GetAllGameWorlds(TArray<UWorld*>& Out);

    /** PIEInstance of a world's context, or INDEX_NONE when it is not a PIE world. */
    static int32 GetClientIndex(const UWorld* World);

    /**
     * Resolve a Client selector to worlds. Empty selector -> returns true with Out EMPTY, which
     * means "no override: use the default behaviour". "all" -> every game world. "N" -> that
     * PIE instance. Anything unusable returns false with a refusal that names the valid
     * clients, because acting on a different client than the one asked for is the silent wrong
     * answer this exists to end.
     */
    static bool ResolveClients(const FString& Client, TArray<UWorld*>& Out, FString& OutError);

    /** JSON: every client's index, world, net mode, window title and which one is the default. */
    static FString DescribeClientsJson();

    /** "0 (UEDPIE_0_L_DevTest, ListenServer), 1 (UEDPIE_1_L_DevTest, Client)" for refusals. */
    static FString DescribeClientsShort();

    /** Make World the current game world until destroyed. Nests; restores the previous one. */
    struct UNREALAGENTPLAYERRUNTIME_API FScope
    {
        explicit FScope(UWorld* World);
        ~FScope();
        FScope(const FScope&) = delete;
        FScope& operator=(const FScope&) = delete;
    private:
        TWeakObjectPtr<UWorld> Previous;
    };
};
