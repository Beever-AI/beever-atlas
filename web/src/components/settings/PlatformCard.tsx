import { MessageSquare, XCircle, AlertCircle, Settings, Trash2, RefreshCw, MonitorSmartphone, Send, FileText, KeyRound } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/hooks/useTheme";
import { getPlatformBadgeStyle } from "@/lib/platform-badge";
import type { PlatformConnection } from "@/lib/types";

interface PlatformCardProps {
  connection: PlatformConnection;
  onDisconnect: () => void;
  onManage: () => void;
  onEdit: () => void;
}

function SlackIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zm-1.27 0a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.163 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.163 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.163 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zm0-1.27a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.315A2.528 2.528 0 0 1 24 15.163a2.528 2.528 0 0 1-2.522 2.523h-6.315z" />
    </svg>
  );
}

function DiscordIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028 14.09 14.09 0 0 0 1.226-1.994.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.095 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.095 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z" />
    </svg>
  );
}

function MattermostIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path d="M12.081 0C7.048-.067 2.433 3.324.851 8.16c-1.584 4.834.353 10.139 4.683 12.87l.133-.263c.188-.377.39-.746.607-1.1A10.042 10.042 0 0 1 3.15 13.61a9.98 9.98 0 0 1 3.058-7.308A9.927 9.927 0 0 1 13.59 3.59c5.473.263 9.774 4.862 9.774 10.351v.18a9.93 9.93 0 0 1-2.585 6.57 9.98 9.98 0 0 1-6.242 3.278c-1.2.115-2.404.008-3.557-.303l-.31.652c-.103.22-.214.436-.333.646a12.108 12.108 0 0 0 5.864.516 12.07 12.07 0 0 0 7.564-4.97A12.125 12.125 0 0 0 25.364 14v-.218C25.296 5.948 19.397.068 12.081 0zm-.158 5.378a6.733 6.733 0 0 0-5.208 2.461 6.793 6.793 0 0 0-1.397 5.637 6.642 6.642 0 0 0 2.183 3.823c.327.29.68.55 1.053.776.12-.303.254-.6.401-.889a4.737 4.737 0 0 1-1.862-3.178 4.79 4.79 0 0 1 .898-3.574 4.67 4.67 0 0 1 6.293-1.06 4.67 4.67 0 0 1 1.863 2.792 4.787 4.787 0 0 1-.42 3.213 4.736 4.736 0 0 1-2.406 2.2c-.013.37-.05.738-.111 1.103a6.74 6.74 0 0 0 4.009-3.736 6.79 6.79 0 0 0-.426-5.86 6.726 6.726 0 0 0-4.87-3.708z" />
    </svg>
  );
}

const PLATFORM_META: Record<
  string,
  { label: string; Icon: React.ComponentType<{ className?: string }> }
> = {
  slack: { label: "Slack", Icon: SlackIcon },
  discord: { label: "Discord", Icon: DiscordIcon },
  teams: { label: "Microsoft Teams", Icon: MonitorSmartphone },
  telegram: { label: "Telegram", Icon: Send },
  mattermost: { label: "Mattermost", Icon: MattermostIcon },
  file: { label: "Uploaded files (CSV / TSV / JSONL)", Icon: FileText },
};

export function PlatformCard({ connection, onDisconnect, onManage, onEdit }: PlatformCardProps) {
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
                onClick={onEdit}
                title="Edit credentials"
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <KeyRound className="w-4 h-4" />
                Edit credentials
              </button>
            )}
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
                onClick={onEdit}
                title="Edit credentials"
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <KeyRound className="w-4 h-4" />
              </button>
            )}
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
