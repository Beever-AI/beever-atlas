import { useState, useEffect } from "react";
import { X, ArrowLeft, ArrowRight, CheckCircle2, Loader2, AlertCircle, ExternalLink, Zap, Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { ChannelSelector } from "./ChannelSelector";
import { useCreateConnection } from "@/hooks/useConnections";
import { useConnectionChannels, useUpdateChannels } from "@/hooks/useConnections";
import type { PlatformConnection } from "@/lib/types";

type Platform = "slack" | "discord" | "teams" | "telegram" | "mattermost";

interface ConnectionWizardProps {
  platform: Platform;
  onClose: () => void;
  onComplete: (connection: PlatformConnection) => void;
}

type Step = 1 | 2 | 3 | 4 | 5;

const SLACK_INSTRUCTIONS = [
  { text: "Go to", link: "https://api.slack.com/apps", linkText: "api.slack.com/apps" },
  { text: "Create New App → From scratch, then open your app." },
  {
    text: "OAuth & Permissions → Bot Token Scopes — add:",
    details: [
      "channels:history",
      "channels:read",
      "files:read",
      "groups:history",
      "groups:read",
      "users:read",
    ],
  },
  { text: "Install to Workspace and authorize." },
  { text: "OAuth & Permissions → copy the Bot User OAuth Token (starts with xoxb-)." },
  {
    text: "Pick how Slack delivers events — Socket Mode is recommended (no public URL, survives restarts):",
    details: [
      "Socket Mode → Settings → Socket Mode (enable) → Basic Information → App-Level Tokens → generate with scope connections:write → copy the xapp- token",
      "Events API → Basic Information → copy the Signing Secret, then set the Request URL shown above",
    ],
  },
  {
    text: "Event Subscriptions → Subscribe to bot events — add (both modes):",
    details: ["app_mention", "message.channels", "message.groups"],
  },
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
  {
    text: "Create an Azure Bot resource",
    link: "https://portal.azure.com/#create/Microsoft.AzureBot",
    linkText: "in Azure Portal",
    details: ["App type: SingleTenant (recommended) or MultiTenant"],
  },
  {
    text: "Expose this bridge over HTTPS, then set the Bot's Messaging endpoint to your URL + /api/teams",
    details: [
      "Local dev: ngrok http 3001",
      "https://<your-host>/api/teams",
    ],
  },
  { text: "Copy the Microsoft App ID from Bot → Configuration" },
  { text: "On the linked App Registration → Manage Password, create a client secret and copy the VALUE (shown once)" },
  { text: "Copy the Tenant ID from Azure Active Directory → Overview" },
  { text: "On the Bot resource → Channels, add the Microsoft Teams channel" },
  {
    text: "On API permissions, add Microsoft Graph application permission, then Grant admin consent — a Global Admin must do this",
    details: ["Channel.ReadBasic.All"],
  },
];

const TELEGRAM_INSTRUCTIONS = [
  { text: "Open Telegram and search for", link: "https://t.me/BotFather", linkText: "@BotFather" },
  { text: "Send /newbot and follow the prompts to choose a name and username" },
  { text: "Copy the bot token provided by BotFather (e.g. 123456:ABC-DEF...)" },
  { text: "Optionally generate a webhook secret token for request verification" },
  { text: "Add the bot to your group chat and grant it admin permissions to read messages" },
];

const MATTERMOST_INSTRUCTIONS = [
  { text: "In Mattermost, go to System Console > Integrations > Bot Accounts and create a new bot. Copy the generated access token" },
  { text: "Ensure your Mattermost server allows bot accounts and has the REST API and WebSocket gateway accessible (enabled by default)" },
  { text: "Add the bot user to any channels where it should read from. The bot will only receive events from channels it is a member of" },
];

interface CredentialField {
  key: string;
  label: string;
  placeholder: string;
  type?: string;
  optional?: boolean;
  /** When present, render as a `<select>` with these options instead of a
   *  free-text input. Eliminates the historical typo class on `app_type`
   *  (the Teams adapter rejects anything other than the exact strings
   *  "SingleTenant" or "MultiTenant" — a leading space or lowercase letter
   *  silently silently produces a MSAL `missing_tenant_id_error`). */
  enum?: string[];
  /** Pre-fills the credential when the user first reaches the credentials
   *  step. Only honoured when `enum` is also set — keeps tokens/secrets
   *  empty by default. */
  default?: string;
  /** Optional helper text rendered under the input. */
  hint?: string;
  /** Synchronous client-side validator. Return null when OK, or a short
   *  error string. Empty values are NOT passed in — the required-field
   *  gate is handled separately by `credentialsFilled`. */
  validate?: (value: string) => string | null;
}

/** AAD GUIDs are 8-4-4-4-12 hex. Without this check users routinely paste
 *  a display name like "Teams" into the App ID field; the wizard's
 *  validate step (which doesn't actually mint a Graph token) accepts it,
 *  then the bot fails later with AADSTS700016. */
const AAD_GUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function validateAadGuid(label: string) {
  return (value: string): string | null =>
    AAD_GUID_RE.test(value.trim())
      ? null
      : `${label} must look like xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`;
}

const CREDENTIAL_FIELDS: Record<Platform, CredentialField[]> = {
  slack: [
    { key: "bot_token", label: "Bot Token", placeholder: "xoxb-...", type: "password" },
    {
      key: "app_token",
      label: "App-Level Token (Socket Mode — recommended)",
      placeholder: "xapp-...",
      type: "password",
      optional: true,
      hint: "Recommended for local/self-hosted: Socket Mode uses an outbound connection, so no public URL or tunnel is needed and it keeps working after restarts. Generate under Basic Information → App-Level Tokens with the connections:write scope.",
    },
    {
      key: "signing_secret",
      label: "Signing Secret (Events API)",
      placeholder: "Your app's signing secret",
      type: "password",
      optional: true,
      hint: "Only needed if you are NOT using Socket Mode. Events API requires a public Request URL (a tunnel in local dev).",
    },
  ],
  discord: [
    { key: "bot_token", label: "Bot Token", placeholder: "Your bot token", type: "password" },
    { key: "public_key", label: "Public Key", placeholder: "64-character hex string from General Information" },
    { key: "application_id", label: "Application ID", placeholder: "Your Discord application ID" },
    { key: "mention_role_ids", label: "Mention Role IDs (optional)", placeholder: "Comma-separated role IDs, e.g. 1234567890,9876543210", optional: true },
  ],
  teams: [
    {
      key: "app_id",
      label: "Microsoft App ID",
      placeholder: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      validate: validateAadGuid("Microsoft App ID"),
    },
    { key: "app_password", label: "App Password (Client Secret)", placeholder: "Your Azure app client secret", type: "password" },
    {
      key: "app_tenant_id",
      label: "Azure AD Tenant ID",
      placeholder: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      hint: "Required. The adapter cannot fetch message history without this, and SingleTenant bots reject the client_credentials token without it.",
      validate: validateAadGuid("Azure AD Tenant ID"),
    },
    {
      key: "app_type",
      label: "App Type",
      placeholder: "SingleTenant",
      enum: ["SingleTenant", "MultiTenant"],
      default: "SingleTenant",
      hint: "Both modes are supported. SingleTenant is recommended for org-internal bots and is the safer default. MultiTenant is for bots installed into multiple tenants (ISV / public scenarios).",
    },
  ],
  telegram: [
    { key: "bot_token", label: "Bot Token", placeholder: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11", type: "password" },
    { key: "secret_token", label: "Webhook Secret Token (optional)", placeholder: "Optional verification secret for webhook requests", optional: true },
  ],
  mattermost: [
    { key: "base_url", label: "Server URL", placeholder: "https://your-mattermost.com" },
    { key: "bot_token", label: "Bot Token", placeholder: "Your bot access token", type: "password" },
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

  const INSTRUCTIONS_MAP: Record<Platform, { text: string; link?: string; linkText?: string; details?: string[] }[]> = {
    slack: SLACK_INSTRUCTIONS,
    discord: DISCORD_INSTRUCTIONS,
    teams: TEAMS_INSTRUCTIONS,
    telegram: TELEGRAM_INSTRUCTIONS,
    mattermost: MATTERMOST_INSTRUCTIONS,
  };
  const instructions = INSTRUCTIONS_MAP[platform];
  const fields = CREDENTIAL_FIELDS[platform];

  // Telegram has no channel listing API and stays webhook-only. Teams used
  // to be in this bucket too, but PR #206 added Graph-based channel
  // enumeration (TeamsBridge.listChannels → GET /teams/{id}/channels), so
  // the Channels step now renders the real channel list for Teams.
  const isWebhookOnly = platform === "telegram";

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

  const credentialsFilled =
    fields.every((f) => f.optional || (credentials[f.key] ?? "").trim().length > 0) &&
    // Slack accepts EITHER an app-level token (Socket Mode) OR a signing
    // secret (Events API) — both are marked optional individually, so require
    // at least one here.
    (platform !== "slack" ||
      (credentials.app_token ?? "").trim().length > 0 ||
      (credentials.signing_secret ?? "").trim().length > 0);
  // Disable Validate when any FILLED field fails its own validator. Empty
  // fields are handled by `credentialsFilled`; we don't double-report.
  const credentialsValid = fields.every((f) => {
    const v = (credentials[f.key] ?? "").trim();
    return !v || !f.validate || f.validate(v) === null;
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      />

      {/* Dialog — flex column capped at the viewport so a tall step (e.g. the
          Slack instructions) scrolls in the middle instead of pushing the
          footer off-screen / overlapping it. */}
      <div className="relative z-10 flex flex-col w-full max-w-3xl max-h-[90vh] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden">
        {/* Header */}
        <div className="shrink-0 px-6 py-4 border-b border-border space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold text-foreground">
              Connect {{ slack: "Slack", discord: "Discord", teams: "Microsoft Teams", telegram: "Telegram", mattermost: "Mattermost" }[platform]}
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

        {/* Content — scrolls independently; min-h-0 lets it shrink within the
            flex column so overflow-y actually engages. */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-5">
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
            isWebhookOnly ? (
              <StepWebhookMode platform={platform} />
            ) : (
              <StepChannels
                channels={channels}
                selected={selectedChannels}
                onChange={setSelectedChannels}
                loading={channelsLoading}
                error={validationError}
                platform={platform}
              />
            )
          )}
          {step === 5 && connection && (
            <StepConfirmation connection={connection} selectedChannels={selectedChannels} />
          )}
        </div>

        {/* Footer */}
        <div className="shrink-0 flex items-center justify-between px-6 py-4 border-t border-border bg-muted/30">
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
                disabled={!credentialsFilled || !credentialsValid}
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

interface ConnectivityConfig {
  public_bot_url: string;
  configured: boolean;
  webhooks: { slack: string; teams: string };
}

/** Shows the live public webhook URL the user must paste into Slack's Request
 *  URL / Teams' messaging endpoint, fetched from /api/config/connectivity.
 *  When PUBLIC_BOT_URL is unset it explains how to configure connectivity
 *  instead of leaving the user guessing. Only relevant for inbound-webhook
 *  platforms (Slack Events API, Teams); harmless for Slack Socket Mode. */
function WebhookUrlCallout({ platform }: { platform: "slack" | "teams" }) {
  const [cfg, setCfg] = useState<ConnectivityConfig | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .get<ConnectivityConfig>("/api/config/connectivity")
      .then((c) => alive && setCfg(c))
      .catch(() => alive && setCfg(null));
    return () => {
      alive = false;
    };
  }, []);

  const url = cfg?.webhooks?.[platform] ?? "";
  const label =
    platform === "slack" ? "Slack Event Subscriptions → Request URL" : "Teams bot Messaging endpoint";

  async function copy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard may be blocked; the URL is still shown for manual copy */
    }
  }

  return (
    <div className="rounded-lg border border-border bg-muted/40 px-3 py-2.5 space-y-1.5">
      <p className="text-xs font-medium text-foreground">
        {platform === "slack" ? "Public URL (Events API only — skip for Socket Mode)" : "Public messaging endpoint"}
      </p>
      {cfg?.configured && url ? (
        <>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-xs font-mono text-foreground/90 bg-background border border-border rounded px-2 py-1 truncate">
              {url}
            </code>
            <button
              type="button"
              onClick={copy}
              className="shrink-0 inline-flex items-center gap-1 text-xs text-primary hover:underline"
            >
              {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="text-[11px] text-muted-foreground">Paste this as your {label}.</p>
        </>
      ) : (
        <p className="text-[11px] text-muted-foreground">
          No public URL is configured. Set <code className="font-mono">PUBLIC_BOT_URL</code> (a tunnel like
          {" "}<code className="font-mono">ngrok http 3001</code> in local dev, or your public domain in production) so
          this shows the exact {label} to paste.
          {platform === "slack" && " Or use Socket Mode (App-Level Token) to skip public URLs entirely."}
        </p>
      )}
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
  instructions: { text: string; link?: string; linkText?: string; details?: string[] }[];
  displayName: string;
  onDisplayNameChange: (v: string) => void;
}) {
  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Set up your {{ slack: "Slack", discord: "Discord", teams: "Microsoft Teams", telegram: "Telegram", mattermost: "Mattermost" }[platform]} app
        </h3>
        <p className="text-xs text-muted-foreground">Follow these steps before entering your credentials.</p>
      </div>
      {(platform === "slack" || platform === "teams") && <WebhookUrlCallout platform={platform} />}
      <div className="space-y-1">
        {instructions.map((instruction, i) => (
          <div key={i} className="flex gap-3 items-start px-3 py-2.5 rounded-lg hover:bg-muted/40 transition-colors">
            <span className="flex items-center justify-center w-5 h-5 rounded-full bg-primary/10 text-primary text-[11px] font-bold shrink-0 mt-0.5">
              {i + 1}
            </span>
            <div className="text-sm text-foreground/80 leading-relaxed">
              <span>
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
              {instruction.details && instruction.details.length > 0 && (
                <ul className="mt-1.5 space-y-1 text-xs text-foreground/75">
                  {instruction.details.map((detail) => (
                    <li key={detail} className="font-mono">
                      • {detail}
                    </li>
                  ))}
                </ul>
              )}
            </div>
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
          placeholder={`e.g. ${{ slack: "Engineering Workspace", discord: "Community Server", teams: "Corp Tenant", telegram: "Alerts Bot", mattermost: "Team Chat" }[platform]}`}
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
  fields: CredentialField[];
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  // Auto-fill defaults the first time the user reaches this step so a
  // typo-prone enum like Teams `app_type` lands on the recommended value
  // instead of an empty string.
  //
  // `fields` is `CREDENTIAL_FIELDS[platform]` — a module-level constant —
  // so its reference never changes across renders and this effect fires
  // exactly once per mount. `values` and `onChange` are intentionally
  // omitted from the deps: re-running on every keystroke would clobber
  // the user's typing, and the one-shot fill is all we want.
  useEffect(() => {
    for (const field of fields) {
      if (field.default != null && (values[field.key] ?? "") === "") {
        onChange(field.key, field.default);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fields]);

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">Enter your credentials</h3>
        <p className="text-xs text-muted-foreground">These are stored securely and never shared.</p>
      </div>
      {fields.map((field) => (
        <div key={field.key}>
          <label className="block text-xs font-medium text-foreground mb-1.5">{field.label}</label>
          {field.enum ? (
            <select
              value={values[field.key] ?? field.default ?? ""}
              onChange={(e) => onChange(field.key, e.target.value)}
              className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/20 font-mono"
            >
              {field.enum.map((opt) => (
                <option key={opt} value={opt}>{opt}</option>
              ))}
            </select>
          ) : (
            <input
              type={field.type ?? "text"}
              value={values[field.key] ?? ""}
              onChange={(e) => onChange(field.key, e.target.value)}
              placeholder={field.placeholder}
              className="w-full h-9 px-3 rounded-lg border border-border bg-background text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20 font-mono"
              autoComplete="off"
              spellCheck={false}
            />
          )}
          {(() => {
            const trimmed = (values[field.key] ?? "").trim();
            const err = trimmed && field.validate ? field.validate(trimmed) : null;
            if (err) {
              return <p className="text-[11px] text-rose-500 mt-1 leading-snug">{err}</p>;
            }
            if (field.hint) {
              return <p className="text-[11px] text-muted-foreground/85 mt-1 leading-snug">{field.hint}</p>;
            }
            return null;
          })()}
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
        <ChannelSelector channels={channels} selected={selected} onChange={onChange} platform={platform} />
      )}
    </div>
  );
}

function StepWebhookMode({ platform }: { platform: Platform }) {
  if (platform === "teams") return <TeamsWebhookMode />;
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">Webhook-driven ingestion</h3>
        <p className="text-xs text-muted-foreground">
          Telegram bots receive messages via webhook and have no channel listing API. Channels appear
          automatically once the bot receives its first message from a chat it&apos;s been added to.
        </p>
      </div>
      <div className="flex items-start gap-2 rounded-lg bg-primary/5 border border-primary/20 px-3 py-2.5">
        <Zap className="w-4 h-4 text-primary shrink-0 mt-0.5" />
        <p className="text-xs text-muted-foreground">
          Make sure the bot is added to your group and, for privacy-enabled bots, granted admin permission so it can read messages.
        </p>
      </div>
    </div>
  );
}

/** Teams' post-validation step. Endpoint + Graph consent are covered in
 *  the Setup step list (they happen in Azure before credentials), so this
 *  panel is just the one remaining action that has to happen in TEAMS
 *  itself — uploading the app package so the bot appears in channels. */
function TeamsWebhookMode() {
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">One step left in Teams</h3>
        <p className="text-xs text-muted-foreground">
          Credentials validated. To make the bot appear in channels, a Teams admin uploads the app package:
        </p>
      </div>

      <div className="rounded-lg border border-border px-3 py-2.5">
        <p className="text-[11px] text-muted-foreground leading-snug">
          Teams Admin Center → Manage apps → Upload custom app:
        </p>
        <code className="block mt-1 text-[11px] bg-muted px-2 py-1 rounded font-mono break-all">
          bot/teams-app/beever-atlas-teams.zip
        </code>
        <p className="text-[11px] text-muted-foreground/85 mt-1.5 leading-snug">
          Once uploaded, add the app to the team(s) and channels you want Beever Atlas to read.
        </p>
      </div>

      <div className="flex items-start gap-2 rounded-lg bg-primary/5 border border-primary/20 px-3 py-2.5">
        <Zap className="w-4 h-4 text-primary shrink-0 mt-0.5" />
        <p className="text-xs text-muted-foreground">
          Channels appear automatically — no @mention required.
        </p>
      </div>
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
