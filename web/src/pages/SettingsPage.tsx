import { useState } from "react";
import { Plus, MessageSquare, MonitorSmartphone, Send, ChevronDown, ChevronRight, Settings2 } from "lucide-react";
import { useConnections, useDeleteConnection } from "@/hooks/useConnections";
import { PlatformCard } from "@/components/settings/PlatformCard";
import { ConnectionWizard } from "@/components/settings/ConnectionWizard";
import { ManageChannelsDialog } from "@/components/settings/ManageChannelsDialog";
import { SyncDefaultsSection } from "@/components/settings/SyncDefaultsSection";
import { AgentModelSettings } from "@/components/settings/AgentModelSettings";
import type { PlatformConnection } from "@/lib/types";

type Platform = "slack" | "discord" | "teams" | "telegram";

function SlackIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zM18.956 8.834a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zm-1.27 0a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.163 0a2.528 2.528 0 0 1 2.523 2.522v6.312zM15.163 18.956a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.163 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zm0-1.27a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.315A2.528 2.528 0 0 1 24 15.163a2.528 2.528 0 0 1-2.522 2.523h-6.315z" />
    </svg>
  );
}

const PLATFORM_OPTIONS: { value: Platform; label: string; description: string; Icon: React.ComponentType<{ className?: string }> }[] = [
  { value: "slack", label: "Slack", description: "Connect a Slack workspace", Icon: SlackIcon },
  { value: "discord", label: "Discord", description: "Connect a Discord server", Icon: MessageSquare },
  { value: "teams", label: "Microsoft Teams", description: "Connect a Teams tenant", Icon: MonitorSmartphone },
  { value: "telegram", label: "Telegram", description: "Connect a Telegram bot", Icon: Send },
];

export function SettingsPage() {
  const { connections, loading, error, refetch } = useConnections();
  const { remove } = useDeleteConnection();

  const [wizardPlatform, setWizardPlatform] = useState<Platform | null>(null);
  const [managingConnection, setManagingConnection] = useState<PlatformConnection | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  const [showDefaults, setShowDefaults] = useState(false);

  async function handleDisconnect(connection: PlatformConnection) {
    if (!confirm(`Disconnect "${connection.display_name || connection.platform}"? This cannot be undone.`)) return;
    try {
      await remove(connection.id);
      refetch();
      window.dispatchEvent(new Event("connections-changed"));
    } catch {
      // error shown by hook
    }
  }

  function handleWizardComplete(_connection: PlatformConnection) {
    setWizardPlatform(null);
    refetch();
    window.dispatchEvent(new Event("connections-changed"));
  }

  function handleManageComplete() {
    setManagingConnection(null);
    refetch();
    window.dispatchEvent(new Event("connections-changed"));
  }

  const connectedCount = connections.filter((c) => c.status === "connected").length;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      {/* Page header */}
      <div className="mb-8 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground tracking-tight">Integrations</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Connect your team communication platforms to start building knowledge.
          </p>
          {!loading && connectedCount > 0 && (
            <div className="mt-3 inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/20">
              <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
              <span className="text-xs font-medium text-emerald-600 dark:text-emerald-400">
                {connectedCount} connection{connectedCount !== 1 ? "s" : ""} active
              </span>
            </div>
          )}
        </div>
        {!loading && connections.length > 0 && (
          <button
            type="button"
            onClick={() => setShowPicker(true)}
            className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors shrink-0"
          >
            <Plus className="w-4 h-4" />
            Add Connection
          </button>
        )}
      </div>

      {error && (
        <div className="mb-6 rounded-lg border border-rose-200 dark:border-rose-900 bg-rose-50 dark:bg-rose-950/30 px-4 py-3 text-sm text-rose-700 dark:text-rose-300">
          Failed to load connections: {error}
        </div>
      )}

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-48 rounded-2xl bg-muted/40 animate-pulse" />
          ))}
        </div>
      ) : connections.length === 0 ? (
        /* Empty state */
        <div className="flex flex-col items-center justify-center py-16 px-6 rounded-2xl border-2 border-dashed border-border">
          <div className="flex items-center gap-3 mb-5">
            {PLATFORM_OPTIONS.map(({ value, Icon }) => (
              <div key={value} className="w-10 h-10 rounded-xl bg-muted/60 flex items-center justify-center text-muted-foreground">
                <Icon className="w-5 h-5" />
              </div>
            ))}
          </div>
          <h2 className="text-lg font-semibold text-foreground mb-1">No connections yet</h2>
          <p className="text-sm text-muted-foreground text-center max-w-md mb-6">
            Connect a communication platform to start ingesting messages and building your team's knowledge graph.
          </p>
          <button
            type="button"
            onClick={() => setShowPicker(true)}
            className="inline-flex items-center gap-1.5 px-5 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
          >
            <Plus className="w-4 h-4" />
            Add Your First Connection
          </button>
        </div>
      ) : (
        /* Connection list */
        <div className="grid gap-4 sm:grid-cols-2">
          {connections.map((connection) => (
            <PlatformCard
              key={connection.id}
              connection={connection}
              onDisconnect={() => handleDisconnect(connection)}
              onManage={() => setManagingConnection(connection)}
            />
          ))}
        </div>
      )}

      {/* Default channel settings — collapsed by default */}
      <div className="mt-8 rounded-2xl border border-border bg-card overflow-hidden">
        <button
          type="button"
          onClick={() => setShowDefaults(!showDefaults)}
          className="flex w-full items-center justify-between px-5 py-4 text-left hover:bg-muted/30 transition-colors"
        >
          <div className="flex items-center gap-2">
            <Settings2 className="h-4 w-4 text-muted-foreground" />
            <div>
              <span className="text-sm font-medium text-foreground">Default Channel Settings</span>
              {!showDefaults && (
                <span className="text-[11px] text-muted-foreground ml-2">
                  Applied to new channels automatically
                </span>
              )}
            </div>
          </div>
          {showDefaults
            ? <ChevronDown className="h-4 w-4 text-muted-foreground" />
            : <ChevronRight className="h-4 w-4 text-muted-foreground" />
          }
        </button>
        {showDefaults && (
          <div className="border-t border-border px-1">
            <SyncDefaultsSection />
          </div>
        )}
      </div>

      {/* AI Model configuration */}
      <div className="mt-4 rounded-2xl border border-border bg-card overflow-hidden p-5">
        <AgentModelSettings />
      </div>

      {/* Platform picker dialog */}
      {showPicker && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={() => setShowPicker(false)} />
          <div className="relative z-10 w-full max-w-md bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
            <div className="px-6 py-4 border-b border-border">
              <h2 className="text-base font-semibold text-foreground">Choose a platform</h2>
              <p className="text-xs text-muted-foreground mt-0.5">Select the platform you want to connect.</p>
            </div>
            <div className="p-3">
              {PLATFORM_OPTIONS.map(({ value, label, description, Icon }) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => {
                    setShowPicker(false);
                    setWizardPlatform(value);
                  }}
                  className="w-full flex items-center gap-4 px-4 py-3 rounded-xl text-left hover:bg-muted/50 transition-colors"
                >
                  <div className="w-10 h-10 rounded-xl bg-muted/60 flex items-center justify-center shrink-0">
                    <Icon className="w-5 h-5 text-foreground" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-foreground">{label}</div>
                    <div className="text-xs text-muted-foreground">{description}</div>
                  </div>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Connection wizard */}
      {wizardPlatform && (
        <ConnectionWizard
          platform={wizardPlatform}
          onClose={() => setWizardPlatform(null)}
          onComplete={handleWizardComplete}
        />
      )}

      {/* Manage channels dialog */}
      {managingConnection && (
        <ManageChannelsDialog
          connection={managingConnection}
          onClose={handleManageComplete}
        />
      )}
    </div>
  );
}
