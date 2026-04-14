import { useState } from "react";
import { cn } from "@/lib/utils";
import type { ActivityEntry, ActivitySample } from "@/lib/types";

export function ExpandableText({ text, className }: { text: string; className?: string }) {
  const [expanded, setExpanded] = useState(false);
  const isLong = text.length > 120;
  return (
    <span
      onClick={isLong ? () => setExpanded(!expanded) : undefined}
      className={cn(
        isLong && "cursor-pointer hover:text-foreground transition-colors",
        !expanded && isLong && "line-clamp-2",
        className,
      )}
    >
      {text}
      {isLong && !expanded && <span className="text-primary/40 ml-0.5">▸</span>}
    </span>
  );
}

export function AgentIcon({ agent }: { agent: string }) {
  if (agent.includes("media") || agent.includes("digest") || agent.includes("describe")) {
    return <span className="flex items-center justify-center w-5 h-5 rounded bg-pink-500/10 text-pink-500 text-[11px]">📸</span>;
  }
  switch (agent) {
    case "preprocessor":
      return <span className="flex items-center justify-center w-5 h-5 rounded bg-blue-500/10 text-blue-500 text-[11px]">📥</span>;
    case "fact_extractor":
      return <span className="flex items-center justify-center w-5 h-5 rounded bg-violet-500/10 text-violet-500 text-[11px]">🧠</span>;
    case "entity_extractor":
      return <span className="flex items-center justify-center w-5 h-5 rounded bg-emerald-500/10 text-emerald-500 text-[11px]">🔗</span>;
    case "embedder":
      return <span className="flex items-center justify-center w-5 h-5 rounded bg-amber-500/10 text-amber-500 text-[11px]">🔢</span>;
    case "cross_batch_validator_agent":
      return <span className="flex items-center justify-center w-5 h-5 rounded bg-rose-500/10 text-rose-500 text-[11px]">🛡️</span>;
    case "persister":
      return <span className="flex items-center justify-center w-5 h-5 rounded bg-slate-500/10 text-slate-500 text-[11px]">💾</span>;
    default:
      return <span className="flex items-center justify-center w-5 h-5 rounded bg-muted text-muted-foreground text-[11px]">⚙️</span>;
  }
}

export function AgentSampleRow({ sample }: { sample: ActivitySample }) {
  if (sample.item_type === "fact") {
    const scoreColor = (sample.score ?? 0) >= 0.7 ? "text-emerald-500 bg-emerald-500/10 border-emerald-500/20" : "text-amber-500 bg-amber-500/10 border-amber-500/20";
    const tag = sample.tags?.[0] ?? "Fact";
    return (
      <div className="flex items-start gap-2 py-0.5">
        <span className={cn("text-[9px] px-1 py-px rounded border font-mono shrink-0", scoreColor)}>
          {sample.score?.toFixed(1) ?? "?.?"}
        </span>
        <span className="text-[9px] uppercase tracking-wider text-muted-foreground/60 shrink-0 mt-px">{tag}</span>
        <ExpandableText text={sample.content ?? ""} className="text-[10px] text-foreground/80 leading-snug" />
      </div>
    );
  }

  if (sample.item_type === "entity") {
    return (
      <span className="inline-flex items-center gap-1 rounded bg-muted/60 border border-border/50 px-1.5 py-0.5 text-[10px]">
        <span className="text-[9px] text-primary/60 uppercase">{sample.tags?.[0] ?? "Entity"}</span>
        <span className="text-foreground/80">{sample.content}</span>
      </span>
    );
  }

  if (sample.item_type === "relationship") {
    return (
      <div className="flex flex-wrap items-center gap-1 text-[10px] py-0.5">
        <span className="text-foreground/70 bg-muted/50 px-1 rounded">{sample.source}</span>
        <span className="text-primary/60 text-[9px] uppercase tracking-wider">→ {sample.rel_type} →</span>
        <span className="text-foreground/70 bg-muted/50 px-1 rounded">{sample.target}</span>
      </div>
    );
  }

  if (sample.item_type === "media" && sample.status === "timeout") {
    return (
      <div className="rounded-md bg-amber-500/5 border border-amber-500/10 p-1.5 flex gap-2 items-start text-[10px]">
        <span className="shrink-0 text-amber-500/70 p-0.5 bg-amber-500/10 rounded">⏱</span>
        <div className="space-y-0.5 flex-1 min-w-0">
          <div className="flex items-center justify-between">
            <div className="font-medium text-amber-500/80 uppercase text-[9px] tracking-wider">{sample.agent?.replace(/_/g, " ")}</div>
            {sample.model && <span className="text-[8px] text-amber-500/40 font-mono italic">{sample.model}</span>}
          </div>
          <ExpandableText text={sample.content ?? ""} className="text-amber-600/70 dark:text-amber-400/70" />
        </div>
      </div>
    );
  }

  if (sample.item_type === "media" && sample.status === "skipped") {
    return (
      <div className="rounded-md bg-muted/30 border border-border/30 p-1.5 flex gap-2 items-start text-[10px]">
        <span className="shrink-0 text-muted-foreground/50 p-0.5 bg-muted/40 rounded">⏭</span>
        <div className="space-y-0.5 flex-1 min-w-0">
          <div className="flex items-center justify-between">
            <div className="font-medium text-muted-foreground/60 uppercase text-[9px] tracking-wider">{sample.agent?.replace(/_/g, " ")}</div>
            {sample.model && <span className="text-[8px] text-muted-foreground/30 font-mono italic">{sample.model}</span>}
          </div>
          <ExpandableText text={sample.content ?? ""} className="text-muted-foreground/60" />
        </div>
      </div>
    );
  }

  if (sample.item_type === "media") {
    return (
      <div className="rounded-md bg-blue-500/5 border border-blue-500/10 p-1.5 flex gap-2 items-start text-[10px]">
        <span className="shrink-0 text-blue-500/70 p-0.5 bg-blue-500/10 rounded">{sample.agent === "document_digester" ? "📄" : "🖼"}</span>
        <div className="space-y-0.5 flex-1 min-w-0">
          <div className="flex items-center justify-between">
            <div className="font-medium text-blue-500/80 uppercase text-[9px] tracking-wider">{sample.agent?.replace(/_/g, " ")}</div>
            {sample.model && (
              <span className="text-[8px] text-blue-500/40 font-mono italic">{sample.model}</span>
            )}
          </div>
          <ExpandableText text={sample.content ?? ""} className="text-foreground/70 leading-relaxed" />
        </div>
      </div>
    );
  }

  if (sample.item_type === "validation") {
    return (
      <div className="flex items-center gap-1.5 text-[10px] py-0.5 px-1.5 rounded bg-rose-500/5 border border-rose-500/10">
        <span className="text-rose-500 font-medium">✨</span>
        <span className="text-foreground/80">{sample.content}</span>
      </div>
    );
  }

  // fallback for messages
  return (
    <div className="flex items-start gap-1.5 text-[10px] text-muted-foreground">
      {sample.author && <span className="font-medium shrink-0">{sample.author}:</span>}
      <ExpandableText text={sample.content ?? ""} className="text-muted-foreground" />
      {sample.tags && sample.tags.length > 0 && (
        <span className="text-[9px] text-muted-foreground/40 shrink-0">[{sample.tags.join(", ")}]</span>
      )}
    </div>
  );
}

export function ActivityLog({ details }: { details?: { activity_log?: ActivityEntry[]; [key: string]: unknown } }) {
  const log = details?.activity_log ?? [];

  if (log.length === 0) {
    return (
      <div className="text-[11px] text-muted-foreground/60 py-2">
        Waiting for pipeline events...
      </div>
    );
  }

  return (
    <div className="space-y-3 max-h-[400px] overflow-y-auto pr-1">
      {log.map((entry, i) => (
        <div key={i} className="relative pl-3">
          {/* Timeline connecting line */}
          <div className="absolute left-0 top-3 bottom-[-12px] w-px bg-border/50 last:hidden" />

          {entry.type === "stage_start" ? (
            <div className="flex items-center gap-2 text-[11px] relative -left-[14px]">
              <div className="w-2 h-2 rounded-full bg-primary/40 ring-4 ring-background z-10 shrink-0" />
              <span className="text-foreground/80 font-medium">{entry.stage}</span>
            </div>
          ) : (
            <div className="relative -left-[17px] mb-1">
              <div className="flex gap-2.5 items-start">
                <div className="mt-1 z-10 shrink-0 bg-background rounded-full p-0.5 border border-border/50 shadow-sm">
                  <AgentIcon agent={entry.agent} />
                </div>

                <div className="flex-1 min-w-0 bg-card rounded-lg border border-border/40 shadow-sm overflow-hidden mt-0.5">
                  <div className="bg-muted/30 px-2.5 py-1.5 border-b border-border/30 flex justify-between items-center">
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] uppercase tracking-wider font-semibold text-foreground/70">
                        {entry.agent.replace(/_/g, " ")}
                      </span>
                      {entry.batch_idx != null && (
                        <span className="text-[9px] px-1 py-px rounded bg-muted text-muted-foreground border border-border/50 font-mono">
                          Batch {entry.batch_idx}
                        </span>
                      )}
                      {entry.model && (
                        <span className="text-[9px] px-1.2 py-px rounded bg-primary/5 text-primary/60 border border-primary/10 font-mono">
                          {entry.model}
                        </span>
                      )}
                    </div>
                    {entry.elapsed != null && (
                      <span className="text-[9px] text-muted-foreground font-mono">
                        {entry.elapsed < 1 ? `${(entry.elapsed * 1000).toFixed(0)}ms` : `${entry.elapsed.toFixed(1)}s`}
                      </span>
                    )}
                  </div>

                  <div className="p-2.5 space-y-1.5">
                    {entry.message && (
                      <div className="text-[11px] text-foreground/80 mb-1.5">{entry.message}</div>
                    )}

                    {entry.samples && entry.samples.length > 0 && (
                      <div className={entry.samples[0].item_type === "entity" ? "flex flex-wrap gap-1" : "space-y-1"}>
                        {entry.samples.map((sample, j) => (
                          <AgentSampleRow key={j} sample={sample} />
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
