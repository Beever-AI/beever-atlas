import { MessageSquare, XCircle, AlertCircle, Settings, Trash2, RefreshCw, MonitorSmartphone, Send, FileText } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/hooks/useTheme";
import { getPlatformBadgeStyle } from "@/lib/platform-badge";
import type { PlatformConnection } from "@/lib/types";

interface PlatformCardProps {
  connection: PlatformConnection;
  onDisconnect: () => void;
  onManage: () => void;
}

function SlackIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zm-1.27 0a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.163 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.163 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.163 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zm0-1.27a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.315A2.528 2.528 0 0 1 24 15.163a2.528 2.528 0 0 1-2.522 2.523h-6.315z" />
    </svg>
  );
}

const PLATFORM_META: Record<
  string,
  { label: string; Icon: React.ComponentType<{ className?: string }> }
> = {
  slack: { label: "Slack", Icon: SlackIcon },
  discord: { label: "Discord", Icon: MessageSquare },
  teams: { label: "Microsoft Teams", Icon: MonitorSmartphone },
  telegram: { label: "Telegram", Icon: Send },
  file: { label: "Uploaded files (CSV / TSV / JSONL)", Icon: FileText },
};

export function PlatformCard({ connection, onDisconnect, onManage }: PlatformCardProps) {
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";
  const meta = PLATFORM_META[connection.platform] ?? { label: connection.platform, Icon: MessageSquare };
  const { Icon } = meta;
  const badgeStyle = getPlatformBadgeStyle(connection.platform, isDark);
  const isEnv = connection.source === "env";

  return (
    <div
      className={cn(
        "group bg-card border rounded-2xl overflow-hidden transition-all duration-200",
        "hover:shadow-lg hover:shadow-black/5 dark:hover:shadow-black/20",
        connection.status === "connected" && "border-emerald-500/30",
        connection.status === "error" && "border-rose-500/30",
        connection.status === "disconnected" && "border-border",
      )}
    >
      {/* Header */}
      <div className="px-6 pt-6 pb-4 flex items-start gap-4">
        <div
          className="w-12 h-12 rounded-xl flex items-center justify-center shrink-0 transition-transform duration-200 group-hover:scale-105"
          style={{ backgroundColor: badgeStyle.backgroundColor }}
        >
          <div style={{ color: badgeStyle.color }}>
            <Icon className="w-6 h-6" />
          </div>
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-base font-semibold text-foreground">{connection.display_name || meta.label}</h3>
            <StatusBadge status={connection.status} />
            {isEnv && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-muted text-muted-foreground">
                System
              </span>
            )}
          </div>
          <p className="text-sm text-muted-foreground mt-0.5 truncate">{meta.label}</p>
        </div>
      </div>

      {/* Error message */}
      {connection.status === "error" && connection.error_message && (
        <div className="mx-6 mb-4 flex items-start gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 px-3 py-2.5">
          <AlertCircle className="w-4 h-4 text-rose-500 shrink-0 mt-0.5" />
          <p className="text-xs text-rose-600 dark:text-rose-400 leading-relaxed">{connection.error_message}</p>
        </div>
      )}

      {/* Channel count */}
      {connection.status === "connected" && connection.selected_channels.length > 0 && (
        <div className="mx-6 mb-4 flex items-center gap-2 px-3 py-2 rounded-lg bg-emerald-500/5 border border-emerald-500/10 text-xs text-emerald-600 dark:text-emerald-400">
          <div className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
          {connection.selected_channels.length} channel{connection.selected_channels.length !== 1 ? "s" : ""} monitored
        </div>
      )}

      {/* Actions */}
      <div className="px-6 pb-6 flex gap-2 flex-wrap">
        {connection.status === "error" ? (
          <>
            <button
              type="button"
              onClick={onManage}
              className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
            >
              <RefreshCw className="w-4 h-4" />
              Retry
            </button>
            {!isEnv && (
              <button
                type="button"
                onClick={onDisconnect}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <Trash2 className="w-4 h-4" />
                Remove
              </button>
            )}
          </>
        ) : (
          <>
            <button
              type="button"
              onClick={onManage}
              className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg border border-border text-sm font-medium text-foreground hover:bg-muted transition-colors"
            >
              <Settings className="w-4 h-4" />
              Manage Channels
            </button>
            {!isEnv && (
              <button
                type="button"
                onClick={onDisconnect}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium text-muted-foreground hover:bg-rose-500/10 hover:text-rose-600 dark:hover:text-rose-400 transition-colors"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: PlatformConnection["status"] }) {
  if (status === "connected") {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 uppercase tracking-wide">
        <div className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
        Connected
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-rose-500/10 text-rose-600 dark:text-rose-400 uppercase tracking-wide">
        <XCircle className="w-3 h-3" />
        Error
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium bg-muted text-muted-foreground">
      Disconnected
    </span>
  );
}
