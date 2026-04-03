import { useState } from "react";
import { X, ArrowLeft, ArrowRight, CheckCircle2, Loader2, AlertCircle, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import { ChannelSelector } from "./ChannelSelector";
import { useCreateConnection } from "@/hooks/useConnections";
import { useConnectionChannels, useUpdateChannels } from "@/hooks/useConnections";
import type { PlatformConnection } from "@/lib/types";

type Platform = "slack" | "discord" | "teams" | "telegram";

interface ConnectionWizardProps {
  platform: Platform;
  onClose: () => void;
  onComplete: (connection: PlatformConnection) => void;
}

type Step = 1 | 2 | 3 | 4 | 5;

const SLACK_INSTRUCTIONS = [
  { text: "Go to", link: "https://api.slack.com/apps", linkText: "api.slack.com/apps" },
  { text: "Click Create New App → From scratch" },
  { text: "Under OAuth & Permissions, add Bot Token Scopes: channels:history, channels:read, groups:history, groups:read, users:read" },
  { text: "Click Install to Workspace and authorize" },
  { text: "Copy the Bot User OAuth Token (starts with xoxb-)" },
  { text: "Under Basic Information, copy the Signing Secret" },
];

const DISCORD_INSTRUCTIONS = [
  { text: "Go to", link: "https://discord.com/developers/applications", linkText: "discord.com/developers" },
  { text: "Click New Application and give it a name" },
  { text: "Copy the Application ID and Public Key from the General Information page" },
  { text: "Go to the Bot tab, click Reset Token, and copy the Bot Token" },
  { text: "Enable Message Content Intent and Server Members Intent under Privileged Gateway Intents" },
  { text: "Go to OAuth2 → URL Generator, select bot and applications.commands scopes" },
  { text: "Under Bot Permissions, enable: Send Messages, Read Message History, Add Reactions, Attach Files" },
  { text: "Copy the generated URL and open it to invite the bot to your server" },
];

const TEAMS_INSTRUCTIONS = [
  { text: "Go to", link: "https://portal.azure.com/#create/Microsoft.AzureBot", linkText: "Azure Portal" },
  { text: "Create a new Azure Bot resource — choose SingleTenant or MultiTenant" },
  { text: "Under Configuration, copy the Microsoft App ID" },
  { text: "Click Manage Password → New client secret and copy the value" },
  { text: "Note your Azure AD Tenant ID from the Azure Active Directory overview (required for SingleTenant)" },
  { text: "Under Channels, add the Microsoft Teams channel and save" },
  { text: "Set the messaging endpoint to your bot's webhook URL" },
];

const TELEGRAM_INSTRUCTIONS = [
  { text: "Open Telegram and search for", link: "https://t.me/BotFather", linkText: "@BotFather" },
  { text: "Send /newbot and follow the prompts to choose a name and username" },
  { text: "Copy the bot token provided by BotFather (e.g. 123456:ABC-DEF...)" },
  { text: "Optionally generate a webhook secret token for request verification" },
  { text: "Add the bot to your group chat and grant it admin permissions to read messages" },
];

const CREDENTIAL_FIELDS: Record<Platform, { key: string; label: string; placeholder: string; type?: string }[]> = {
  slack: [
    { key: "bot_token", label: "Bot Token", placeholder: "xoxb-...", type: "password" },
    { key: "signing_secret", label: "Signing Secret", placeholder: "Your app's signing secret", type: "password" },
  ],
  discord: [
    { key: "bot_token", label: "Bot Token", placeholder: "Your bot token", type: "password" },
    { key: "public_key", label: "Public Key", placeholder: "64-character hex string from General Information" },
    { key: "application_id", label: "Application ID", placeholder: "Your Discord application ID" },
    { key: "mention_role_ids", label: "Mention Role IDs (optional)", placeholder: "Comma-separated role IDs, e.g. 1234567890,9876543210" },
  ],
  teams: [
    { key: "app_id", label: "Microsoft App ID", placeholder: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" },
    { key: "app_password", label: "App Password (Client Secret)", placeholder: "Your Azure app client secret", type: "password" },
    { key: "app_tenant_id", label: "Azure AD Tenant ID", placeholder: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" },
    { key: "app_type", label: "App Type", placeholder: "MultiTenant" },
  ],
  telegram: [
    { key: "bot_token", label: "Bot Token", placeholder: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", type: "password" },
    { key: "secret_token", label: "Webhook Secret Token (optional)", placeholder: "Optional verification secret for webhook requests" },
  ],
};

export function ConnectionWizard({ platform, onClose, onComplete }: ConnectionWizardProps) {
  const [step, setStep] = useState<Step>(1);
  const [displayName, setDisplayName] = useState("");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [connection, setConnection] = useState<PlatformConnection | null>(null);
  const [selectedChannels, setSelectedChannels] = useState<string[]>([]);
  const [validationError, setValidationError] = useState<string | null>(null);

  const { create } = useCreateConnection();
  const { channels, loading: channelsLoading } = useConnectionChannels(connection?.id ?? null);
  const { updateChannels, loading: updatingChannels } = useUpdateChannels(connection?.id ?? null);

  const INSTRUCTIONS_MAP: Record<Platform, { text: string; link?: string; linkText?: string }[]> = {
    slack: SLACK_INSTRUCTIONS,
    discord: DISCORD_INSTRUCTIONS,
    teams: TEAMS_INSTRUCTIONS,
    telegram: TELEGRAM_INSTRUCTIONS,
  };
  const instructions = INSTRUCTIONS_MAP[platform];
  const fields = CREDENTIAL_FIELDS[platform];

  function handleCredentialChange(key: string, value: string) {
    setCredentials((prev) => ({ ...prev, [key]: value }));
  }

  async function handleValidate() {
    setValidationError(null);
    setStep(3);
    try {
      const conn = await create({
        platform,
        credentials,
        display_name: displayName.trim(),
      });
      setConnection(conn);
      setSelectedChannels(conn.selected_channels);
      setStep(4);
    } catch (err) {
      setValidationError(err instanceof Error ? err.message : "Validation failed");
      setStep(2);
    }
  }

  async function handleFinish() {
    if (!connection) return;
    try {
      await updateChannels(selectedChannels);
      onComplete({ ...connection, selected_channels: selectedChannels });
    } catch {
      // still close — channels can be updated later
      onComplete(connection);
    }
  }

  const credentialsFilled = fields.every((f) => (credentials[f.key] ?? "").trim().length > 0);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Dialog */}
      <div className="relative z-10 w-full max-w-lg bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="px-6 py-4 border-b border-border space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold text-foreground">
              Connect {{ slack: "Slack", discord: "Discord", teams: "Microsoft Teams", telegram: "Telegram" }[platform]}
            </h2>
            <button
              type="button"
              onClick={onClose}
              className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted transition-colors"
            >
              <X className="w-4 h-4 text-muted-foreground" />
            </button>
          </div>
          <StepIndicator current={step} />
        </div>

        {/* Content */}
        <div className="px-6 py-5">
          {step === 1 && (
            <StepInstructions
              platform={platform}
              instructions={instructions}
              displayName={displayName}
              onDisplayNameChange={setDisplayName}
            />
          )}
          {step === 2 && (
            <StepCredentials
              fields={fields}
              values={credentials}
              onChange={handleCredentialChange}
            />
          )}
          {step === 3 && (
            <StepValidating />
          )}
          {step === 4 && (
            <StepChannels
              channels={channels}
              selected={selectedChannels}
              onChange={setSelectedChannels}
              loading={channelsLoading}
              error={validationError}
              platform={platform}
            />
          )}
          {step === 5 && connection && (
            <StepConfirmation connection={connection} selectedChannels={selectedChannels} />
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-muted/30">
          <div>
            {step === 2 && (
              <button
                type="button"
                onClick={() => setStep(1)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <ArrowLeft className="w-4 h-4" />
                Back
              </button>
            )}
            {step === 4 && (
              <button
                type="button"
                onClick={() => setStep(2)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <ArrowLeft className="w-4 h-4" />
                Back
              </button>
            )}
          </div>
          <div className="flex gap-2">
            {validationError && step === 2 && (
              <div className="flex items-center gap-1.5 text-xs text-rose-600 dark:text-rose-400 mr-2">
                <AlertCircle className="w-3.5 h-3.5" />
                {validationError}
              </div>
            )}
            {step === 1 && (
              <button
                type="button"
                onClick={() => setStep(2)}
                disabled={!displayName.trim()}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:pointer-events-none"
              >
                Next
                <ArrowRight className="w-4 h-4" />
              </button>
            )}
            {step === 2 && (
              <button
                type="button"
                onClick={handleValidate}
                disabled={!credentialsFilled}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:pointer-events-none"
              >
                Validate
                <ArrowRight className="w-4 h-4" />
              </button>
            )}
            {step === 4 && (
              <button
                type="button"
                onClick={() => setStep(5)}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
              >
                Next
                <ArrowRight className="w-4 h-4" />
              </button>
            )}
            {step === 5 && (
              <button
                type="button"
                onClick={handleFinish}
                disabled={updatingChannels}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                {updatingChannels ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <CheckCircle2 className="w-4 h-4" />
                )}
                Start Ingestion
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// Sub-components

const STEP_LABELS: Record<Step, string> = {
  1: "Setup",
  2: "Credentials",
  3: "Validating",
  4: "Channels",
  5: "Done",
};

function StepIndicator({ current }: { current: Step }) {
  const visible: Step[] = [1, 2, 4, 5]; // skip 3 (transient validating state)
  return (
    <div className="flex items-center gap-1">
      {visible.map((s, i) => (
        <div key={s} className="flex items-center gap-1">
          {i > 0 && (
            <div
              className={cn(
                "w-4 h-px transition-colors",
                s <= current || (current === 3 && s === 4)
                  ? "bg-primary/40"
                  : "bg-muted-foreground/20",
              )}
            />
          )}
          <div
            className={cn(
              "flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium transition-colors",
              s === current || (current === 3 && s === 2)
                ? "bg-primary/15 text-primary"
                : s < current || (current === 3 && s < 2)
                  ? "text-primary/60"
                  : "text-muted-foreground/50",
            )}
          >
            <div
              className={cn(
                "w-4 h-4 rounded-full flex items-center justify-center text-[10px] font-bold transition-colors",
                s === current || (current === 3 && s === 2)
                  ? "bg-primary text-primary-foreground"
                  : s < current || (current === 3 && s < 2)
                    ? "bg-primary/20 text-primary"
                    : "bg-muted text-muted-foreground/60",
              )}
            >
              {s < current && current !== 3 ? (
                <CheckCircle2 className="w-3 h-3" />
              ) : (
                visible.indexOf(s) + 1
              )}
            </div>
            <span className="hidden sm:inline">{STEP_LABELS[s]}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function StepInstructions({
  platform,
  instructions,
  displayName,
  onDisplayNameChange,
}: {
  platform: Platform;
  instructions: { text: string; link?: string; linkText?: string }[];
  displayName: string;
  onDisplayNameChange: (v: string) => void;
}) {
  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Set up your {{ slack: "Slack", discord: "Discord", teams: "Microsoft Teams", telegram: "Telegram" }[platform]} app
        </h3>
        <p className="text-xs text-muted-foreground">Follow these steps before entering your credentials.</p>
      </div>
      <div className="space-y-1">
        {instructions.map((instruction, i) => (
          <div key={i} className="flex gap-3 items-start px-3 py-2.5 rounded-lg hover:bg-muted/40 transition-colors">
            <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/10 text-primary text-[11px] font-bold shrink-0 mt-0.5">
              {i + 1}
            </span>
            <span className="text-sm text-foreground/80 leading-relaxed">
              {instruction.text}{" "}
              {instruction.link && (
                <a
                  href={instruction.link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-0.5 text-primary hover:underline font-medium"
                >
                  {instruction.linkText}
                  <ExternalLink className="w-3 h-3" />
                </a>
              )}
            </span>
          </div>
        ))}
      </div>
      <div>
        <label className="block text-xs font-medium text-foreground mb-1.5">
          Display name
        </label>
        <input
          type="text"
          value={displayName}
          onChange={(e) => onDisplayNameChange(e.target.value)}
          placeholder={`e.g. ${{ slack: "Engineering Workspace", discord: "Community Server", teams: "Corp Tenant", telegram: "Alerts Bot" }[platform]}`}
          className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
        />
      </div>
    </div>
  );
}

function StepCredentials({
  fields,
  values,
  onChange,
}: {
  fields: { key: string; label: string; placeholder: string; type?: string }[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">Enter your credentials</h3>
        <p className="text-xs text-muted-foreground">These are stored securely and never shared.</p>
      </div>
      {fields.map((field) => (
        <div key={field.key}>
          <label className="block text-xs font-medium text-foreground mb-1.5">{field.label}</label>
          <input
            type={field.type ?? "text"}
            value={values[field.key] ?? ""}
            onChange={(e) => onChange(field.key, e.target.value)}
            placeholder={field.placeholder}
            className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20 font-mono"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
      ))}
    </div>
  );
}

function StepValidating() {
  return (
    <div className="flex flex-col items-center justify-center py-12 gap-4">
      <div className="relative">
        <div className="w-14 h-14 rounded-2xl bg-primary/10 flex items-center justify-center">
          <Loader2 className="w-7 h-7 text-primary animate-spin" />
        </div>
      </div>
      <div className="text-center">
        <p className="text-sm font-medium text-foreground">Validating credentials</p>
        <p className="text-xs text-muted-foreground mt-1">Connecting to your platform and verifying access.</p>
      </div>
    </div>
  );
}

function StepChannels({
  channels,
  selected,
  onChange,
  loading,
  error,
  platform,
}: {
  channels: import("@/lib/types").AvailableChannel[];
  selected: string[];
  onChange: (v: string[]) => void;
  loading: boolean;
  error: string | null;
  platform: Platform;
}) {
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">Select channels to monitor</h3>
        <p className="text-xs text-muted-foreground">
          {platform === "teams" || platform === "telegram"
            ? "Teams and Telegram bots are event-driven — messages are ingested in real time as they arrive via webhook."
            : "Choose which channels Beever will ingest messages from."}
        </p>
      </div>
      {loading ? (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="w-6 h-6 text-primary animate-spin" />
        </div>
      ) : error ? (
        <div className="flex items-center gap-2 rounded-lg bg-rose-500/10 border border-rose-500/20 px-3 py-2.5">
          <AlertCircle className="w-4 h-4 text-rose-500 shrink-0" />
          <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p>
        </div>
      ) : (
        <ChannelSelector channels={channels} selected={selected} onChange={onChange} />
      )}
    </div>
  );
}

function StepConfirmation({
  connection,
  selectedChannels,
}: {
  connection: PlatformConnection;
  selectedChannels: string[];
}) {
  return (
    <div className="space-y-5">
      <div className="flex flex-col items-center py-4 gap-3">
        <div className="w-14 h-14 rounded-2xl bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center">
          <CheckCircle2 className="w-7 h-7 text-emerald-500" />
        </div>
        <div className="text-center">
          <h3 className="text-sm font-semibold text-foreground">Ready to go!</h3>
          <p className="text-xs text-muted-foreground mt-1">
            {connection.display_name || connection.platform} is connected and ready for ingestion.
          </p>
        </div>
      </div>
      <div className="rounded-xl border border-border divide-y divide-border overflow-hidden">
        <div className="px-4 py-3 flex justify-between text-sm">
          <span className="text-muted-foreground">Platform</span>
          <span className="font-medium text-foreground capitalize">{connection.platform}</span>
        </div>
        {connection.display_name && (
          <div className="px-4 py-3 flex justify-between text-sm">
            <span className="text-muted-foreground">Name</span>
            <span className="font-medium text-foreground">{connection.display_name}</span>
          </div>
        )}
        <div className="px-4 py-3 flex justify-between text-sm">
          <span className="text-muted-foreground">Channels selected</span>
          <span className="font-medium text-foreground">{selectedChannels.length}</span>
        </div>
      </div>
    </div>
  );
}
