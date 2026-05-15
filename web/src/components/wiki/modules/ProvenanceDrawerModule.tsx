/**
 * ProvenanceDrawerModule — exposes the source messages each fact on
 * the page came from, with platform deep-links.
 *
 * Default-collapsed accordion. When expanded, shows messages
 * chronologically as cards: timestamp, author chip, platform pill,
 * channel pill (if present), snippet, contributed-to fact-id chips,
 * "Open ↗" external link.
 *
 * The first 10 messages render eagerly; the remainder live behind a
 * "Show N more ▾" expander so very busy pages don't dump a wall of
 * cards at once.
 *
 * Shape (set by `wiki/modules/provenance_drawer.py::build_provenance_drawer_data`):
 *   - `messages`: array of source-message rows
 *   - `total_count`: full size (used for "+N more" header)
 */

import { useState } from "react";
import type { ModuleProps } from "./ModuleRenderer";

interface ProvenanceMessage {
  ts?: string;
  author?: string;
  platform?: string;
  channel?: string;
  url?: string;
  snippet?: string;
  contributed_to_facts?: string[];
}

interface ProvenanceData {
  label?: string;
  messages?: ProvenanceMessage[];
  total_count?: number;
}

const INITIAL_VISIBLE = 10;

/** Format an ISO timestamp as a compact "Apr 22 · 10:32" header.
 *  Returns the raw string when unparseable so we never render "NaN". */
function formatTs(ts: string): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const month = d.toLocaleString("en-US", { month: "short" });
  const day = d.getDate();
  const hours = d.getHours().toString().padStart(2, "0");
  const mins = d.getMinutes().toString().padStart(2, "0");
  return `${month} ${day} · ${hours}:${mins}`;
}

export function ProvenanceDrawerModule({ module }: ModuleProps) {
  const data = (module.data ?? {}) as ProvenanceData;
  const messages = data.messages ?? [];
  const totalCount = typeof data.total_count === "number"
    ? data.total_count
    : messages.length;

  const [expanded, setExpanded] = useState(false);
  const [showAll, setShowAll] = useState(false);

  if (messages.length === 0) return null;

  const visible = expanded
    ? showAll
      ? messages
      : messages.slice(0, INITIAL_VISIBLE)
    : [];
  const hiddenCount = expanded ? Math.max(0, messages.length - INITIAL_VISIBLE) : 0;

  return (
    <section
      className="mt-8 border border-border/60 rounded-lg bg-muted/10"
      id={`module-${module.anchor}`}
      data-testid="module-provenance_drawer"
      data-toc-skip
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left text-sm font-medium hover:bg-muted/20 transition-colors rounded-lg"
        data-testid="provenance-drawer-toggle"
        aria-expanded={expanded}
      >
        <span className="inline-flex items-center gap-2">
          <span aria-hidden="true">📜</span>
          <span>Source messages ({totalCount})</span>
        </span>
        <span className="text-muted-foreground text-xs">
          {expanded ? "Hide ▴" : "Show ▾"}
        </span>
      </button>

      {expanded && (
        <ul
          className="px-4 pb-4 space-y-3 list-none"
          data-testid="provenance-drawer-list"
        >
          {visible.map((msg, idx) => {
            const tsLabel = formatTs(msg.ts || "");
            const facts = msg.contributed_to_facts ?? [];
            return (
              <li
                key={`${msg.ts || ""}-${idx}`}
                className="border-l-2 border-border/60 pl-3 py-2"
                data-testid="provenance-message"
              >
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  {tsLabel && (
                    <span className="font-medium text-foreground">{tsLabel}</span>
                  )}
                  {msg.author && (
                    <>
                      <span aria-hidden="true">·</span>
                      <span
                        data-testid="provenance-author-chip"
                        className="inline-flex items-center px-1.5 py-0.5 rounded bg-muted/40 text-foreground"
                      >
                        @{msg.author}
                      </span>
                    </>
                  )}
                  {msg.platform && (
                    <span
                      data-testid="provenance-platform-pill"
                      className="inline-flex items-center px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 capitalize"
                    >
                      {msg.platform}
                    </span>
                  )}
                  {msg.channel && (
                    <span
                      data-testid="provenance-channel-pill"
                      className="inline-flex items-center px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                    >
                      #{msg.channel}
                    </span>
                  )}
                </div>
                {msg.snippet && (
                  <p className="mt-1 text-sm text-foreground/80 leading-snug">
                    {msg.snippet}
                  </p>
                )}
                {facts.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                    <span>contributed to:</span>
                    {facts.map((fid) => (
                      <span
                        key={fid}
                        data-testid="provenance-fact-chip"
                        className="px-1.5 py-0.5 rounded bg-muted/40 font-mono"
                      >
                        {fid}
                      </span>
                    ))}
                  </div>
                )}
                {msg.url && (
                  <div className="mt-1.5">
                    <a
                      href={msg.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
                    >
                      Open ↗
                    </a>
                  </div>
                )}
              </li>
            );
          })}
          {hiddenCount > 0 && !showAll && (
            <li>
              <button
                type="button"
                onClick={() => setShowAll(true)}
                className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
                data-testid="provenance-show-more"
              >
                Show {hiddenCount} more ▾
              </button>
            </li>
          )}
        </ul>
      )}
    </section>
  );
}
