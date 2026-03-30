import { useState, useRef, useEffect } from "react";
import { useParams } from "react-router-dom";
import { useAsk } from "@/hooks/useAsk";
import { Badge } from "@/components/ui/badge";
import {
  ArrowUp,
  Bot,
  Sparkles,
  AlertCircle,
  ChevronDown,
  ChevronRight,
  MessageCircle,
  Link2,
  Loader2,
} from "lucide-react";

const EXAMPLE_QUESTIONS = [
  "What decisions were made about the API design?",
  "Who is the primary contact for infrastructure?",
  "What was discussed about the Q4 roadmap?",
  "Are there any unresolved issues from last sprint?",
];

export function AskTab() {
  const { id: channelId } = useParams<{ id: string }>();
  const [input, setInput] = useState("");
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const [lastQuestion, setLastQuestion] = useState("");
  const responseRef = useRef<HTMLDivElement>(null);
  const { ask, response, thinking, citations, metadata, isStreaming, error } =
    useAsk(channelId || "");

  useEffect(() => {
    if (response && responseRef.current) {
      responseRef.current.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }, [response]);

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;
    const question = input.trim();
    setLastQuestion(question);
    ask(question);
    setInput("");
    setThinkingOpen(false);
  };

  const hasContent = response || isStreaming || error;

  return (
    <div className="grid grid-rows-[1fr_auto] h-full bg-background">
      <div className="overflow-auto px-4 sm:px-6 py-6">
        <div className="max-w-3xl mx-auto">
          {!hasContent && (
            <div className="min-h-[58vh] flex flex-col items-center justify-center text-center animate-fade-in">
              <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center mb-4">
                <Sparkles size={18} className="text-primary" />
              </div>
              <h3 className="text-xl font-semibold text-foreground">Ask this channel</h3>
              <p className="text-sm text-muted-foreground mt-2 mb-6 max-w-lg">
                Ask questions about decisions, owners, and context from this channel history.
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full">
                {EXAMPLE_QUESTIONS.map((q) => (
                  <button
                    key={q}
                    onClick={() => setInput(q)}
                    className="text-left px-3.5 py-2.5 rounded-xl border border-border bg-card hover:bg-muted transition-colors text-sm text-foreground/90"
                  >
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {hasContent && (
            <div className="space-y-5 animate-fade-in pb-8">
              {lastQuestion && (
                <div className="flex justify-end">
                  <div className="max-w-[85%] rounded-2xl bg-muted px-4 py-3 text-sm text-foreground leading-relaxed">
                    {lastQuestion}
                  </div>
                </div>
              )}

              {isStreaming && !response && (
                <div className="rounded-2xl border border-border bg-card px-4 py-3">
                  <div className="flex items-center gap-2 text-sm text-foreground">
                    <Loader2 size={14} className="animate-spin text-primary" />
                    Thinking…
                  </div>
                </div>
              )}

              <div className="flex items-start gap-3" ref={responseRef}>
                <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center shrink-0">
                  <Bot className="w-4 h-4 text-primary" />
                </div>
                <div className="flex-1 min-w-0 space-y-3">
                  {response && (
                    <div className="rounded-2xl border border-border bg-card px-4 py-3 text-[15px] text-foreground whitespace-pre-wrap leading-relaxed">
                      {response}
                    </div>
                  )}

                  {thinking.length > 0 && (
                    <div className="rounded-xl border border-border bg-muted/40 px-3 py-2.5">
                      <button
                        onClick={() => setThinkingOpen(!thinkingOpen)}
                        className="flex items-center gap-2 w-full text-left"
                      >
                        {thinkingOpen ? (
                          <ChevronDown size={12} className="text-muted-foreground" />
                        ) : (
                          <ChevronRight size={12} className="text-muted-foreground" />
                        )}
                        <span className="text-xs font-medium text-muted-foreground">Reasoning</span>
                        <Badge variant="outline" className="ml-auto text-xs h-4">
                          {thinking.length}
                        </Badge>
                      </button>
                      {thinkingOpen && (
                        <div className="mt-2 space-y-1.5">
                          {thinking.map((step, i) => (
                            <p
                              key={i}
                              className="text-sm text-muted-foreground pl-3 border-l-2 border-border leading-relaxed"
                            >
                              {step}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {citations.length > 0 && (
                    <div className="space-y-1.5">
                      <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                        Sources
                      </p>
                      <div className="space-y-1.5">
                        {citations.map((c, i) => (
                          <div
                            key={i}
                            className="flex items-start gap-2 rounded-lg border border-border bg-background px-3 py-2 text-sm text-muted-foreground"
                          >
                            <span className="mt-0.5 text-primary shrink-0">
                              <Link2 size={12} />
                            </span>
                            <span>
                              [{i + 1}] {c.text}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {metadata && (
                    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                      <span className="rounded-md bg-primary/10 text-primary px-2 py-0.5 font-medium">
                        {metadata.route}
                      </span>
                      <span>{(metadata.confidence * 100).toFixed(0)}% confidence</span>
                      <span className="font-mono">${metadata.cost_usd.toFixed(4)}</span>
                    </div>
                  )}

                  {error && (
                    <div className="flex items-start gap-2 bg-rose-50 dark:bg-rose-950/30 border border-rose-200 dark:border-rose-900 rounded-lg p-3">
                      <AlertCircle
                        size={14}
                        className="text-rose-600 dark:text-rose-400 shrink-0 mt-0.5"
                      />
                      <p className="text-sm text-rose-700 dark:text-rose-400">{error}</p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="border-t border-border bg-background/95 backdrop-blur-sm px-4 sm:px-6 py-4">
        <div className="max-w-3xl mx-auto">
          <form onSubmit={handleSubmit} className="flex items-center gap-2">
            <div className="flex-1 flex items-center gap-2 bg-card rounded-2xl border border-border px-4 py-3 shadow-sm">
              <MessageCircle size={16} className="text-muted-foreground shrink-0" />
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder="Message this channel knowledge base..."
                disabled={isStreaming}
                className="flex-1 text-base bg-transparent outline-none text-foreground placeholder:text-muted-foreground"
                autoComplete="off"
              />
            </div>
            <button
              type="submit"
              disabled={isStreaming || !input.trim()}
              className="w-11 h-11 rounded-xl bg-primary hover:bg-primary/90 flex items-center justify-center shrink-0 disabled:opacity-40 transition-colors"
              aria-label="Send"
            >
              <ArrowUp size={16} className="text-primary-foreground" />
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
