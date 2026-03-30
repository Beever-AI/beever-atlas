import type { MemoryTier0 } from "@/lib/types";

interface SummaryCardProps {
  summary: MemoryTier0;
}

export function SummaryCard({ summary }: SummaryCardProps) {
  const updatedDate = new Date(summary.updated_at);

  return (
    <div className="rounded-2xl border border-border bg-card shadow-sm p-5 sm:p-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs uppercase tracking-wider text-muted-foreground mb-2">Channel Summary</p>
          <h3 className="text-xl font-semibold text-foreground">
            {summary.channel_name} <span className="text-muted-foreground">overview</span>
          </h3>
        </div>
        <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-primary/10 text-primary text-sm font-medium w-fit">
          {summary.message_count.toLocaleString()} messages
        </div>
      </div>
      <p className="mt-4 text-base text-foreground/85 leading-relaxed">
        {summary.summary}
      </p>
      <p className="mt-4 text-sm text-muted-foreground">
        Updated{" "}
        {updatedDate.toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
          year: "numeric",
        })}
      </p>
    </div>
  );
}
