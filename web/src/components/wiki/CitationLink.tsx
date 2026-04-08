import type { WikiCitation } from "@/lib/types";

interface CitationLinkProps {
  index: number;
  citation?: WikiCitation;
}

export function CitationLink({ index, citation }: CitationLinkProps) {
  const handleClick = () => {
    // Scroll to the citation in the Sources panel at the bottom
    const el = document.getElementById(`citation-${index}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      // Brief highlight
      el.classList.add("ring-2", "ring-primary", "ring-offset-2", "ring-offset-background");
      setTimeout(() => el.classList.remove("ring-2", "ring-primary", "ring-offset-2", "ring-offset-background"), 2000);
      return;
    }
    window.dispatchEvent(new CustomEvent("wiki:citation-jump", { detail: { index } }));
  };

  return (
    <span className="group/cite relative inline-block align-super">
      <button
        onClick={handleClick}
        className="inline-flex items-center justify-center min-w-[1.1rem] h-[1.1rem] rounded-full bg-primary/10 hover:bg-primary/20 text-primary text-[10px] font-semibold leading-none cursor-pointer transition-colors px-1"
      >
        {index}
      </button>
      {citation && (
        <span className="pointer-events-none invisible group-hover/cite:visible absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-72 rounded-lg bg-popover border border-border px-3 py-2.5 text-xs text-popover-foreground shadow-xl z-50">
          <div className="flex items-center gap-2 mb-1.5">
            {citation.author && (
              <span className="inline-flex items-center rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                @{citation.author}
              </span>
            )}
            {citation.timestamp && (
              <span className="text-muted-foreground text-[10px]">{citation.timestamp}</span>
            )}
            {citation.media_type && (
              <span className="text-[10px]">
                {citation.media_type === "pdf" ? "📄" : citation.media_type === "image" ? "🖼️" : "📎"}
              </span>
            )}
          </div>
          <p className="text-popover-foreground/90 leading-relaxed line-clamp-3">
            {citation.text_excerpt}
          </p>
          {citation.permalink && citation.permalink.startsWith("http") && (
            <a
              href={citation.permalink}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1.5 text-[10px] text-primary/70 hover:text-primary inline-block"
              onClick={(e) => e.stopPropagation()}
            >
              View original ↗
            </a>
          )}
        </span>
      )}
    </span>
  );
}
