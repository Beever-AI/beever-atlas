import { useState } from "react";
import { NavLink, Outlet, useLocation, useOutletContext } from "react-router-dom";
import {
  Plus,
  MonitorSmartphone,
  Settings2,
  FileText,
  Plug,
  Cpu,
  Layers,
  KeyRound,
} from "lucide-react";
import { useConnections, useDeleteConnection } from "@/hooks/useConnections";
import { PlatformCard } from "@/components/settings/PlatformCard";
import { ConnectionWizard } from "@/components/settings/ConnectionWizard";
import { FileImportWizard } from "@/components/settings/FileImportWizard";
import { ManageChannelsDialog } from "@/components/settings/ManageChannelsDialog";
import { EditCredentialsDialog } from "@/components/settings/EditCredentialsDialog";
import { ConfirmRemoveDialog } from "@/components/settings/ConfirmRemoveDialog";
import type { PlatformConnection } from "@/lib/types";

type Platform = "slack" | "discord" | "teams" | "telegram" | "mattermost";
type PickerOption = Platform | "file";

/** Tab slug as it appears in the URL: ``/settings/<slug>``. */
export type SettingsTab = "integrations" | "channels" | "endpoints" | "embedding" | "agents";

/** Shape passed down to tab route elements via ``<Outlet context>``. */
export type SettingsOutletContext = {
  loading: boolean;
  error: string | null;
  connections: PlatformConnection[];
  onAdd: () => void;
  onDisconnect: (c: PlatformConnection) => void;
  onManage: (c: PlatformConnection) => void;
  onEdit: (c: PlatformConnection) => void;
};

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

const PLATFORM_OPTIONS: { value: PickerOption; label: string; description: string; Icon: React.ComponentType<{ className?: string }> }[] = [
  { value: "slack", label: "Slack", description: "Connect a Slack workspace", Icon: SlackIcon },
  { value: "discord", label: "Discord", description: "Connect a Discord server", Icon: DiscordIcon },
  { value: "teams", label: "Microsoft Teams", description: "Connect a Teams tenant", Icon: MonitorSmartphone },
  { value: "mattermost", label: "Mattermost", description: "Connect a Mattermost server", Icon: MattermostIcon },
  { value: "file", label: "File Import", description: "Upload a CSV / TSV / JSONL chat export", Icon: FileText },
];

const TABS: { value: SettingsTab; label: string; description: string; Icon: React.ComponentType<{ className?: string }> }[] = [
  { value: "integrations", label: "Integrations", description: "Connected platforms and data sources", Icon: Plug },
  { value: "channels", label: "Channels", description: "Default sync behavior for new channels", Icon: Settings2 },
  {
    value: "endpoints",
    label: "Endpoints",
    description: "Model providers you've connected — API endpoints, local Ollama, credentials",
    Icon: KeyRound,
  },
  {
    value: "embedding",
    label: "Embedding",
    description: "Vector model for semantic search — changing it re-embeds everything",
    Icon: Layers,
  },
  {
    value: "agents",
    label: "Agent models",
    description: "Which model each ingestion / wiki / Ask agent uses",
    Icon: Cpu,
  },
];

const DEFAULT_TAB: SettingsTab = "integrations";

function activeTabFromPath(pathname: string): SettingsTab {
  const slug = pathname.split("/")[2] as SettingsTab | undefined;
  return TABS.some((t) => t.value === slug) ? (slug as SettingsTab) : DEFAULT_TAB;
}

export function SettingsPage() {
  const { connections, loading, error, refetch } = useConnections();
  const { remove } = useDeleteConnection();
  const location = useLocation();

  const [wizardPlatform, setWizardPlatform] = useState<Platform | null>(null);
  const [showFileImport, setShowFileImport] = useState(false);
  const [managingConnection, setManagingConnection] = useState<PlatformConnection | null>(null);
  const [showPicker, setShowPicker] = useState(false);
  const [removingConnection, setRemovingConnection] = useState<PlatformConnection | null>(null);
  const [editingConnection, setEditingConnection] = useState<PlatformConnection | null>(null);

  function handleDisconnect(connection: PlatformConnection) {
    setRemovingConnection(connection);
  }

  function handleEdit(connection: PlatformConnection) {
    setEditingConnection(connection);
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
  const activeTab = activeTabFromPath(location.pathname);
  const activeMeta = TABS.find((t) => t.value === activeTab)!;
  const onIntegrations = activeTab === "integrations";

  const outletContext: SettingsOutletContext = {
    loading,
    error,
    connections,
    onAdd: () => setShowPicker(true),
    onDisconnect: handleDisconnect,
    onManage: setManagingConnection,
    onEdit: handleEdit,
  };

  return (
    <div className="h-full overflow-auto">
      <div className="p-6 max-w-6xl mx-auto">
        {/* Page header */}
        <div className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-foreground tracking-tight">Settings</h1>
            <p className="text-sm text-muted-foreground mt-1">{activeMeta.description}</p>
          </div>
          {onIntegrations && !loading && connections.length > 0 && (
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

        {/* Horizontal tabs */}
        <div className="mb-6 flex items-center justify-between border-b border-border">
          <nav className="flex gap-1 -mb-px">
            {TABS.map(({ value, label, Icon }) => (
              <NavLink
                key={value}
                to={`/settings/${value}`}
                className={({ isActive }) =>
                  `inline-flex items-center gap-2 px-4 py-2.5 text-sm border-b-2 transition-colors ${
                    isActive
                      ? "border-primary text-foreground font-medium"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  }`
                }
              >
                <Icon className="w-4 h-4" />
                {label}
              </NavLink>
            ))}
          </nav>
          {onIntegrations && !loading && connectedCount > 0 && (
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20 mb-2">
              <div className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
              <span className="text-xs font-medium text-emerald-600 dark:text-emerald-400">
                {connectedCount} active
              </span>
            </div>
          )}
        </div>

        <Outlet context={outletContext} />
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
                    if (value === "file") {
                      setShowFileImport(true);
                    } else {
                      setWizardPlatform(value as Platform);
                    }
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

      {wizardPlatform && (
        <ConnectionWizard
          platform={wizardPlatform}
          onClose={() => setWizardPlatform(null)}
          onComplete={handleWizardComplete}
        />
      )}

      {showFileImport && (
        <FileImportWizard
          onClose={() => setShowFileImport(false)}
          onComplete={() => {
            setShowFileImport(false);
            refetch();
            window.dispatchEvent(new Event("connections-changed"));
          }}
        />
      )}

      {managingConnection && (
        <ManageChannelsDialog
          connection={managingConnection}
          onClose={handleManageComplete}
        />
      )}

      {removingConnection && (
        <ConfirmRemoveDialog
          connection={removingConnection}
          onCancel={() => setRemovingConnection(null)}
          onConfirm={async (cascade) => {
            try {
              await remove(removingConnection.id, cascade);
              refetch();
              window.dispatchEvent(new Event("connections-changed"));
            } catch {
              // error shown by hook
            } finally {
              setRemovingConnection(null);
            }
          }}
        />
      )}

      {editingConnection && (
        <EditCredentialsDialog
          connection={editingConnection}
          onClose={() => setEditingConnection(null)}
          onSaved={() => {
            setEditingConnection(null);
            refetch();
            window.dispatchEvent(new Event("connections-changed"));
          }}
        />
      )}
    </div>
  );
}

/** The Integrations tab content — a route element under ``/settings/integrations``.
 *  Pulls connection state + handlers from the parent ``SettingsPage`` via outlet context. */
export function IntegrationsTab() {
  const { loading, error, connections, onAdd, onDisconnect, onManage, onEdit } =
    useOutletContext<SettingsOutletContext>();
  return (
    <>
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
            onClick={onAdd}
            className="inline-flex items-center gap-1.5 px-5 py-2.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
          >
            <Plus className="w-4 h-4" />
            Add Your First Connection
          </button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {connections.map((connection) => (
            <PlatformCard
              key={connection.id}
              connection={connection}
              onDisconnect={() => onDisconnect(connection)}
              onManage={() => onManage(connection)}
              onEdit={() => onEdit(connection)}
            />
          ))}
        </div>
      )}
    </>
  );
}
