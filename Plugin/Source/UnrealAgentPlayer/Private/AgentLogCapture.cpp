#include "AgentLogCapture.h"

#include "HAL/PlatformTime.h"

FAgentLogCapture::FAgentLogCapture(int32 InCapacity)
    : Capacity(FMath::Max(256, InCapacity))
{
    Buffer.SetNum(Capacity);
}

FAgentLogCapture::~FAgentLogCapture() = default;

void FAgentLogCapture::Serialize(const TCHAR* V, ELogVerbosity::Type Verbosity, const FName& Category)
{
    FScopeLock Lock(&BufferMutex);
    FAgentLogEntry& Slot = Buffer[Head];
    Slot.Cursor = NextCursor++;
    Slot.TimestampSeconds = FPlatformTime::Seconds();
    Slot.Category = Category;
    Slot.Verbosity = Verbosity;
    Slot.Message = V;
    Head = (Head + 1) % Capacity;
    if (Size < Capacity) { ++Size; }
}

int64 FAgentLogCapture::GetCursor() const
{
    FScopeLock Lock(&BufferMutex);
    return NextCursor - 1;
}

static bool PassesFilters(const FAgentLogEntry& E, FName CategoryFilter, EAgentLogVerbosity MinVerbosity)
{
    if (!CategoryFilter.IsNone() && E.Category != CategoryFilter) { return false; }
    return static_cast<uint8>(E.Verbosity) <= static_cast<uint8>(MinVerbosity);
}

void FAgentLogCapture::ReadSince(
    int64 AfterCursor, int32 MaxLines, FName CategoryFilter,
    EAgentLogVerbosity MinVerbosity,
    TArray<FAgentLogEntry>& OutEntries, int64& OutCursor) const
{
    FScopeLock Lock(&BufferMutex);
    OutEntries.Reserve(FMath::Min(MaxLines, Size));
    int32 Start = (Head - Size + Capacity) % Capacity;
    int32 Idx = Start;
    for (int32 i = 0; i < Size && OutEntries.Num() < MaxLines; ++i)
    {
        const FAgentLogEntry& E = Buffer[Idx];
        if (E.Cursor > AfterCursor && PassesFilters(E, CategoryFilter, MinVerbosity))
        {
            OutEntries.Add(E);
        }
        Idx = (Idx + 1) % Capacity;
    }
    OutCursor = OutEntries.Num() ? OutEntries.Last().Cursor : AfterCursor;
}
