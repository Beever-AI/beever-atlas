import React, { useRef, useState, useEffect, useCallback } from "react";
import { Send, Square, Paperclip, X, Sparkles, ChevronDown } from "lucide-react";
import type { AnswerMode, AttachmentFile } from "../../types/askTypes";

interface ChatInputBarProps {
  onSubmit: (question: string, options?: { mode?: AnswerMode; attachments?: AttachmentFile[] }) => void;
  onAbort?: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  mode: AnswerMode;
  onModeChange: (mode: AnswerMode) => void;
  attachments?: AttachmentFile[];
  onFileUpload?: (file: File) => void;
  onRemoveAttachment?: (fileId: string) => void;
  uploading?: boolean;
  /** Optional inline channel picker rendered inside the input bar (v2 flow). */
  channelPicker?: React.ReactNode;
  /** Placeholder override for the textarea. */
  placeholder?: string;
  /**
   * Optional seed text. When it changes to a non-empty value the textarea is
   * prefilled and focused, letting callers hand off a draft (e.g. Dashboard
   * suggestion chips → /ask?q=…) without auto-sending.
   */
  initialValue?: string;
}

const MODE_OPTIONS: { value: AnswerMode; label: string; description: string }[] = [
  { value: "quick", label: "Quick", description: "Fast 1–3 sentence answer from cached wiki" },
  { value: "deep", label: "Deep Research", description: "Thorough answer using all knowledge sources" },
  { value: "summarize", label: "Summarize", description: "Structured bullet-point summary" },
];

export function ChatInputBar({
  onSubmit,
  onAbort,
  isStreaming,
  disabled,
  mode,
  onModeChange,
  attachments = [],
  onFileUpload,
  onRemoveAttachment,
  uploading,
  channelPicker,
  placeholder,
  initialValue,
}: ChatInputBarProps) {
  const [text, setText] = useState(initialValue ?? "");

  // Seed from initialValue when it arrives / changes. Intentionally only
  // reacts to the seed itself — we must not echo `text` back here or we'd
  // clobber the user's edits.
  useEffect(() => {
    if (initialValue) {
      setText(initialValue);
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(el.value.length, el.value.length);
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialValue]);
  const [focused, setFocused] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [showModeMenu, setShowModeMenu] = useState(false);

  const adjustHeight = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 180) + "px";
  }, []);

  useEffect(() => adjustHeight(), [text, adjustHeight]);

  const handleSubmit = () => {
    const trimmed = text.trim();
    if (!trimmed || isStreaming || disabled) return;
    onSubmit(trimmed, { mode, attachments });
    setText("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const files = Array.from(e.dataTransfer.files);
    files.forEach((f) => onFileUpload?.(f));
  };

  const handleDragOver = (e: React.DragEvent) => e.preventDefault();

  const currentMode = MODE_OPTIONS.find((m) => m.value === mode) ?? MODE_OPTIONS[1];
  const canSubmit = !!text.trim() && !isStreaming && !disabled;

  return (
    <div className="px-4 sm:px-6 pb-5 pt-2 bg-background">
      <div className="max-w-3xl mx-auto w-full">
        {/* Attachments row */}
        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2">
            {attachments.map((att) => (
              <span
                key={att.file_id}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-card rounded-lg text-xs text-foreground/90 border border-border"
              >
                <Paperclip className="w-3 h-3 text-muted-foreground/70" />
                {att.filename}
                <button
                  onClick={() => onRemoveAttachment?.(att.file_id)}
                  className="text-muted-foreground/60 hover:text-foreground transition-colors"
                >
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* Composer */}
        <div
          className={`flex flex-col bg-card border rounded-2xl transition-all duration-200 ${
            focused
              ? "border-primary/40 shadow-[0_0_0_4px_oklch(from_var(--primary)_l_c_h_/_0.08)]"
              : "border-border shadow-sm hover:border-border/80 hover:shadow-md"
          }`}
          onDrop={handleDrop}
          onDragOver={handleDragOver}
        >
          {/* Textarea row */}
          <div className="flex items-start gap-2 px-4 pt-3">
            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={handleKeyDown}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              placeholder={
                isStreaming
                  ? "Waiting for response…"
                  : placeholder ?? "Ask a question…"
              }
              disabled={isStreaming || disabled}
              rows={1}
              className="flex-1 bg-transparent text-foreground text-[15px] resize-none outline-none placeholder:text-muted-foreground/50 disabled:opacity-50 py-1 leading-relaxed min-h-[28px]"
            />
          </div>

          {/* Controls row */}
          <div className="flex items-center gap-1.5 px-3 pb-2.5 pt-1">
            {/* Channel picker (v2 flow) */}
            {channelPicker}

            {/* Attach */}
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={isStreaming || uploading}
              className="inline-flex items-center justify-center w-8 h-8 rounded-lg text-muted-foreground/70 hover:text-foreground hover:bg-muted transition-colors disabled:opacity-40"
              title="Attach file"
            >
              <Paperclip className="w-4 h-4" strokeWidth={2} />
            </button>
            <input
              ref={fileInputRef}
              type="file"
              className="hidden"
              accept=".pdf,.png,.jpg,.jpeg,.docx,.txt,.csv"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) onFileUpload?.(file);
                e.target.value = "";
              }}
            />

            {/* Mode selector — subdued, text-only */}
            <div className="relative">
              <button
                onClick={() => setShowModeMenu(!showModeMenu)}
                className="inline-flex items-center gap-1 h-8 px-2 rounded-lg text-[13px] font-medium text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
              >
                <Sparkles className="w-3.5 h-3.5 opacity-70" strokeWidth={2} />
                {currentMode.label}
                <ChevronDown
                  className={`w-3 h-3 opacity-60 transition-transform ${
                    showModeMenu ? "rotate-180" : ""
                  }`}
                />
              </button>
              {showModeMenu && (
                <>
                  <div
                    className="fixed inset-0 z-40"
                    onClick={() => setShowModeMenu(false)}
                  />
                  <div className="absolute bottom-full left-0 mb-2 bg-popover border border-border rounded-xl shadow-xl py-1 w-64 z-50 motion-safe:animate-scale-in origin-bottom-left">
                    {MODE_OPTIONS.map((opt) => (
                      <button
                        key={opt.value}
                        onClick={() => {
                          onModeChange(opt.value);
                          setShowModeMenu(false);
                        }}
                        className={`w-full text-left px-3 py-2 text-sm hover:bg-muted/60 transition-colors ${
                          mode === opt.value ? "text-primary" : "text-foreground/90"
                        }`}
                      >
                        <div className="font-medium">{opt.label}</div>
                        <div className="text-[11px] text-muted-foreground/70 mt-0.5">
                          {opt.description}
                        </div>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>

            {/* Spacer */}
            <div className="flex-1" />

            {/* Send / Stop */}
            {isStreaming ? (
              <button
                onClick={onAbort}
                className="inline-flex items-center justify-center w-9 h-9 rounded-xl bg-destructive/15 text-destructive hover:bg-destructive/25 transition-colors"
                title="Stop generating"
              >
                <Square className="w-3.5 h-3.5" fill="currentColor" />
              </button>
            ) : (
              <button
                onClick={handleSubmit}
                disabled={!canSubmit}
                className={`inline-flex items-center justify-center w-9 h-9 rounded-xl transition-all duration-150 ${
                  canSubmit
                    ? "bg-primary text-primary-foreground hover:bg-primary/90 shadow-sm"
                    : "bg-muted text-muted-foreground/50 cursor-not-allowed"
                }`}
                title="Send (⏎)"
              >
                <Send className="w-4 h-4" strokeWidth={2.5} />
              </button>
            )}
          </div>
        </div>

      </div>
    </div>
  );
}
