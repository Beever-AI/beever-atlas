import { Brain, Users, GitBranch, Hash } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { Stats } from "@/hooks/useStats";

interface StatCardProps {
  icon: React.ReactNode;
  label: string;
  value: number | null;
  loading: boolean;
}

function StatCard({ icon, label, value, loading }: StatCardProps) {
  return (
    <Card>
      <CardContent className="flex items-center gap-4 py-2">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
          {icon}
        </div>
        <div className="min-w-0">
          {loading ? (
            <>
              <Skeleton className="h-6 w-16 mb-1" />
              <Skeleton className="h-3 w-24" />
            </>
          ) : (
            <>
              <p className="text-2xl font-semibold tabular-nums text-foreground">
                {value?.toLocaleString() ?? "—"}
              </p>
              <p className="text-xs text-muted-foreground">{label}</p>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

interface StatCardsProps {
  stats: Stats | null;
  loading: boolean;
}

export function StatCards({ stats, loading }: StatCardsProps) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        icon={<Brain size={20} />}
        label="Things learned"
        value={stats?.total_memories ?? null}
        loading={loading}
      />
      <StatCard
        icon={<Users size={20} />}
        label="People & topics"
        value={stats?.total_entities ?? null}
        loading={loading}
      />
      <StatCard
        icon={<GitBranch size={20} />}
        label="Connections mapped"
        value={stats?.total_relationships ?? null}
        loading={loading}
      />
      <StatCard
        icon={<Hash size={20} />}
        label="Channels synced"
        value={stats?.channels_synced ?? null}
        loading={loading}
      />
    </div>
  );
}
