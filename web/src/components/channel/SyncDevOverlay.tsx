import type { SyncState } from "@/hooks/useSync";

interface SyncDevOverlayProps {
  syncState: SyncState;
}

const PIPELINE_STAGES = [
  { key: "preprocessor", label: "Preprocess", icon: "1" },
  { key: "fact_extractor", label: "Facts", icon: "2" },
  { key: "entity_extractor", label: "Entities", icon: "2" },
  { key: "classifier_agent", label: "Classify", icon: "3" },
  { key: "embedder", label: "Embed", icon: "4" },
  { key: "cross_batch_validator_agent", label: "Validate", icon: "5" },
  { key: "persister", label: "Persist", icon: "6" },
];

function getStageStatus(
  stageKey: string,
  timings: Record<string, number>,
  currentStage: string | null | undefined,
): "done" | "active" | "pending" {
  if (timings[stageKey] !== undefined) return "done";
  if (currentStage?.toLowerCase().includes(stageKey.replace("_agent", "")))
    return "active";
  return "pending";
}

function formatTime(seconds: number): string {
  if (seconds < 0.01) return "<0.01s";
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)}ms`;
  return `${seconds.toFixed(1)}s`;
}

export function SyncDevOverlay({ syncState }: SyncDevOverlayProps) {
  const timings = syncState.stage_timings ?? {};
  const details = syncState.stage_details ?? {};
  const currentStage = syncState.current_stage;
  const totalTime = Object.values(timings).reduce((a, b) => a + b, 0);

  return (
    <div className="px-3 sm:px-6 py-3 border-b border-border bg-muted/30 space-y-3">
      {/* Pipeline stage visualization */}
      <div className="flex items-center gap-1 overflow-x-auto pb-1">
        {PIPELINE_STAGES.map((stage, i) => {
          const status = getStageStatus(stage.key, timings, currentStage);
          const time = timings[stage.key];
          return (
            <div key={stage.key} className="flex items-center">
              {i > 0 && (
                <div
                  className={`w-4 h-px mx-0.5 ${status === "pending" ? "bg-border" : "bg-primary/40"}`}
                />
              )}
              <div
                className={`flex flex-col items-center px-2 py-1 rounded-md text-[10px] min-w-[56px] ${
                  status === "done"
                    ? "bg-emerald-100 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300"
                    : status === "active"
                      ? "bg-primary/10 text-primary ring-1 ring-primary/30 animate-pulse"
                      : "bg-muted text-muted-foreground/50"
                }`}
              >
                <span className="font-semibold">{stage.label}</span>
                {time !== undefined && (
                  <span className="opacity-70">{formatTime(time)}</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Timing summary */}
      {totalTime > 0 && (
        <div className="flex items-center gap-4 text-[11px] text-muted-foreground">
          <span>
            Total: <span className="font-medium text-foreground">{formatTime(totalTime)}</span>
          </span>
          {Object.entries(timings).length > 0 && (
            <span>
              Stages: {Object.entries(timings).length}/{PIPELINE_STAGES.length}
            </span>
          )}
        </div>
      )}

      {/* Stage details */}
      {Object.keys(details).length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-[11px]">
          {Object.entries(details).map(([stage, data]) => (
            <div
              key={stage}
              className="rounded-md bg-background/60 px-2 py-1.5 border border-border/50"
            >
              <div className="font-medium text-foreground/80 mb-0.5">
                {stage.replace("_agent", "").replace("_", " ")}
              </div>
              {Object.entries(data as Record<string, unknown>).map(
                ([key, val]) => (
                  <div key={key} className="text-muted-foreground">
                    {key}: <span className="text-foreground/70">{String(val)}</span>
                  </div>
                ),
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
