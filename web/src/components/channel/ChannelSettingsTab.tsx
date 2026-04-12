import { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import { Loader2, Clock, Zap, Calendar, Hand, ChevronDown, ChevronRight, Settings2, Info, Sparkles, FolderTree, BookOpen } from "lucide-react";
import { useChannelPolicy } from "@/hooks/useChannelPolicy";
import { cn } from "@/lib/utils";
import type { SyncConfig, IngestionConfig, ConsolidationConfig, ConsolidationStrategy, WikiConfig, WikiGenerationStrategy } from "@/lib/types";

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

const DEFAULT_SYNC: SyncConfig = {
  trigger_mode: "manual", cron_expression: null, interval_minutes: null,
  sync_type: "auto", max_messages: 1000, min_sync_interval_minutes: 1,
};
const DEFAULT_INGESTION: IngestionConfig = {
  batch_size: 10, quality_threshold: 0.5, max_facts_per_message: 2,
  skip_entity_extraction: false, skip_graph_writes: false,
};
const DEFAULT_CONSOLIDATION: ConsolidationConfig = {
  strategy: "manual", after_n_syncs: null, cron_expression: null,
  similarity_threshold: 0.6, merge_threshold: 0.85,
  min_facts_for_clustering: 3, staleness_refresh_days: null,
};
const DEFAULT_WIKI: WikiConfig = {
  enabled: true, generation_strategy: "after_consolidation",
  cron_expression: null, auto_regenerate_on_stale: true,
  min_facts_for_generation: 5, topic_subpage_threshold: 15,
};

// ---------------------------------------------------------------------------
// Frequency options (the primary question)
// ---------------------------------------------------------------------------

const FREQUENCY_OPTIONS = [
  {
    id: "realtime",
    icon: Zap,
    label: "Every few minutes",
    hint: "Best for fast-moving channels where decisions happen quickly",
    sync: { trigger_mode: "interval" as const, interval_minutes: 5 },
  },
  {
    id: "hourly",
    icon: Clock,
    label: "Every hour",
    hint: "Good balance for active channels without high urgency",
    sync: { trigger_mode: "interval" as const, interval_minutes: 60 },
  },
  {
    id: "daily",
    icon: Calendar,
    label: "Once a day",
    hint: "Recommended for most channels. Runs at 2 AM to avoid disruption",
    recommended: true,
    sync: { trigger_mode: "cron" as const, cron_expression: "0 2 * * *", interval_minutes: null },
  },
  {
    id: "manual",
    icon: Hand,
    label: "Only when I ask",
    hint: "You control exactly when knowledge gets updated",
    sync: { trigger_mode: "manual" as const, interval_minutes: null, cron_expression: null },
  },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function detectFrequency(sync: SyncConfig): string {
  if (sync.trigger_mode === "manual") return "manual";
  if (sync.trigger_mode === "cron") return "daily";
  if (sync.trigger_mode === "interval") {
    if (sync.interval_minutes && sync.interval_minutes <= 15) return "realtime";
    if (sync.interval_minutes && sync.interval_minutes <= 120) return "hourly";
  }
  return "custom";
}

function Tip({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex gap-2 rounded-lg bg-primary/5 border border-primary/10 px-3 py-2">
      <Info className="h-3.5 w-3.5 text-primary shrink-0 mt-0.5" />
      <p className="text-[12px] leading-relaxed text-muted-foreground">{children}</p>
    </div>
  );
}

function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 focus:outline-none focus:ring-2 focus:ring-primary focus:ring-offset-1",
        checked ? "bg-primary" : "bg-muted-foreground/30",
      )}
    >
      <span className={cn(
        "pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform duration-200",
        checked ? "translate-x-4" : "translate-x-0",
      )} />
    </button>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ChannelSettingsTab() {
  const { id } = useParams<{ id: string }>();
  const channelId = id ?? "";
  const { policy, isLoading, error, savePolicy, deletePolicy } = useChannelPolicy(channelId);

  const [sync, setSync] = useState<SyncConfig>(DEFAULT_SYNC);
  const [ingestion, setIngestion] = useState<IngestionConfig>(DEFAULT_INGESTION);
  const [consolidation, setConsolidation] = useState<ConsolidationConfig>(DEFAULT_CONSOLIDATION);
  const [wiki, setWiki] = useState<WikiConfig>(DEFAULT_WIKI);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; message: string } | null>(null);
  const [showMore, setShowMore] = useState(false);

  const selectedFreq = detectFrequency(sync);
  const deepAnalysis = !(ingestion.skip_entity_extraction ?? false);
  const qualityThreshold = ingestion.quality_threshold ?? 0.5;
  const strategy = consolidation.strategy ?? "after_every_sync";

  useEffect(() => {
    if (!policy) return;
    const e = policy.effective;
    setSync(e.sync);
    setIngestion(e.ingestion);
    setConsolidation(e.consolidation);
    setWiki(e.wiki ?? DEFAULT_WIKI);
  }, [policy]);

  function selectFrequency(freqId: string) {
    const opt = FREQUENCY_OPTIONS.find((f) => f.id === freqId);
    if (!opt) return;
    setSync((prev) => ({ ...prev, ...opt.sync }));
    // Set sensible consolidation defaults based on frequency
    if (freqId === "realtime") {
      setConsolidation((prev) => ({ ...prev, strategy: "after_every_sync" as ConsolidationStrategy }));
    } else if (freqId === "daily") {
      setConsolidation((prev) => ({ ...prev, strategy: "after_every_sync" as ConsolidationStrategy }));
    }
    setFeedback(null);
  }

  async function handleSave() {
    setSaving(true);
    setFeedback(null);
    try {
      await savePolicy({ sync, ingestion, consolidation, wiki });
      setFeedback({ kind: "success", message: "Settings saved." });
    } catch {
      setFeedback({ kind: "error", message: error ?? "Failed to save." });
    } finally {
      setSaving(false);
    }
  }

  async function handleReset() {
    setResetting(true);
    setFeedback(null);
    try {
      await deletePolicy();
      setFeedback({ kind: "success", message: "Reset to defaults." });
    } catch {
      setFeedback({ kind: "error", message: error ?? "Failed to reset." });
    } finally {
      setResetting(false);
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16 text-muted-foreground gap-2">
        <Loader2 size={16} className="animate-spin" />
        <span className="text-sm">Loading...</span>
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto animate-fade-in">
      <div className="mx-auto w-full max-w-3xl p-4 sm:p-6 lg:p-8 space-y-6">
      {/* Header */}
      <div>
        <h3 className="font-heading text-[28px] leading-tight text-foreground">Channel Settings</h3>
        <p className="text-sm text-muted-foreground mt-1">
          Beever reads your channel messages and turns them into searchable knowledge — topics, decisions, and key facts. These settings control how that works.
        </p>
      </div>

      {/* ── Section 1: How often? ─────────────────────────── */}
      <div className="space-y-3">
        <div>
          <h4 className="text-sm font-semibold text-foreground">How often should knowledge update?</h4>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            Beever checks for new messages and extracts knowledge on this schedule.
          </p>
        </div>

        <div className="grid gap-2">
          {FREQUENCY_OPTIONS.map((opt) => {
            const Icon = opt.icon;
            const isSelected = selectedFreq === opt.id;
            return (
              <button
                key={opt.id}
                type="button"
                onClick={() => selectFrequency(opt.id)}
                className={cn(
                  "flex items-start gap-3 rounded-xl border px-4 py-3 text-left transition-all duration-150",
                  isSelected
                    ? "border-primary bg-primary/5"
                    : "border-border bg-card hover:border-primary/30 hover:bg-muted/20",
                )}
              >
                <div className={cn(
                  "flex h-8 w-8 items-center justify-center rounded-lg shrink-0 mt-0.5",
                  isSelected ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
                )}>
                  <Icon className="h-4 w-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className={cn("text-sm font-medium", isSelected ? "text-primary" : "text-foreground")}>
                      {opt.label}
                    </span>
                    {opt.recommended && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 font-medium">
                        Recommended
                      </span>
                    )}
                  </div>
                  <p className="text-[12px] text-muted-foreground mt-0.5">{opt.hint}</p>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {/* ── Section 2: More options (progressive disclosure) ── */}
      <button
        type="button"
        onClick={() => setShowMore(!showMore)}
        className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <Settings2 className="h-3.5 w-3.5" />
        <span>More options</span>
        {showMore
          ? <ChevronDown className="h-3.5 w-3.5" />
          : <ChevronRight className="h-3.5 w-3.5" />
        }
      </button>

      {showMore && (
        <div className="space-y-6 animate-fade-in">

          {/* ── 2a: Analysis depth ──────────────────────────── */}
          <div className="rounded-2xl border border-border bg-card px-5 py-4 space-y-4">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              <h4 className="text-sm font-semibold text-foreground">Analysis depth</h4>
            </div>

            <Tip>
              When Beever reads your messages, it extracts facts like "Team decided to use PostgreSQL" or "Launch date moved to March". These settings control how deeply it analyzes each message.
            </Tip>

            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm text-foreground">Deep analysis</div>
                <p className="text-[11px] text-muted-foreground mt-0.5 max-w-sm">
                  Identifies people, projects, and decisions to build a knowledge graph.
                  Turn off for casual channels where speed matters more than detail.
                </p>
              </div>
              <Toggle
                checked={deepAnalysis}
                onChange={(v) => setIngestion((prev) => ({
                  ...prev,
                  skip_entity_extraction: !v,
                  skip_graph_writes: !v,
                }))}
              />
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm text-foreground">Quality filter</div>
                  <p className="text-[11px] text-muted-foreground mt-0.5 max-w-sm">
                    Filters out low-value facts like greetings or off-topic chat.
                    Lower = keep more, higher = only keep important facts.
                  </p>
                </div>
                <span className="text-sm font-mono text-muted-foreground tabular-nums">{qualityThreshold.toFixed(1)}</span>
              </div>
              <input
                type="range" min={0} max={1} step={0.1}
                value={qualityThreshold}
                onChange={(e) => setIngestion((prev) => ({ ...prev, quality_threshold: parseFloat(e.target.value) }))}
                className="w-full accent-primary"
              />
              <div className="flex justify-between text-[11px] text-muted-foreground/60">
                <span>Keep more</span>
                <span>Keep less, higher quality</span>
              </div>
            </div>

            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="text-sm text-foreground">Batch size</div>
                <p className="text-[11px] text-muted-foreground mt-0.5 max-w-sm">
                  Messages processed together in each pipeline run.
                  Larger batches = fewer LLM calls but slower per batch.
                </p>
              </div>
              <input
                type="number" min={5} max={50} step={5}
                value={ingestion.batch_size ?? 20}
                onChange={(e) => {
                  const val = parseInt(e.target.value, 10);
                  if (!isNaN(val) && val >= 1) setIngestion((prev) => ({ ...prev, batch_size: val }));
                }}
                className="h-9 w-20 rounded-lg border border-border bg-card px-3 text-sm text-foreground text-center placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
          </div>

          {/* ── 2b: Topic organization ──────────────────────── */}
          <div className="rounded-2xl border border-border bg-card px-5 py-4 space-y-4">
            <div className="flex items-center gap-2">
              <FolderTree className="h-4 w-4 text-primary" />
              <h4 className="text-sm font-semibold text-foreground">Topic organization</h4>
            </div>

            <Tip>
              After extracting facts, Beever groups related facts into topics (like "Infrastructure migration" or "Q2 Planning") and generates summaries. This setting controls when that grouping happens.
            </Tip>

            <div className="space-y-1.5">
              {([
                { value: "after_every_sync" as ConsolidationStrategy, label: "After each update", hint: "Topics and summaries refresh every time new knowledge is added" },
                { value: "after_n_syncs" as ConsolidationStrategy, label: "Periodically", hint: "Waits for several updates before reorganizing (saves processing)" },
                { value: "manual" as ConsolidationStrategy, label: "When I choose", hint: "Use the Reconsolidate button on the Memories tab" },
              ]).map((opt) => (
                <label
                  key={opt.value}
                  className={cn(
                    "flex items-start gap-2.5 cursor-pointer rounded-lg px-3 py-2.5 transition-colors",
                    strategy === opt.value ? "bg-primary/5" : "hover:bg-muted/30",
                  )}
                >
                  <input
                    type="radio" name="organize"
                    checked={strategy === opt.value}
                    onChange={() => setConsolidation((prev) => ({ ...prev, strategy: opt.value }))}
                    className="accent-primary mt-0.5"
                  />
                  <div>
                    <div className="text-sm text-foreground">{opt.label}</div>
                    <div className="text-[11px] text-muted-foreground">{opt.hint}</div>
                  </div>
                </label>
              ))}
            </div>

            {strategy === "after_n_syncs" && (
              <div className="flex items-center gap-2 pl-3">
                <span className="text-sm text-muted-foreground">Reorganize after</span>
                <input
                  type="number" min={2}
                  value={consolidation.after_n_syncs ?? ""}
                  onChange={(e) => {
                    const val = parseInt(e.target.value, 10);
                    setConsolidation((prev) => ({ ...prev, after_n_syncs: isNaN(val) ? null : val }));
                  }}
                  placeholder="3"
                  className="h-9 w-16 rounded-lg border border-border bg-card px-3 text-sm text-foreground text-center placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                />
                <span className="text-sm text-muted-foreground">updates</span>
              </div>
            )}
          </div>

          {/* ── 2c: Wiki generation ─────────────────────────── */}
          <div className="rounded-2xl border border-border bg-card px-5 py-4 space-y-4">
            <div className="flex items-center gap-2">
              <BookOpen className="h-4 w-4 text-primary" />
              <h4 className="text-sm font-semibold text-foreground">Wiki generation</h4>
            </div>
            <Tip>
              Beever can automatically generate a structured wiki for each channel, turning scattered conversations into organized documentation.
            </Tip>
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm text-foreground">Enable wiki</div>
                <p className="text-[11px] text-muted-foreground mt-0.5 max-w-sm">
                  Generate a wiki page for channels with enough knowledge
                </p>
              </div>
              <Toggle checked={wiki.enabled ?? true} onChange={(v) => setWiki((prev) => ({ ...prev, enabled: v }))} />
            </div>
            {(wiki.enabled ?? true) && (
              <>
                <div className="space-y-1.5">
                  <div className="text-sm text-foreground mb-1">When to regenerate</div>
                  {([
                    { value: "after_consolidation" as WikiGenerationStrategy, label: "After topics update", hint: "Regenerate when topics and summaries change" },
                    { value: "after_every_sync" as WikiGenerationStrategy, label: "After every sync", hint: "Always up-to-date but uses more resources" },
                    { value: "manual" as WikiGenerationStrategy, label: "Only manually", hint: "Use the Regenerate button in the wiki tab" },
                  ]).map((opt) => (
                    <label key={opt.value} className={cn("flex items-start gap-2.5 cursor-pointer rounded-lg px-3 py-2.5 transition-colors", (wiki.generation_strategy ?? "after_consolidation") === opt.value ? "bg-primary/5" : "hover:bg-muted/30")}>
                      <input type="radio" name="channel_wiki_strategy" checked={(wiki.generation_strategy ?? "after_consolidation") === opt.value}
                        onChange={() => setWiki((prev) => ({ ...prev, generation_strategy: opt.value }))}
                        className="accent-primary mt-0.5" />
                      <div>
                        <div className="text-sm text-foreground">{opt.label}</div>
                        <div className="text-[11px] text-muted-foreground">{opt.hint}</div>
                      </div>
                    </label>
                  ))}
                </div>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm text-foreground">Auto-regenerate when stale</div>
                    <p className="text-[11px] text-muted-foreground mt-0.5 max-w-sm">
                      Automatically rebuild the wiki when new data makes it outdated
                    </p>
                  </div>
                  <Toggle checked={wiki.auto_regenerate_on_stale ?? true} onChange={(v) => setWiki((prev) => ({ ...prev, auto_regenerate_on_stale: v }))} />
                </div>
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-sm text-foreground">Minimum facts</div>
                    <p className="text-[11px] text-muted-foreground mt-0.5 max-w-sm">
                      Don't generate a wiki until the channel has this many facts
                    </p>
                  </div>
                  <input type="number" min={1} max={100} value={wiki.min_facts_for_generation ?? 5}
                    onChange={(e) => { const v = parseInt(e.target.value, 10); if (!isNaN(v) && v >= 1) setWiki((prev) => ({ ...prev, min_facts_for_generation: v })); }}
                    className="h-9 w-20 rounded-lg border border-border bg-card px-3 text-sm text-foreground text-center placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary" />
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ── Feedback ──────────────────────────────────────── */}
      {feedback && (
        <div className={
          feedback.kind === "success"
            ? "rounded-lg border border-emerald-500/20 bg-emerald-500/5 px-4 py-2.5 text-sm text-emerald-600 dark:text-emerald-400"
            : "rounded-lg border border-rose-500/20 bg-rose-500/5 px-4 py-2.5 text-sm text-rose-600 dark:text-rose-400"
        }>
          {feedback.message}
        </div>
      )}

      {/* ── Actions ───────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <button
          type="button" onClick={handleSave} disabled={saving}
          className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving && <Loader2 size={14} className="animate-spin" />}
          {saving ? "Saving..." : "Save"}
        </button>
        <button
          type="button" onClick={handleReset} disabled={resetting}
          className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {resetting && <Loader2 size={14} className="animate-spin" />}
          {resetting ? "Resetting..." : "Reset to defaults"}
        </button>
      </div>
      </div>
    </div>
  );
}
