import { useState } from "react";
import { ChevronDown, ChevronRight, CircleDot, HelpCircle, Zap, Users, MessageSquare } from "lucide-react";
import type { MemoryTier1, MemoryTier2 } from "@/lib/types";
import { FactCard } from "./FactCard";

interface ClusterCardProps {
  cluster: MemoryTier1;
  facts: MemoryTier2[];
}

function statusBadge(status: string): { color: string; label: string } {
  switch (status) {
    case "completed":
      return { color: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300", label: "Completed" };
    case "stale":
      return { color: "bg-red-100 text-red-700 dark:bg-red-950/50 dark:text-red-300", label: "Stale" };
    default:
      return { color: "bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300", label: "Active" };
  }
}

function roleBadge(role: string): string {
  const colors: Record<string, string> = {
    decision_maker: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
    expert: "bg-purple-100 text-purple-700 dark:bg-purple-950/50 dark:text-purple-300",
    contributor: "bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300",
    mentioned: "bg-muted text-muted-foreground",
  };
  return colors[role] || colors.mentioned;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "";
  try {
    if (/^\d+\.\d+$/.test(ts)) {
      return new Date(parseFloat(ts) * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }
    return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}

export function ClusterCard({ cluster, facts }: ClusterCardProps) {
  const [expanded, setExpanded] = useState(false);

  const memberFacts = facts.filter((f) => f.cluster_id === cluster.id);
  const badge = statusBadge(cluster.status);
  const hasEnrichment = cluster.key_facts?.length > 0 || cluster.people?.length > 0 ||
    cluster.decisions?.length > 0 || cluster.technologies?.length > 0;

  return (
    <div className="rounded-2xl border border-border bg-card shadow-sm overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-start gap-3 p-4 sm:p-5 text-left hover:bg-muted/40 transition-colors"
      >
        {expanded ? (
          <ChevronDown size={16} className="text-primary shrink-0 mt-0.5" />
        ) : (
          <ChevronRight size={16} className="text-primary shrink-0 mt-0.5" />
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h4 className="text-lg font-semibold text-foreground leading-tight line-clamp-2">
                {cluster.title || cluster.topic}
              </h4>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-sm text-muted-foreground">{cluster.fact_count} facts</span>
                <span className={`px-1.5 py-0.5 text-[10px] font-medium rounded-full ${badge.color}`}>
                  {badge.label}
                </span>
                {cluster.date_range.start && (
                  <span className="text-xs text-muted-foreground">
                    {formatTimestamp(cluster.date_range.start)} – {formatTimestamp(cluster.date_range.end)}
                  </span>
                )}
              </div>
            </div>
            <div className="hidden lg:flex flex-wrap justify-end gap-1 shrink-0 max-w-[40%]">
              {cluster.topic_tags.map((tag) => (
                <span key={tag} className="px-2 py-0.5 text-xs rounded-full bg-muted text-muted-foreground">
                  {tag}
                </span>
              ))}
            </div>
          </div>

          {/* Summary */}
          <p className="text-sm text-muted-foreground mt-2 leading-relaxed line-clamp-2">
            {cluster.summary}
          </p>

          {/* Current state + open questions inline */}
          {(cluster.current_state || cluster.open_questions) && (
            <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2">
              {cluster.current_state && (
                <p className="text-xs text-foreground/60 line-clamp-1">
                  <CircleDot size={10} className="inline mr-1 text-emerald-500" />
                  {cluster.current_state}
                </p>
              )}
              {cluster.open_questions && (
                <p className="text-xs text-foreground/60 line-clamp-1">
                  <HelpCircle size={10} className="inline mr-1 text-amber-500" />
                  {cluster.open_questions}
                </p>
              )}
            </div>
          )}

          {/* People chips */}
          {cluster.people?.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-2">
              {cluster.people.slice(0, 5).map((p) => (
                <span key={p.name} className={`px-1.5 py-0.5 text-[10px] rounded-full ${roleBadge(p.role)}`}>
                  {p.name}
                </span>
              ))}
              {cluster.people.length > 5 && (
                <span className="text-[10px] text-muted-foreground self-center">+{cluster.people.length - 5} more</span>
              )}
            </div>
          )}
        </div>
      </button>

      {/* Expanded content */}
      {expanded && (
        <div className="border-t border-border">
          {/* Enrichment sections */}
          {hasEnrichment && (
            <div className="bg-muted/15 px-4 sm:px-5 py-3 space-y-3">
              {/* Impact note */}
              {cluster.impact_note && (
                <div className="flex items-start gap-1.5">
                  <Zap size={12} className="text-amber-500 mt-0.5 shrink-0" />
                  <p className="text-xs text-foreground/70">{cluster.impact_note}</p>
                </div>
              )}

              {/* Key facts */}
              {cluster.key_facts?.length > 0 && (
                <div>
                  <h5 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">Key Facts</h5>
                  <div className="space-y-1">
                    {cluster.key_facts.map((kf) => (
                      <div key={kf.fact_id} className="flex items-start gap-2 text-xs">
                        <span className="shrink-0 mt-0.5 w-1.5 h-1.5 rounded-full bg-primary/60" />
                        <div className="flex-1 min-w-0">
                          <p className="text-foreground/80 leading-relaxed">{kf.memory_text}</p>
                          <span className="text-muted-foreground">
                            {kf.author_name && <>{kf.author_name} · </>}
                            {kf.fact_type && <span className="capitalize">{kf.fact_type}</span>}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Decisions */}
              {cluster.decisions?.length > 0 && (
                <div>
                  <h5 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">Decisions</h5>
                  <div className="space-y-1">
                    {cluster.decisions.map((d) => (
                      <div key={d.name} className="flex items-center gap-2 text-xs">
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${d.status === "active" ? "bg-emerald-500" : "bg-red-400"}`} />
                        <span className={`text-foreground/80 ${d.status === "superseded" ? "line-through opacity-60" : ""}`}>
                          {d.name}
                        </span>
                        {d.decided_by && <span className="text-muted-foreground">by {d.decided_by}</span>}
                        {d.superseded_by && <span className="text-muted-foreground">→ {d.superseded_by}</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Technologies + People row */}
              <div className="flex flex-wrap gap-4">
                {cluster.technologies?.length > 0 && (
                  <div>
                    <h5 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-1">Tech</h5>
                    <div className="flex flex-wrap gap-1">
                      {cluster.technologies.map((t) => (
                        <span key={t.name} className="px-2 py-0.5 text-[10px] rounded-full bg-cyan-100 text-cyan-700 dark:bg-cyan-950/50 dark:text-cyan-300">
                          {t.name}
                          {t.champion && <span className="opacity-60 ml-1">({t.champion})</span>}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {cluster.people?.length > 0 && (
                  <div>
                    <h5 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-1">
                      <Users size={10} className="inline mr-1" />People
                    </h5>
                    <div className="flex flex-wrap gap-1">
                      {cluster.people.map((p) => (
                        <span key={p.name} className={`px-2 py-0.5 text-[10px] rounded-full ${roleBadge(p.role)}`}>
                          {p.name} <span className="opacity-60">{p.role.replace("_", " ")}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* FAQ candidates */}
              {cluster.faq_candidates?.length > 0 && (
                <div>
                  <h5 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-1.5">
                    <MessageSquare size={10} className="inline mr-1" />FAQ
                  </h5>
                  <div className="space-y-1.5">
                    {cluster.faq_candidates.map((faq, i) => (
                      <div key={i} className="text-xs">
                        <p className="font-medium text-foreground/80">Q: {faq.question}</p>
                        <p className="text-muted-foreground ml-3">A: {faq.answer}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Fact type breakdown */}
              {Object.keys(cluster.fact_type_counts || {}).length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(cluster.fact_type_counts).map(([type, count]) => (
                    <span key={type} className="px-2 py-0.5 text-[10px] rounded-full bg-muted text-muted-foreground capitalize">
                      {count} {type}{count !== 1 ? "s" : ""}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Member facts */}
          <div className="bg-muted/25 p-3 sm:p-4 space-y-2">
            <h5 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-2">
              All Facts ({memberFacts.length})
            </h5>
            {memberFacts.length > 0 ? (
              memberFacts.map((fact) => <FactCard key={fact.id} fact={fact} />)
            ) : (
              <p className="text-sm text-muted-foreground p-2">
                No facts in this cluster.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
