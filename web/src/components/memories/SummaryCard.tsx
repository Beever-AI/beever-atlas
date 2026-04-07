import { Users, FileText, TrendingUp, MessageSquare, BookOpen, Zap } from "lucide-react";
import type { MemoryTier0 } from "@/lib/types";

interface SummaryCardProps {
  summary: MemoryTier0;
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

export function SummaryCard({ summary }: SummaryCardProps) {
  const hasActivity = summary.recent_activity_summary &&
    (summary.recent_activity_summary.facts_added_7d > 0 ||
     summary.recent_activity_summary.new_topics?.length > 0);

  return (
    <div className="rounded-2xl border border-border bg-card shadow-sm overflow-hidden">
      {/* Header */}
      <div className="p-5 sm:p-6 pb-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-xs uppercase tracking-wider text-muted-foreground mb-1.5">Channel Summary</p>
            <h3 className="text-xl font-semibold text-foreground">
              {summary.channel_name}
            </h3>
            {summary.description && (
              <p className="text-sm text-muted-foreground mt-1">{summary.description}</p>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-primary/10 text-primary text-sm font-medium">
              {summary.message_count.toLocaleString()} facts
            </span>
            {summary.cluster_count > 0 && (
              <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-muted text-muted-foreground text-sm font-medium">
                {summary.cluster_count} topics
              </span>
            )}
          </div>
        </div>

        {/* Overview narrative */}
        <p className="mt-4 text-base text-foreground/85 leading-relaxed">
          {summary.summary}
        </p>
      </div>

      {/* Multi-angle sections */}
      {(summary.themes || summary.momentum || summary.team_dynamics) && (
        <div className="border-t border-border bg-muted/20 px-5 sm:px-6 py-4 grid gap-4 sm:grid-cols-3">
          {summary.themes && (
            <div>
              <div className="flex items-center gap-1.5 mb-1.5">
                <BookOpen size={13} className="text-muted-foreground" />
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Themes</h4>
              </div>
              <p className="text-sm text-foreground/80 leading-relaxed">{summary.themes}</p>
            </div>
          )}
          {summary.momentum && (
            <div>
              <div className="flex items-center gap-1.5 mb-1.5">
                <TrendingUp size={13} className="text-muted-foreground" />
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Momentum</h4>
              </div>
              <p className="text-sm text-foreground/80 leading-relaxed">{summary.momentum}</p>
            </div>
          )}
          {summary.team_dynamics && (
            <div>
              <div className="flex items-center gap-1.5 mb-1.5">
                <Users size={13} className="text-muted-foreground" />
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Team</h4>
              </div>
              <p className="text-sm text-foreground/80 leading-relaxed">{summary.team_dynamics}</p>
            </div>
          )}
        </div>
      )}

      {/* Recent activity */}
      {hasActivity && summary.recent_activity_summary && (
        <div className="border-t border-border px-5 sm:px-6 py-3">
          <div className="flex items-center gap-1.5 mb-2">
            <Zap size={13} className="text-amber-500" />
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Last 7 Days</h4>
          </div>
          <div className="flex flex-wrap gap-3 text-sm">
            {summary.recent_activity_summary.facts_added_7d > 0 && (
              <span className="text-foreground/70">
                +{summary.recent_activity_summary.facts_added_7d} facts
              </span>
            )}
            {summary.recent_activity_summary.decisions_added_7d > 0 && (
              <span className="text-foreground/70">
                +{summary.recent_activity_summary.decisions_added_7d} decisions
              </span>
            )}
            {summary.recent_activity_summary.new_topics?.length > 0 && (
              <span className="text-foreground/70">
                {summary.recent_activity_summary.new_topics.length} new topics
              </span>
            )}
          </div>
          {summary.recent_activity_summary.highlights?.length > 0 && (
            <div className="mt-2 space-y-1">
              {summary.recent_activity_summary.highlights.map((h, i) => (
                <p key={i} className="text-xs text-foreground/60 line-clamp-1">
                  <span className="text-primary font-medium">{h.author_name || "Someone"}</span>: {h.memory_text}
                </p>
              ))}
            </div>
          )}
        </div>
      )}

      {/* People & Tech */}
      {(summary.top_people?.length > 0 || summary.tech_stack?.length > 0) && (
        <div className="border-t border-border px-5 sm:px-6 py-3 flex flex-wrap gap-4">
          {summary.top_people?.length > 0 && (
            <div className="flex-1 min-w-[200px]">
              <div className="flex items-center gap-1.5 mb-2">
                <Users size={13} className="text-muted-foreground" />
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Key People</h4>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {summary.top_people.slice(0, 8).map((p) => (
                  <span key={p.name} className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded-full ${roleBadge(p.role)}`}>
                    {p.name}
                    <span className="opacity-60">{p.role.replace("_", " ")}</span>
                  </span>
                ))}
              </div>
            </div>
          )}
          {summary.tech_stack?.length > 0 && (
            <div className="flex-1 min-w-[200px]">
              <div className="flex items-center gap-1.5 mb-2">
                <FileText size={13} className="text-muted-foreground" />
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Tech Stack</h4>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {summary.tech_stack.slice(0, 10).map((t) => (
                  <span key={t.name} className="px-2 py-0.5 text-xs rounded-full bg-cyan-100 text-cyan-700 dark:bg-cyan-950/50 dark:text-cyan-300">
                    {t.name}
                    {t.category && <span className="opacity-60 ml-1">{t.category}</span>}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Glossary */}
      {summary.glossary_terms?.length > 0 && (
        <div className="border-t border-border px-5 sm:px-6 py-3">
          <div className="flex items-center gap-1.5 mb-2">
            <MessageSquare size={13} className="text-muted-foreground" />
            <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Glossary</h4>
          </div>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {summary.glossary_terms.map((g) => (
              <div key={g.term} className="text-xs">
                <span className="font-semibold text-foreground">{g.term}</span>
                <span className="text-muted-foreground"> — {g.definition}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Stats footer */}
      <div className="border-t border-border px-5 sm:px-6 py-2.5 flex items-center gap-4 text-xs text-muted-foreground">
        {summary.author_count > 0 && <span>{summary.author_count} contributors</span>}
        {summary.media_count > 0 && <span>{summary.media_count} media files</span>}
        {summary.updated_at && (
          <span className="ml-auto">
            Updated {new Date(summary.updated_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}
          </span>
        )}
      </div>
    </div>
  );
}
