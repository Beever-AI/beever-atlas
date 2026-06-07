import { useState } from "react";
import { X, Loader2, AlertCircle, KeyRound } from "lucide-react";
import { useUpdateCredentials } from "@/hooks/useConnections";
import { CREDENTIAL_FIELDS } from "./ConnectionWizard";
import type { PlatformConnection } from "@/lib/types";
import type { Platform } from "./ConnectionWizard";

interface EditCredentialsDialogProps {
  connection: PlatformConnection;
  onClose: () => void;
  onSaved: () => void;
}

const PLATFORM_LABELS: Record<Platform, string> = {
  slack: "Slack",
  discord: "Discord",
  teams: "Microsoft Teams",
  telegram: "Telegram",
  mattermost: "Mattermost",
};

export function EditCredentialsDialog({ connection, onClose, onSaved }: EditCredentialsDialogProps) {
  const [values, setValues] = useState<Record<string, string>>({});
  const { update, loading, error } = useUpdateCredentials();

  // Only render for platforms that have credential fields
  const platform = connection.platform as Platform;
  const fields = CREDENTIAL_FIELDS[platform] ?? [];

  const anyFilled = fields.some((f) => (values[f.key] ?? "").trim().length > 0);

  function handleChange(key: string, value: string) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  async function handleSave() {
    const filled: Record<string, string> = {};
    for (const f of fields) {
      const v = (values[f.key] ?? "").trim();
      if (v) filled[f.key] = v;
    }
    await update(connection.id, filled);
    onSaved();
  }

  const platformLabel = PLATFORM_LABELS[platform] ?? connection.platform;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Dialog */}
      <div className="relative z-10 flex flex-col w-full max-w-lg max-h-[90vh] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="shrink-0 px-6 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-muted-foreground" />
            <h2 className="text-base font-semibold text-foreground">
              Edit {platformLabel} credentials
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted transition-colors"
          >
            <X className="w-4 h-4 text-muted-foreground" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-5 space-y-4">
          <p className="text-xs text-muted-foreground">
            Leave any field blank to keep its current value. Only filled fields will be updated.
          </p>

          {fields.map((field) => {
            const isAppToken = field.key === "app_token" && platform === "slack";
            return (
              <div key={field.key}>
                <label className="block text-xs font-medium text-foreground mb-1.5">
                  {field.label}
                  {field.optional && (
                    <span className="ml-1 text-muted-foreground font-normal">(optional)</span>
                  )}
                </label>
                {field.enum ? (
                  <select
                    value={values[field.key] ?? ""}
                    onChange={(e) => handleChange(field.key, e.target.value)}
                    className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/20 font-mono"
                  >
                    <option value="">— keep current —</option>
                    {field.enum.map((opt) => (
                      <option key={opt} value={opt}>{opt}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="password"
                    value={values[field.key] ?? ""}
                    onChange={(e) => handleChange(field.key, e.target.value)}
                    placeholder={field.placeholder}
                    className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20 font-mono"
                    autoComplete="new-password"
                    spellCheck={false}
                  />
                )}
                {isAppToken ? (
                  // Emphasize the Socket Mode token; prefer the shared field hint.
                  <p className="text-[11px] text-primary/80 mt-1 leading-snug">
                    {field.hint ??
                      "Add your App-Level Token (xapp-…) to switch this workspace to Socket Mode — no public URL/tunnel needed, survives restarts."}
                  </p>
                ) : field.hint ? (
                  // Surface the same setup guidance the wizard shows (e.g. Teams
                  // tenant_id is required). The "leave blank = keep" note at the
                  // top of the body covers fields without a hint.
                  <p className="text-[11px] text-muted-foreground/85 mt-1 leading-snug">
                    {field.hint}
                  </p>
                ) : null}
              </div>
            );
          })}

          {error && (
            <div className="flex items-center gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 px-3 py-2.5">
              <AlertCircle className="w-4 h-4 text-rose-500 shrink-0" />
              <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="shrink-0 flex items-center justify-end gap-2 px-6 py-4 border-t border-border bg-muted/30">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!anyFilled || loading}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:pointer-events-none"
          >
            {loading ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <KeyRound className="w-4 h-4" />
            )}
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
