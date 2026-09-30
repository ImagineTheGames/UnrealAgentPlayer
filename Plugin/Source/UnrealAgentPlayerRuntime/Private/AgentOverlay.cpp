#include "AgentOverlay.h"

#include "AgentActivity.h"
#include "AgentInput.h"
#include "CanvasItem.h"
#include "CanvasTypes.h"
#include "Debug/DebugDrawService.h"
#include "Dom/JsonObject.h"
#include "Engine/Canvas.h"
#include "Engine/Engine.h"
#include "Engine/Font.h"
#include "Engine/GameViewportClient.h"
#include "Engine/World.h"
#include "Fonts/FontMeasure.h"
#include "Framework/Application/SlateApplication.h"
#include "HAL/IConsoleManager.h"
#include "HAL/PlatformProcess.h"
#include "HAL/PlatformTime.h"
#include "Misc/App.h"
#include "Misc/DateTime.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Rendering/DrawElements.h"
#include "SceneInterface.h"
#include "SceneView.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Styling/CoreStyle.h"
#include "Widgets/SLeafWidget.h"

// --- CVars --------------------------------------------------------------------------------
// Default ON: the whole point is that an agent-driven editor announces itself without anyone
// having to know a command. 0 UNREGISTERS the draw callback (see ApplyEnabled), so a disabled
// overlay is not merely early-outing per frame -- it is not called at all.
static TAutoConsoleVariable<int32> CVarUAPOverlay(
    TEXT("uap.Overlay"), 1,
    TEXT("Draw the uap activity overlay in the editor viewport and in PIE. 0 unregisters it."),
    ECVF_Default);

// Reinaldo picked top right, 2026-09-30. Kept as a CVar because it is a one-line change of mind.
static TAutoConsoleVariable<int32> CVarUAPOverlayCorner(
    TEXT("uap.Overlay.Corner"), 1,
    TEXT("uap overlay corner: 0 = top-left, 1 = top-right."),
    ECVF_Default);

// The overlay lands in every `uap screenshot` by his decision ("top right corner, show it in
// screenshots"), so it has to be readable in the HTML report, not just on a 4K monitor.
static TAutoConsoleVariable<float> CVarUAPOverlayScale(
    TEXT("uap.Overlay.Scale"), 1.25f,
    TEXT("Text scale for the uap activity overlay."),
    ECVF_Default);

// A verb that has been running this long is almost always BLOCKED, not working, so it is
// coloured like an alarm rather than like progress. Reinaldo's note off the first live look:
// "RUN pie 147s had been running a long time... a long-running verb is usually a block."
static TAutoConsoleVariable<float> CVarUAPOverlayLongVerb(
    TEXT("uap.Overlay.LongVerbSeconds"), 30.0f,
    TEXT("Seconds after which the uap overlay's running-verb line turns red."),
    ECVF_Default);

namespace
{
    FDelegateHandle GDrawHandle;
    FDelegateHandle GViewportCreatedHandle;

    // --- Colours ---------------------------------------------------------------------------
    const FLinearColor CTitle(0.55f, 0.85f, 1.0f, 1.0f);   // pale blue
    const FLinearColor CRun(1.0f, 0.85f, 0.25f, 1.0f);     // amber -- a verb is executing
    const FLinearColor CIdle(0.70f, 0.70f, 0.70f, 1.0f);   // grey
    const FLinearColor CInfo(0.80f, 0.90f, 0.80f, 1.0f);
    const FLinearColor CAlarm(1.0f, 0.30f, 0.25f, 1.0f);   // held key / throttled fps / long verb
    const FLinearColor CGood(0.45f, 1.0f, 0.55f, 1.0f);

    struct FLine
    {
        FString Text;
        FLinearColor Color;
    };

    // --- Coordination state, read off disk ---------------------------------------------------
    // The lease, the machine-wide foreground lock and the report slot are FILES the shared uap
    // CLI already writes (~/.uap-reports/.leases/*.json, ~/.uap-reports/.active). Reading them
    // is how the overlay can name the agent, the lease holder and the open report WITHOUT any
    // change to the CLI, which is shared across projects and is not ours to change.
    struct FCoordSnapshot
    {
        FString LeaseAgent;
        FString LeaseReason;
        FString Waiters;
        FString CliVerb;        // machine-lock reason, e.g. "input:hold" -- the CLI-side verb
        FString CliAgent;
        int64   CliSinceUnix = 0;
        FString ReportName;
    };

    FCoordSnapshot GCoord;
    double GCoordPolledAt = 0.0;
    float  GFpsEMA = 0.0f;

    FString ReportsBase()
    {
        const FString Env = FPlatformMisc::GetEnvironmentVariable(TEXT("UAP_REPORTS_DIR"));
        if (!Env.IsEmpty())
        {
            return Env;
        }
        FString Home = FPlatformMisc::GetEnvironmentVariable(TEXT("USERPROFILE"));
        if (Home.IsEmpty())
        {
            Home = FPlatformProcess::UserHomeDir();
        }
        return FPaths::Combine(Home, TEXT(".uap-reports"));
    }

    /** Parses a json file; returns null on a missing file OR a half-written one (the CLI writes
        these under its own lockfile, so a torn read is possible and must not clobber the last
        good snapshot). */
    TSharedPtr<FJsonObject> LoadJson(const FString& Path)
    {
        FString Raw;
        if (!FFileHelper::LoadFileToString(Raw, *Path))
        {
            return nullptr;
        }
        TSharedPtr<FJsonObject> Obj;
        TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(Raw);
        if (!FJsonSerializer::Deserialize(Reader, Obj))
        {
            return nullptr;
        }
        return Obj;
    }

    void PollCoordination()
    {
        const double Now = FPlatformTime::Seconds();
        if (Now - GCoordPolledAt < 1.0)
        {
            return;     // 1 Hz: three small local files, off the per-frame path entirely.
        }
        GCoordPolledAt = Now;

        const FString Base = ReportsBase();
        const FString Leases = FPaths::Combine(Base, TEXT(".leases"));
        // The CLI keys the lease file on the lowercased project name (coordination.py
        // _safe_project), which is what FApp::GetProjectName() gives us.
        const FString ProjKey = FString(FApp::GetProjectName()).ToLower();

        FCoordSnapshot Next;

        if (TSharedPtr<FJsonObject> Lease = LoadJson(FPaths::Combine(Leases, ProjKey + TEXT(".json"))))
        {
            const TSharedPtr<FJsonObject>* Excl = nullptr;
            if (Lease->TryGetObjectField(TEXT("exclusive"), Excl) && Excl && Excl->IsValid())
            {
                (*Excl)->TryGetStringField(TEXT("agent"), Next.LeaseAgent);
                (*Excl)->TryGetStringField(TEXT("reason"), Next.LeaseReason);
            }
            const TArray<TSharedPtr<FJsonValue>>* Waiters = nullptr;
            if (Lease->TryGetArrayField(TEXT("waiters"), Waiters) && Waiters)
            {
                TArray<FString> Names;
                for (const TSharedPtr<FJsonValue>& W : *Waiters)
                {
                    const TSharedPtr<FJsonObject>* WO = nullptr;
                    FString Name;
                    if (W.IsValid() && W->TryGetObject(WO) && WO && (*WO)->TryGetStringField(TEXT("agent"), Name))
                    {
                        Names.Add(Name);
                    }
                }
                Next.Waiters = FString::Join(Names, TEXT(", "));
            }
        }

        // The machine-wide foreground lock records the CLI-side verb for the duration of the
        // call (`pie:start`, `input:hold`, `screenshot`, ...). It is the only place the verb
        // NAME exists for a call that has not reached the editor yet -- which is exactly the
        // case that reads as a hung tool.
        if (TSharedPtr<FJsonObject> Machine = LoadJson(FPaths::Combine(Leases, TEXT("_machine.json"))))
        {
            const TSharedPtr<FJsonObject>* Excl = nullptr;
            if (Machine->TryGetObjectField(TEXT("exclusive"), Excl) && Excl && Excl->IsValid())
            {
                FString HolderProject;
                (*Excl)->TryGetStringField(TEXT("project"), HolderProject);
                if (HolderProject.Equals(ProjKey, ESearchCase::IgnoreCase))
                {
                    (*Excl)->TryGetStringField(TEXT("reason"), Next.CliVerb);
                    (*Excl)->TryGetStringField(TEXT("agent"), Next.CliAgent);
                    double Since = 0.0;
                    (*Excl)->TryGetNumberField(TEXT("acquired_at"), Since);
                    Next.CliSinceUnix = (int64)Since;
                }
            }
        }

        if (TSharedPtr<FJsonObject> Active = LoadJson(FPaths::Combine(Base, TEXT(".active"))))
        {
            FString RunDir;
            Active->TryGetStringField(TEXT("run_dir"), RunDir);
            FString Name = FPaths::GetCleanFilename(RunDir.Replace(TEXT("\\"), TEXT("/")));
            // Drop the "20260930-121142__" stamp: the slug is the half that says WHAT is being
            // verified, and it was the half getting truncated off the end.
            int32 Sep = INDEX_NONE;
            if (Name.FindChar(TEXT('_'), Sep) && Name.Mid(Sep, 2) == TEXT("__"))
            {
                Name = Name.Mid(Sep + 2);
            }
            Next.ReportName = Name;
        }

        GCoord = Next;
    }

    FString Trim(const FString& In, int32 Max)
    {
        return In.Len() <= Max ? In : (In.Left(Max - 1) + TEXT("~"));
    }

    void BuildLines(TArray<FLine>& Out)
    {
        PollCoordination();

        // 1. Who this editor is. With two editors on this machine (School's Out VR and Project
        //    Broken Wings) a screenshot of the WRONG one auto-fails a report, so the project
        //    name is on the face of every capture now.
        Out.Add({ FString::Printf(TEXT("UAP  %s"), FApp::GetProjectName()), CTitle });

        // 2. What it is doing.
        const float LongVerb = FMath::Max(1.0f, CVarUAPOverlayLongVerb.GetValueOnAnyThread());
        const FUAPActivitySnapshot Act = FUAPActivity::Read();
        if (Act.bActive)
        {
            const FString Detail = Act.Detail.IsEmpty() ? FString() : (TEXT(" ") + Trim(Act.Detail, 40));
            Out.Add({ FString::Printf(TEXT("RUN  %s%s  %.1fs"), *Act.Verb, *Detail, Act.ElapsedSeconds),
                      Act.ElapsedSeconds >= LongVerb ? CAlarm : CRun });
        }
        else if (!GCoord.CliVerb.IsEmpty())
        {
            // In the CLI, not yet in the editor (or blocked on a lease). Still working.
            const int64 NowUnix = FDateTime::UtcNow().ToUnixTimestamp();
            const int64 Elapsed = GCoord.CliSinceUnix > 0 ? FMath::Max((int64)0, NowUnix - GCoord.CliSinceUnix) : 0;
            Out.Add({ FString::Printf(TEXT("RUN  %s  %llds  (cli)"), *Trim(GCoord.CliVerb, 40), Elapsed),
                      (double)Elapsed >= LongVerb ? CAlarm : CRun });
        }
        else if (!Act.LastVerb.IsEmpty())
        {
            Out.Add({ FString::Printf(TEXT("idle  last: %s  %.2fs, %.0fs ago"),
                                      *Trim(Act.LastVerb, 40), Act.LastDurationSeconds,
                                      Act.LastEndedSecondsAgo), CIdle });
        }
        else
        {
            Out.Add({ TEXT("idle  no verb yet this session"), CIdle });
        }

        // 3. Who is driving.
        const FString Who = !GCoord.CliAgent.IsEmpty() ? GCoord.CliAgent : GCoord.LeaseAgent;
        if (!Who.IsEmpty())
        {
            Out.Add({ FString::Printf(TEXT("agent  %s"), *Trim(Who, 34)), CInfo });
        }

        // 4. Keys still down. A `uap input hold` OUTLIVES ITS CALL by design; two of them
        //    overlapping in silence once cost a whole session (a pawn drifted ~3500 cm off the
        //    plaza). This line is the reason the overlay is worth more than convenience.
        TArray<FAgentHeldInputInfo> Held;
        FAgentInput::GetHeld(Held);
        for (const FAgentHeldInputInfo& H : Held)
        {
            const FString Val = H.bAnalog ? FString::Printf(TEXT("=%.2f"), H.Value) : FString();
            Out.Add({ FString::Printf(TEXT("HELD  %s%s  %.1fs left"),
                                      *Trim(H.Key, 34), *Val, H.RemainingSeconds), CAlarm });
        }

        // 5. Frame rate. An UNFOCUSED editor throttles to exactly 3.0 fps and every timing
        //    measured then is void -- that has produced a finding that was not real four times.
        //    Make it impossible to miss.
        const float Dt = (float)FApp::GetDeltaTime();
        const float Inst = Dt > 1e-6f ? (1.0f / Dt) : 0.0f;
        GFpsEMA = GFpsEMA > 0.0f ? FMath::Lerp(GFpsEMA, Inst, 0.2f) : Inst;
        const bool bThrottled = GFpsEMA > 0.0f && GFpsEMA <= 5.0f;
        Out.Add({ bThrottled
                      ? FString::Printf(TEXT("fps  %.1f   THROTTLED - TIMINGS VOID"), GFpsEMA)
                      : FString::Printf(TEXT("fps  %.1f"), GFpsEMA),
                  bThrottled ? CAlarm : CGood });

        // 6. Lease, so a blocked agent does not read as a hung tool. The WAITING line earned its
        //    prominence on the first live look: it is what said, on screen, why nothing was
        //    moving, and it is drawn in the alarm colour for that reason.
        if (!GCoord.LeaseAgent.IsEmpty())
        {
            Out.Add({ FString::Printf(TEXT("lease  %s (%s)"),
                                      *Trim(GCoord.LeaseAgent, 26), *GCoord.LeaseReason), CInfo });
        }
        if (!GCoord.Waiters.IsEmpty())
        {
            Out.Add({ FString::Printf(TEXT("WAITING  %s"), *Trim(GCoord.Waiters, 34)), CAlarm });
        }

        // 7. The open report slot -- one per machine, so knowing whose it is matters.
        if (!GCoord.ReportName.IsEmpty())
        {
            Out.Add({ FString::Printf(TEXT("report  %s"), *Trim(GCoord.ReportName, 38)), CInfo });
        }
    }

    bool OverlayEnabled()
    {
        return CVarUAPOverlay.GetValueOnAnyThread() != 0;
    }

    bool DrawOnRight()
    {
        return CVarUAPOverlayCorner.GetValueOnAnyThread() != 0;
    }

    float OverlayScale()
    {
        return FMath::Clamp(CVarUAPOverlayScale.GetValueOnAnyThread(), 0.5f, 4.0f);
    }

    // =========================================================================================
    // 1. EDITOR viewport -- UDebugDrawService.
    // =========================================================================================
    void DrawCanvas(UCanvas* Canvas, APlayerController* /*PC*/)
    {
        if (!Canvas || !GEngine || !OverlayEnabled())
        {
            return;
        }
        UFont* Font = GEngine->GetMediumFont();
        if (!Font)
        {
            return;
        }
        // LEVEL editor viewport only. UDebugDrawService fires for every
        // FEditorViewportClient::Draw, which includes the material / static mesh / blueprint
        // editor previews (an EditorPreview world) -- clutter in windows nobody watches for
        // agent activity. The game and PIE viewports are covered by the Slate layer below,
        // which is the one `uap screenshot` can actually see.
        const FSceneView* View = Canvas->SceneView;
        if (!View || !View->Family || !View->Family->Scene)
        {
            return;
        }
        const UWorld* W = View->Family->Scene->GetWorld();
        if (!W || W->WorldType != EWorldType::Editor)
        {
            return;
        }

        TArray<FLine> Lines;
        BuildLines(Lines);
        if (Lines.Num() == 0)
        {
            return;
        }

        const float Scale = OverlayScale();
        const float Pad = 8.0f * Scale;
        const float Margin = 12.0f;

        float MaxW = 0.0f;
        float LineH = 0.0f;
        for (const FLine& L : Lines)
        {
            float TW = 0.0f, TH = 0.0f;
            Canvas->TextSize(Font, L.Text, TW, TH, Scale, Scale);
            MaxW = FMath::Max(MaxW, TW);
            LineH = FMath::Max(LineH, TH);
        }
        const float BoxW = MaxW + Pad * 2.0f;
        const float BoxH = LineH * Lines.Num() + Pad * 2.0f;
        const float BoxX = DrawOnRight() ? FMath::Max(0.0f, Canvas->SizeX - BoxW - Margin) : Margin;
        const float BoxY = Margin;

        // The 3-arg form uses Canvas' own default white texture. The GWhiteTexture form drew
        // nothing here, and a text panel with no backing box is unreadable over bright geometry.
        FCanvasTileItem Tile(FVector2D(BoxX, BoxY), FVector2D(BoxW, BoxH),
                             FLinearColor(0.0f, 0.0f, 0.0f, 0.65f));
        Tile.BlendMode = SE_BLEND_Translucent;
        Canvas->DrawItem(Tile);

        float Y = BoxY + Pad;
        for (const FLine& L : Lines)
        {
            FCanvasTextItem Item(FVector2D(BoxX + Pad, Y), FText::FromString(L.Text), Font, L.Color);
            Item.Scale = FVector2D(Scale, Scale);
            Item.EnableShadow(FLinearColor::Black);
            Canvas->DrawItem(Item);
            Y += LineH;
        }
    }

    // =========================================================================================
    // 2. GAME / PIE viewport -- a Slate layer.
    //
    // WHY THIS EXISTS AS WELL: `uap screenshot` composites through
    // FSlateApplication::TakeScreenshot on the game viewport WIDGET, and the engine's debug
    // canvas is NOT in what that reads back -- measured, the overlay was plainly on screen in an
    // OS capture of the PIE window and absent from the `uap screenshot` PNG of the same session.
    // Reinaldo asked for the overlay to appear in screenshots, so the PIE half is drawn as
    // Slate, which TakeScreenshot does capture.
    // =========================================================================================
    class SUAPActivityOverlay : public SLeafWidget
    {
    public:
        SLATE_BEGIN_ARGS(SUAPActivityOverlay) {}
        SLATE_END_ARGS()

        void Construct(const FArguments&)
        {
            SetVisibility(EVisibility::HitTestInvisible);
        }

        virtual FVector2D ComputeDesiredSize(float) const override
        {
            return FVector2D(1.0f, 1.0f);
        }

        virtual int32 OnPaint(const FPaintArgs&, const FGeometry& Geo, const FSlateRect& Clip,
                              FSlateWindowElementList& Out, int32 LayerId,
                              const FWidgetStyle&, bool) const override
        {
            if (!OverlayEnabled() || !FSlateApplication::IsInitialized())
            {
                return LayerId;
            }

            TArray<FLine> Lines;
            BuildLines(Lines);
            if (Lines.Num() == 0)
            {
                return LayerId;
            }

            const float Scale = OverlayScale();
            const FSlateFontInfo Font = FCoreStyle::GetDefaultFontStyle(TEXT("Mono"), 11.0f * Scale);
            const TSharedRef<FSlateFontMeasure> Measure =
                FSlateApplication::Get().GetRenderer()->GetFontMeasureService();

            float MaxW = 0.0f;
            float LineH = 0.0f;
            for (const FLine& L : Lines)
            {
                const FVector2D S = Measure->Measure(L.Text, Font);
                MaxW = FMath::Max(MaxW, (float)S.X);
                LineH = FMath::Max(LineH, (float)S.Y);
            }

            const float Pad = 8.0f * Scale;
            const float Margin = 12.0f;
            const FVector2f Local = FVector2f(Geo.GetLocalSize());
            const float BoxW = MaxW + Pad * 2.0f;
            const float BoxH = LineH * Lines.Num() + Pad * 2.0f;
            const float BoxX = DrawOnRight() ? FMath::Max(0.0f, Local.X - BoxW - Margin) : Margin;
            const float BoxY = Margin;

            FSlateDrawElement::MakeBox(
                Out, LayerId,
                Geo.ToPaintGeometry(FVector2f(BoxW, BoxH), FSlateLayoutTransform(FVector2f(BoxX, BoxY))),
                FCoreStyle::Get().GetBrush(TEXT("WhiteBrush")),
                ESlateDrawEffect::None, FLinearColor(0.0f, 0.0f, 0.0f, 0.65f));

            float Y = BoxY + Pad;
            for (const FLine& L : Lines)
            {
                FSlateDrawElement::MakeText(
                    Out, LayerId + 1,
                    Geo.ToPaintGeometry(FVector2f(MaxW, LineH),
                                        FSlateLayoutTransform(FVector2f(BoxX + Pad, Y))),
                    L.Text, Font, ESlateDrawEffect::None, L.Color);
                Y += LineH;
            }
            return LayerId + 2;
        }
    };

    void OnGameViewportCreated()
    {
        if (GEngine && GEngine->GameViewport)
        {
            // ZOrder above gameplay UMG so it is never hidden by the thing under test, and
            // hit-test invisible so it cannot eat a click an agent is trying to land.
            GEngine->GameViewport->AddViewportWidgetContent(SNew(SUAPActivityOverlay), 10000);
        }
    }

    void ApplyEnabled()
    {
        const bool bWant = CVarUAPOverlay.GetValueOnGameThread() != 0;
        if (bWant && !GDrawHandle.IsValid())
        {
            // "Rendering" rather than "Game": every show flag defaults ON (FEngineShowFlags::Init
            // memsets them true) and Rendering is never cleared, so it is true in the LEVEL EDITOR
            // viewport as well as the game one. "Game" is explicitly false in an editor viewport
            // (ShowFlags.h: SetGame(InitMode != ESFIM_Editor...)), which would have left the
            // overlay invisible in exactly the place he watches it.
            GDrawHandle = UDebugDrawService::Register(
                TEXT("Rendering"), FDebugDrawDelegate::CreateStatic(&DrawCanvas));
        }
        else if (!bWant && GDrawHandle.IsValid())
        {
            UDebugDrawService::Unregister(GDrawHandle);
            GDrawHandle.Reset();
        }
    }
}

void FUAPOverlay::Register()
{
    CVarUAPOverlay.AsVariable()->SetOnChangedCallback(
        FConsoleVariableDelegate::CreateLambda([](IConsoleVariable*) { ApplyEnabled(); }));
    ApplyEnabled();

    // The Slate half attaches to each game viewport as it is created (PIE start, or a -game
    // client). The widget itself checks `uap.Overlay` every paint, so toggling the CVar off
    // costs one bool read rather than needing the widget torn down.
    GViewportCreatedHandle =
        UGameViewportClient::OnViewportCreated().AddStatic(&OnGameViewportCreated);
    OnGameViewportCreated();    // covers a viewport that already exists at module start
}

void FUAPOverlay::Unregister()
{
    if (GDrawHandle.IsValid())
    {
        UDebugDrawService::Unregister(GDrawHandle);
        GDrawHandle.Reset();
    }
    if (GViewportCreatedHandle.IsValid())
    {
        UGameViewportClient::OnViewportCreated().Remove(GViewportCreatedHandle);
        GViewportCreatedHandle.Reset();
    }
}
