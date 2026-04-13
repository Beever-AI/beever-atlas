import { useState, useEffect, useRef, memo, useMemo } from "react";
import { ChevronDown, Sparkles } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

interface ReasoningProps {
  thinking: string[];
  isStreaming: boolean;
  durationMs: number | null;
}

function ReasoningInner({ thinking, isStreaming, durationMs }: ReasoningProps) {
  const [expanded, setExpanded] = useState(true);
  const userToggledRef = useRef(false);

  useEffect(() => {
    if (!isStreaming && thinking.length > 0 && !userToggledRef.current) {
      const t = setTimeout(() => setExpanded(false), 300);
      return () => clearTimeout(t);
    }
  }, [isStreaming, thinking.length]);

  const text = thinking.join("");
  const renderedMarkdown = useMemo(
    () => (
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => (
            <p className="mb-2 leading-relaxed text-sm text-muted-foreground/80">{children}</p>
          ),
          strong: ({ children }) => (
            <strong className="font-semibold text-muted-foreground/90">{children}</strong>
          ),
          ul: ({ children }) => (
            <ul className="mb-2 space-y-0.5 list-disc list-inside text-sm text-muted-foreground/80">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-2 space-y-0.5 list-decimal list-inside text-sm text-muted-foreground/80">{children}</ol>
          ),
          li: ({ children }) => <li className="text-muted-foreground/80">{children}</li>,
          code: ({ children }) => (
            <code className="px-1 py-0.5 bg-muted/50 rounded text-xs text-muted-foreground/90">{children}</code>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    ),
    [text],
  );

  if (!text) return null;

  const label = isStreaming
    ? "Thinking…"
    : durationMs != null
      ? `Thought for ${(durationMs / 1000).toFixed(1)}s`
      : "Thought process";

  const toggle = () => {
    userToggledRef.current = true;
    setExpanded((v) => !v);
  };

  return (
    <div className="mb-3">
      <button
        onClick={toggle}
        className="inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        <Sparkles className="size-3.5" strokeWidth={2} />
        <span>{label}</span>
        <ChevronDown
          className={cn(
            "size-3 transition-transform",
            expanded ? "rotate-0" : "-rotate-90",
          )}
        />
      </button>

      <div
        className={cn(
          "overflow-hidden transition-all duration-300 ease-in-out",
          expanded ? "max-h-[500px] opacity-100 mt-2" : "max-h-0 opacity-0",
        )}
      >
        <div className="border-l border-border pl-4 ml-1.5 max-h-[480px] overflow-y-auto">
          {renderedMarkdown}
        </div>
      </div>
    </div>
  );
}

export const Reasoning = memo(ReasoningInner);
