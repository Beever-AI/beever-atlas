import { useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  FileText,
  Loader2,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useFileImport } from "@/hooks/useFileImport";
import type {
  ImportColumnMapping,
  ImportCommitResponse,
  ImportPreviewResponse,
} from "@/lib/types";

interface FileImportWizardProps {
  onClose: () => void;
  onComplete: (result: ImportCommitResponse) => void;
}

type Step = "upload" | "mapping" | "confirm";

const MAPPING_FIELDS: {
  key: keyof ImportColumnMapping;
  label: string;
  required?: boolean;
  help?: string;
}[] = [
  { key: "content", label: "Message content", required: true },
  { key: "author", label: "Author ID" },
  { key: "author_name", label: "Author display name" },
  { key: "timestamp", label: "Timestamp" },
  { key: "timestamp_time", label: "Timestamp — time (optional split column)" },
  { key: "message_id", label: "Message ID" },
  { key: "thread_id", label: "Thread / reply-to ID" },
  { key: "attachments", label: "Attachments" },
  { key: "reactions", label: "Reactions" },
];

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function FileImportWizard({ onClose, onComplete }: FileImportWizardProps) {
  const [step, setStep] = useState<Step>("upload");
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [useLlm, setUseLlm] = useState(true);
  const [previewResp, setPreviewResp] = useState<ImportPreviewResponse | null>(null);
  const [mapping, setMapping] = useState<ImportColumnMapping | null>(null);
  const [channelName, setChannelName] = useState("");
  const [skipEmpty, setSkipEmpty] = useState(true);
  const [skipSystem, setSkipSystem] = useState(true);
  const [skipDeleted, setSkipDeleted] = useState(true);
  const [dayfirst, setDayfirst] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const { preview, commit, previewing, committing } = useFileImport();

  const headers = previewResp?.headers ?? [];

  async function runPreview(selectedFile: File) {
    setErrorMsg(null);
    setFile(selectedFile);
    try {
      const resp = await preview(selectedFile, useLlm);
      setPreviewResp(resp);
      setMapping(resp.mapping);
      setChannelName(
        selectedFile.name.replace(/\.[^.]+$/, "") || "File Import",
      );
      // Auto-skip mapping step when a preset matched cleanly.
      if (resp.preset && !resp.needs_review) {
        setStep("confirm");
      } else {
        setStep("mapping");
      }
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Preview failed");
    }
  }

  function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const f = files[0];
    const okExt = /\.(csv|tsv|jsonl|ndjson)$/i.test(f.name);
    if (!okExt) {
      setErrorMsg("Unsupported file type. Use .csv, .tsv, or .jsonl.");
      return;
    }
    void runPreview(f);
  }

  async function handleCommit() {
    if (!previewResp || !mapping) return;
    setErrorMsg(null);
    try {
      const resp = await commit({
        file_id: previewResp.file_id,
        channel_name: channelName.trim() || "File Import",
        mapping,
        skip_empty: skipEmpty,
        skip_system: skipSystem,
        skip_deleted: skipDeleted,
        dayfirst,
      });
      onComplete(resp);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Commit failed");
    }
  }

  const sourceBadge = useMemo(() => {
    if (!previewResp) return null;
    const label =
      previewResp.preset
        ? `Preset: ${previewResp.preset}`
        : previewResp.mapping_source === "llm"
          ? "AI-inferred"
          : previewResp.mapping_source === "fuzzy"
            ? "Auto-detected"
            : "Needs review";
    const tone = previewResp.needs_review
      ? "bg-amber-500/10 text-amber-600 border-amber-500/20"
      : "bg-emerald-500/10 text-emerald-600 border-emerald-500/20";
    return (
      <span className={cn("inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[11px]", tone)}>
        <Sparkles className="w-3 h-3" />
        {label}
        <span className="opacity-60">· {Math.round(previewResp.overall_confidence * 100)}% confidence</span>
      </span>
    );
  }, [previewResp]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={onClose} />

      <div className="relative z-10 w-full max-w-3xl max-h-[90vh] bg-card border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col">
        <div className="px-6 py-4 border-b border-border flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
              <FileText className="w-4 h-4 text-primary" />
            </div>
            <div>
              <h2 className="text-base font-semibold text-foreground">Import from file</h2>
              <p className="text-xs text-muted-foreground">
                Upload a CSV / TSV / JSONL export and ingest it as a channel.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted transition-colors"
          >
            <X className="w-4 h-4 text-muted-foreground" />
          </button>
        </div>

        <div className="px-6 py-5 min-h-[320px] overflow-y-auto flex-1">
          {step === "upload" && (
            <div className="space-y-5">
              <div
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragOver(true);
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOver(false);
                  handleFiles(e.dataTransfer.files);
                }}
                onClick={() => inputRef.current?.click()}
                className={cn(
                  "flex flex-col items-center justify-center rounded-xl border-2 border-dashed py-14 px-6 cursor-pointer transition-colors",
                  dragOver
                    ? "border-primary bg-primary/5"
                    : "border-border hover:border-primary/50 hover:bg-muted/30",
                )}
              >
                <div className="w-12 h-12 rounded-2xl bg-muted flex items-center justify-center mb-4">
                  <Upload className="w-6 h-6 text-muted-foreground" />
                </div>
                <p className="text-sm font-medium text-foreground">
                  Drag a file here, or click to browse
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  .csv, .tsv, .jsonl — up to ~100k rows
                </p>
                <input
                  ref={inputRef}
                  type="file"
                  accept=".csv,.tsv,.jsonl,.ndjson,text/csv,text/tab-separated-values"
                  className="hidden"
                  onChange={(e) => handleFiles(e.target.files)}
                />
              </div>
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={useLlm}
                  onChange={(e) => setUseLlm(e.target.checked)}
                  className="rounded"
                />
                Use AI to infer the column mapping when headers are unfamiliar
                <span className="text-[10px] opacity-60">(1 LLM call per file; skipped when a preset matches)</span>
              </label>
              {previewing && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  Analyzing file…
                </div>
              )}
              {errorMsg && (
                <div className="flex items-center gap-2 text-xs text-rose-600">
                  <AlertCircle className="w-3.5 h-3.5" />
                  {errorMsg}
                </div>
              )}
            </div>
          )}

          {step === "mapping" && previewResp && mapping && (
            <div className="space-y-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-foreground">
                    Review column mapping
                  </h3>
                  <p className="text-xs text-muted-foreground">
                    We detected these columns — confirm or adjust the mapping.
                  </p>
                </div>
                {sourceBadge}
              </div>

              <div className="rounded-xl border border-border overflow-hidden">
                <div className="grid grid-cols-[1fr_1fr] gap-0 text-xs bg-muted/30 px-3 py-2 font-medium text-muted-foreground">
                  <span>Canonical field</span>
                  <span>Source column</span>
                </div>
                <div className="divide-y divide-border">
                  {MAPPING_FIELDS.map((f) => {
                    const currentValue = (mapping[f.key] as string | null | undefined) ?? "";
                    return (
                      <div key={f.key} className="grid grid-cols-[1fr_1fr] gap-3 items-center px-3 py-2">
                        <div>
                          <div className="text-sm text-foreground">
                            {f.label}
                            {f.required && <span className="text-rose-500 ml-1">*</span>}
                          </div>
                        </div>
                        <select
                          value={currentValue}
                          onChange={(e) =>
                            setMapping({ ...mapping, [f.key]: e.target.value || null })
                          }
                          className="w-full h-8 px-2 rounded-md border border-border bg-background text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
                        >
                          <option value="">— none —</option>
                          {headers.map((h) => (
                            <option key={h} value={h}>
                              {h}
                            </option>
                          ))}
                        </select>
                      </div>
                    );
                  })}
                </div>
              </div>

              <div>
                <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">
                  Sample (first messages)
                </h4>
                {previewResp.sample_messages.length === 0 ? (
                  <p className="text-xs text-muted-foreground italic">
                    No preview — adjust the mapping and continue.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {previewResp.sample_messages.slice(0, 3).map((m, i) => (
                      <div key={i} className="rounded-lg border border-border bg-muted/20 px-3 py-2">
                        <div className="text-[11px] text-muted-foreground mb-0.5">
                          {m.author_name || m.author || "—"} · {m.timestamp}
                        </div>
                        <div className="text-sm text-foreground truncate">{m.content}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {step === "confirm" && previewResp && (
            <div className="space-y-5">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-foreground">Ready to import</h3>
                  <p className="text-xs text-muted-foreground">
                    Review the summary and start ingestion.
                  </p>
                </div>
                {sourceBadge}
              </div>

              <div className="rounded-xl border border-border divide-y divide-border overflow-hidden">
                <div className="px-4 py-3 flex justify-between text-sm">
                  <span className="text-muted-foreground">File</span>
                  <span className="font-medium text-foreground truncate max-w-[60%]">
                    {previewResp.filename}
                    {file && (
                      <span className="ml-2 text-[11px] text-muted-foreground">
                        ({humanSize(file.size)})
                      </span>
                    )}
                  </span>
                </div>
                <div className="px-4 py-3 flex justify-between text-sm">
                  <span className="text-muted-foreground">Format / encoding</span>
                  <span className="font-mono text-xs text-foreground">
                    {previewResp.format.toUpperCase()} · {previewResp.encoding}
                  </span>
                </div>
                <div className="px-4 py-3 flex justify-between text-sm">
                  <span className="text-muted-foreground">Estimated rows</span>
                  <span className="font-medium text-foreground">{previewResp.row_count_estimate}</span>
                </div>
                <div className="px-4 py-3 flex justify-between items-center text-sm gap-3">
                  <span className="text-muted-foreground">Channel name</span>
                  <input
                    type="text"
                    value={channelName}
                    onChange={(e) => setChannelName(e.target.value)}
                    className="flex-1 max-w-xs h-8 px-2 rounded-md border border-border bg-background text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
                  />
                </div>
              </div>

              <details
                open={showAdvanced}
                onToggle={(e) => setShowAdvanced((e.target as HTMLDetailsElement).open)}
                className="rounded-xl border border-border overflow-hidden"
              >
                <summary className="px-4 py-3 cursor-pointer text-xs font-medium text-muted-foreground hover:bg-muted/30">
                  Advanced options
                </summary>
                <div className="px-4 py-3 space-y-2 text-sm border-t border-border">
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={skipEmpty}
                      onChange={(e) => setSkipEmpty(e.target.checked)}
                    />
                    Skip empty messages
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={skipSystem}
                      onChange={(e) => setSkipSystem(e.target.checked)}
                    />
                    Skip system messages (“Pinned a message.”, etc.)
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={skipDeleted}
                      onChange={(e) => setSkipDeleted(e.target.checked)}
                    />
                    Skip deleted messages
                  </label>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={dayfirst}
                      onChange={(e) => setDayfirst(e.target.checked)}
                    />
                    Parse ambiguous dates as DD/MM (European format)
                  </label>
                </div>
              </details>

              {previewResp.needs_review && (
                <div className="rounded-lg bg-amber-500/10 border border-amber-500/20 px-3 py-2 text-xs text-amber-700 dark:text-amber-400 flex gap-2">
                  <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                  Mapping confidence is low. Double-check before importing —
                  <button
                    type="button"
                    onClick={() => setStep("mapping")}
                    className="underline hover:text-amber-800"
                  >
                    reopen mapping
                  </button>.
                </div>
              )}

              {errorMsg && (
                <div className="flex items-center gap-2 text-xs text-rose-600">
                  <AlertCircle className="w-3.5 h-3.5" />
                  {errorMsg}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="flex items-center justify-between px-6 py-4 border-t border-border bg-muted/30 shrink-0">
          <div>
            {step === "mapping" && (
              <button
                type="button"
                onClick={() => setStep("upload")}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <ArrowLeft className="w-4 h-4" />
                Back
              </button>
            )}
            {step === "confirm" && (
              <button
                type="button"
                onClick={() =>
                  setStep(previewResp?.preset && !previewResp.needs_review ? "upload" : "mapping")
                }
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-border text-sm font-medium text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                <ArrowLeft className="w-4 h-4" />
                Back
              </button>
            )}
          </div>
          <div className="flex gap-2">
            {step === "mapping" && (
              <button
                type="button"
                onClick={() => setStep("confirm")}
                disabled={!mapping?.content}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:pointer-events-none"
              >
                Continue
                <ArrowRight className="w-4 h-4" />
              </button>
            )}
            {step === "confirm" && (
              <button
                type="button"
                onClick={handleCommit}
                disabled={committing}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                {committing ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <CheckCircle2 className="w-4 h-4" />
                )}
                Start import
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
