#pragma once

#include "CoreMinimal.h"
#include "Misc/OutputDevice.h"
#include "UAPAgentTypes.h"

struct FAgentLogEntry
{
    int64 Cursor = 0;
    double TimestampSeconds = 0.0;
    FName Category;
    ELogVerbosity::Type Verbosity = ELogVerbosity::Log;
    FString Message;
};

class UNREALAGENTPLAYERRUNTIME_API FAgentLogCapture : public FOutputDevice
{
public:
    FAgentLogCapture(int32 Capacity);
    virtual ~FAgentLogCapture();

    virtual void Serialize(const TCHAR* V, ELogVerbosity::Type Verbosity, const FName& Category) override;
    virtual bool CanBeUsedOnAnyThread() const override { return true; }

    int64 GetCursor() const;

    void ReadSince(int64 AfterCursor, int32 MaxLines, FName CategoryFilter,
                   EAgentLogVerbosity MinVerbosity,
                   TArray<FAgentLogEntry>& OutEntries, int64& OutCursor) const;

private:
    mutable FCriticalSection BufferMutex;
    TArray<FAgentLogEntry> Buffer;
    int32 Capacity = 0;
    int32 Head = 0;
    int32 Size = 0;
    int64 NextCursor = 1;
};
