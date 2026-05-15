import { useState, useMemo, useEffect, type ComponentType } from "react";
import {
  ChevronRight,
  ChevronDown,
  Folder,
  FolderOpen,
  BookOpen,
  HelpCircle,
  BookText,
  Users,
  Clock,
  Library,
  FileText,
} from "lucide-react";
import type { WikiPageNode } from "@/lib/types";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { wikiT } from "@/lib/wikiI18n";

// Map a fixed-page slug/id to a recognizable icon. Fixed pages are
// always-present sections like Overview, FAQ, Glossary, etc. Operators
// scan for the icon faster than the numeric prefix that would
// otherwise lead the row.
type LucideIcon = ComponentType<{ className?: string; size?: number }>;

// Visual tuning constants — single source of truth so the cascade of
// indented rows stays predictable.
const INDENT_PX = 10; // per-level horizontal step (was 14 — too greedy at depth 3+)
const ROW_BASE_PADDING = 8;
const AUTO_EXPAND_MAX_DEPTH = 2; // root + first level of nesting expand by default

function iconForFixedPage(node: WikiPageNode): LucideIcon {
  const key = (node.slug || node.id || "").toLowerCase();
  if (key.includes("overview")) return BookOpen;
  if (key.includes("faq")) return HelpCircle;
  if (key.includes("glossary")) return BookText;
  if (key.includes("people") || key.includes("expert") || key.includes("member"))
    return Users;
  if (key.includes("activity") || key.includes("recent") || key.includes("timeline"))
    return Clock;
  if (key.includes("resource") || key.includes("media") || key.includes("link"))
    return Library;
  return FileText;
}

// Folder colour tone fades with depth so the eye groups by hierarchy.
function folderToneClasses(depth: number, expanded: boolean): string {
  if (depth <= 1) return expanded ? "text-primary" : "text-primary/80";
  if (depth === 2) return expanded ? "text-foreground/80" : "text-muted-foreground/80";
  return "text-muted-foreground/60";
}

// Strip a leading prefix (case-insensitive, word-boundary) from a
// title, falling back to the original if stripping leaves it empty
// or barely shorter than the prefix itself.
function stripPrefix(title: string, prefix: string): string {
  if (!prefix) return title;
  const t = title.trim();
  const p = prefix.trim();
  if (!t || !p) return title;
  // Word-boundary match: prefix at start, followed by space/punctuation/end.
  const re = new RegExp(`^${escapeRegExp(p)}(\\s*[:\\-–—·]?\\s*)`, "i");
  const stripped = t.replace(re, "").trim();
  if (!stripped) return title;
  return stripped;
}

interface WikiSidebarProps {
  pages: WikiPageNode[];
  activePageId: string;
  onNavigate: (pageId: string) => void;
  lang?: string;
}

interface SidebarItemProps {
  node: WikiPageNode;
  isActive: boolean;
  onClick: () => void;
  indent?: number;
  /** Display title override — used when an item lives inside a
   *  prefix-group and the common prefix has been stripped. */
  displayTitle?: string;
}

function SidebarItem({ node, isActive, onClick, indent = 0, displayTitle }: SidebarItemProps) {
  const fullTitle = [node.section_number, node.title].filter(Boolean).join(" ");
  const shownTitle = displayTitle ?? node.title;
  const isFixed = node.page_type === "fixed";
  const FixedIcon = isFixed ? iconForFixedPage(node) : null;
  // Sidebar icon dispatch — centralised so folder vs leaf vs fixed
  // pages render with consistent treatment:
  //   - fixed pages    → FixedIcon (BookOpen / HelpCircle / etc.)
  //   - leaf topics    → FileText (consistent file glyph, replaces
  //                      the prior `File` icon so leaves match the
  //                      file-icon family fixed pages use)
  //   - parent topics  → no icon (chevron lives upstream in TreeNode)
  // Section number renders to the LEFT of the icon (mono / muted)
  // so the numeric prefix is visually consistent with the body
  // numbering scheme.
  const isLeaf = !isFixed && (node.children?.length ?? 0) === 0;
  const showLeafIcon = isLeaf;

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            onClick={onClick}
            aria-label={fullTitle}
            aria-current={isActive ? "page" : undefined}
            className={`group/row relative flex items-start gap-1.5 w-full rounded-md py-1.5 pr-2 text-left text-[13px] leading-snug transition-colors ${
              isActive
                ? "bg-primary/10 text-primary border-l-2 border-primary font-medium"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            }`}
            data-testid={`sidebar-item-${node.id}`}
            data-leaf={isLeaf ? "true" : undefined}
            data-fixed={isFixed ? "true" : undefined}
            style={{ paddingLeft: `${ROW_BASE_PADDING + indent * INDENT_PX}px` }}
          >
            {!isFixed && node.section_number && (
              <span className="shrink-0 mt-0.5 text-[10.5px] text-muted-foreground/70 font-mono font-semibold tabular-nums">
                {node.section_number}
              </span>
            )}
            {FixedIcon ? (
              <span
                className="shrink-0 mt-0.5 flex h-4 w-5 items-center justify-center text-muted-foreground/75"
                data-testid="sidebar-icon-fixed"
              >
                <FixedIcon size={14} />
              </span>
            ) : showLeafIcon ? (
              <span
                className="shrink-0 mt-0.5 flex h-4 w-5 items-center justify-center text-muted-foreground/50"
                data-testid="sidebar-icon-leaf"
              >
                <FileText size={12} />
              </span>
            ) : (
              <span className="shrink-0 mt-0.5 w-5" aria-hidden="true" />
            )}
            <span className="flex-1 min-w-0 break-words line-clamp-2">{shownTitle}</span>
            {node.memory_count > 0 && (
              <span className="hidden group-hover/row:inline absolute right-2 top-1/2 -translate-y-1/2 text-[10.5px] text-muted-foreground/70 tabular-nums bg-muted/95 backdrop-blur-sm px-1.5 py-0.5 rounded">
                {node.memory_count}
              </span>
            )}
          </button>
        }
      />
      <TooltipContent side="right" className="text-xs max-w-xs">
        {fullTitle}
        {node.memory_count > 0 && (
          <span className="ml-1 text-muted-foreground/70">· {node.memory_count} memories</span>
        )}
      </TooltipContent>
    </Tooltip>
  );
}

/** Folder row — distinct visual treatment from topic rows so the
 *  hierarchy reads at a glance. */
interface FolderRowProps {
  node: WikiPageNode;
  isActive: boolean;
  expanded: boolean;
  onToggle: () => void;
  onNavigate: () => void;
  indent: number;
  childCount: number;
  totalMemories: number;
  depth: number;
  displayTitle?: string;
}

function FolderRow({
  node,
  isActive,
  expanded,
  onToggle,
  onNavigate,
  indent,
  childCount,
  totalMemories,
  depth,
  displayTitle,
}: FolderRowProps) {
  const fullTitle = [node.section_number, node.title].filter(Boolean).join(" ");
  const shownTitle = displayTitle ?? node.title;
  const FolderIcon = expanded ? FolderOpen : Folder;
  const folderTone = folderToneClasses(depth, expanded);

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <div
            className={`group/row relative flex items-start gap-1 w-full rounded-md py-1.5 pr-2 text-left text-[13px] leading-snug transition-colors ${
              isActive
                ? "bg-primary/10 text-primary border-l-2 border-primary font-semibold"
                : "text-foreground/90 hover:bg-muted"
            }`}
            style={{ paddingLeft: `${ROW_BASE_PADDING + indent * INDENT_PX}px` }}
            aria-label={fullTitle}
            aria-current={isActive ? "page" : undefined}
            data-testid={`sidebar-folder-${node.id}`}
            data-folder="true"
            data-expanded={expanded ? "true" : "false"}
          >
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onToggle();
              }}
              className="shrink-0 mt-0.5 p-0.5 -m-0.5 text-muted-foreground/70 hover:text-foreground rounded"
              aria-label={expanded ? "Collapse folder" : "Expand folder"}
              aria-expanded={expanded}
            >
              {expanded ? (
                <ChevronDown className="h-3 w-3" />
              ) : (
                <ChevronRight className="h-3 w-3" />
              )}
            </button>
            <button
              type="button"
              onClick={onNavigate}
              className="flex-1 min-w-0 flex items-start gap-1.5 text-left"
            >
              <span className={`shrink-0 mt-0.5 flex h-4 w-5 items-center justify-center ${folderTone}`}>
                <FolderIcon size={depth <= 1 ? 14 : 13} />
              </span>
              <span className={`flex-1 min-w-0 break-words line-clamp-2 ${depth <= 1 ? "font-semibold" : "font-medium"}`}>
                {shownTitle}
              </span>
              <span className="shrink-0 mt-0.5 text-[10.5px] text-muted-foreground/55 tabular-nums">
                {childCount}
              </span>
            </button>
            {totalMemories > 0 && (
              <span className="hidden group-hover/row:inline absolute right-2 top-1/2 -translate-y-1/2 text-[10.5px] text-muted-foreground/70 tabular-nums bg-muted/95 backdrop-blur-sm px-1.5 py-0.5 rounded">
                {totalMemories} memories
              </span>
            )}
          </div>
        }
      />
      <TooltipContent side="right" className="text-xs max-w-xs">
        {fullTitle}
        <span className="ml-1 text-muted-foreground/70">
          · {childCount} pages
          {totalMemories > 0 && ` · ${totalMemories} memories`}
        </span>
      </TooltipContent>
    </Tooltip>
  );
}

/** Recursively count direct + nested descendants of a node. */
function countDescendants(node: WikiPageNode): { count: number; memories: number } {
  let count = 0;
  let memories = node.memory_count ?? 0;
  for (const child of node.children) {
    count += 1;
    const sub = countDescendants(child);
    count += sub.count;
    memories += sub.memories;
  }
  return { count, memories };
}

/** Check if any descendant of a node matches the active page ID */
function hasActiveChild(node: WikiPageNode, activePageId: string): boolean {
  for (const child of node.children) {
    if (child.id === activePageId) return true;
    if (hasActiveChild(child, activePageId)) return true;
  }
  return false;
}

interface TreeNodeProps {
  node: WikiPageNode;
  activePageId: string;
  onNavigate: (pageId: string) => void;
  indent: number;
  /** Distance from the Topics section root. 1 == top-level folder/topic. */
  depth: number;
  displayTitle?: string;
  /** Stack of ancestor folder titles, root → immediate parent. Used
   *  to strip the WHOLE ancestor chain from this node's display title
   *  so a child of `[Beever Atlas Project] > [Development & Integration]`
   *  doesn't render as "Beever Atlas Project Development and Integration X". */
  ancestorTitles?: string[];
}

function TreeNode({
  node,
  activePageId,
  onNavigate,
  indent,
  depth,
  displayTitle,
  ancestorTitles = [],
}: TreeNodeProps) {
  const isActive = activePageId === node.id;
  const hasChildren = node.children.length > 0;
  const childIsActive = hasActiveChild(node, activePageId);
  const isFolder = node.page_type === "folder";

  // Auto-expand on first mount when shallow enough so users see the
  // structure without clicking. Deeper levels stay collapsed to avoid
  // a 100-row wall-of-text on regenerate.
  const initiallyExpanded = childIsActive || depth <= AUTO_EXPAND_MAX_DEPTH;
  const [userExpanded, setUserExpanded] = useState<boolean>(initiallyExpanded);
  // Re-evaluate auto-expansion when a different page becomes active so
  // navigating to a deep page reveals its ancestor chain.
  useEffect(() => {
    if (childIsActive) setUserExpanded(true);
  }, [childIsActive]);
  const expanded = userExpanded;

  // Inside a folder, redundant "<Folder Name> ..." prefixes on every
  // child page kill readability. Strip the WHOLE ancestor chain so
  // children of "Beever Atlas Project > Development & Integration"
  // don't render as "Beever Atlas Project Development and Integration X"
  // — try the longest (most specific) ancestor first, peeling back to
  // shorter ancestors until something strips successfully.
  let stripped = node.title;
  if (!displayTitle) {
    // Try concatenated ancestor chain first (longest prefix), then
    // each ancestor individually from immediate-parent outward.
    const concatChain = ancestorTitles.join(" ");
    if (concatChain) {
      const candidate = stripPrefix(stripped, concatChain);
      if (candidate !== stripped) stripped = candidate;
    }
    // Then try each ancestor individually (immediate parent first)
    // in case the title only echoes one level of the chain.
    for (let i = ancestorTitles.length - 1; i >= 0; i--) {
      const candidate = stripPrefix(stripped, ancestorTitles[i]);
      if (candidate !== stripped) stripped = candidate;
    }
  }
  const effectiveDisplayTitle = displayTitle ?? stripped;
  const childAncestors = isFolder ? [...ancestorTitles, node.title] : ancestorTitles;

  if (isFolder) {
    const { count, memories } = countDescendants(node);
    return (
      <div>
        <FolderRow
          node={node}
          isActive={isActive}
          expanded={expanded}
          onToggle={() => setUserExpanded((p) => !p)}
          onNavigate={() => onNavigate(node.id)}
          indent={indent}
          childCount={count}
          totalMemories={memories}
          depth={depth}
          displayTitle={effectiveDisplayTitle}
        />
        {expanded && hasChildren && (
          <div>
            {node.children.map((child) => (
              <TreeNode
                key={child.id}
                node={child}
                activePageId={activePageId}
                onNavigate={onNavigate}
                indent={indent + 1}
                depth={depth + 1}
                ancestorTitles={childAncestors}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  // Topic with optional sub-topic children — uses an inline chevron
  // (not the full FolderRow chrome) so it stays visually lighter than
  // a folder.
  if (hasChildren) {
    return (
      <div>
        <div className="flex items-start">
          <button
            onClick={() => setUserExpanded((p) => !p)}
            className="p-0.5 mt-1.5 text-muted-foreground/60 hover:text-foreground shrink-0"
            style={{ marginLeft: `${ROW_BASE_PADDING - 4 + indent * INDENT_PX}px` }}
            aria-label={expanded ? "Collapse sub-topics" : "Expand sub-topics"}
            aria-expanded={expanded}
          >
            {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          </button>
          <div className="flex-1 min-w-0">
            <SidebarItem
              node={node}
              isActive={isActive}
              onClick={() => onNavigate(node.id)}
              indent={0}
              displayTitle={effectiveDisplayTitle}
            />
          </div>
        </div>
        {expanded && (
          <div>
            {node.children.map((child) => (
              <TreeNode
                key={child.id}
                node={child}
                activePageId={activePageId}
                onNavigate={onNavigate}
                indent={indent + 1}
                depth={depth + 1}
                ancestorTitles={childAncestors}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  // Leaf
  return (
    <SidebarItem
      node={node}
      isActive={isActive}
      onClick={() => onNavigate(node.id)}
      indent={indent}
      displayTitle={effectiveDisplayTitle}
    />
  );
}

/**
 * Heuristic prefix-grouping for adjacent topic siblings.
 *
 * When 3+ adjacent topic pages share a multi-word prefix (e.g.
 * "Beever Atlas Documentation", "Beever Atlas GitHub Repository",
 * "Beever Atlas GitHub Star Growth Campaign"), the user's eye sees
 * "Beever Atlas..." 10 times in a row and can't tell them apart.
 *
 * Group them under a synthetic collapsible cluster labelled with the
 * shared prefix; each child item displays only its differentiating
 * tail. The user expands the cluster to access the items.
 */
type SidebarRow =
  | { kind: "item"; node: WikiPageNode }
  | { kind: "group"; prefix: string; items: WikiPageNode[]; key: string };

function commonWordPrefix(a: string, b: string): string {
  const wa = a.split(/\s+/);
  const wb = b.split(/\s+/);
  const out: string[] = [];
  const lim = Math.min(wa.length, wb.length);
  for (let i = 0; i < lim; i++) {
    if (wa[i].toLowerCase() === wb[i].toLowerCase()) {
      out.push(wa[i]);
    } else {
      break;
    }
  }
  return out.join(" ");
}

function groupByPrefix(nodes: WikiPageNode[]): SidebarRow[] {
  const out: SidebarRow[] = [];
  let i = 0;
  while (i < nodes.length) {
    let runEnd = i;
    let prefix = "";
    if (i + 1 < nodes.length) {
      const candidatePrefix = commonWordPrefix(nodes[i].title, nodes[i + 1].title);
      if (candidatePrefix.split(/\s+/).filter(Boolean).length >= 2) {
        runEnd = i + 1;
        prefix = candidatePrefix;
        for (let j = i + 2; j < nodes.length; j++) {
          const next = commonWordPrefix(prefix, nodes[j].title);
          if (next.split(/\s+/).filter(Boolean).length >= 2) {
            prefix = next;
            runEnd = j;
          } else {
            break;
          }
        }
      }
    }
    if (runEnd - i >= 2) {
      out.push({
        kind: "group",
        prefix,
        items: nodes.slice(i, runEnd + 1),
        key: `grp:${prefix}:${nodes[i].id}`,
      });
      i = runEnd + 1;
    } else {
      out.push({ kind: "item", node: nodes[i] });
      i += 1;
    }
  }
  return out;
}

interface PrefixGroupProps {
  prefix: string;
  items: WikiPageNode[];
  activePageId: string;
  onNavigate: (pageId: string) => void;
  indent: number;
  depth: number;
}

function PrefixGroup({ prefix, items, activePageId, onNavigate, indent, depth }: PrefixGroupProps) {
  const childActive = items.some(
    (n) => n.id === activePageId || hasActiveChild(n, activePageId),
  );
  // Synthetic prefix groups also auto-expand when shallow so the user
  // sees what's inside without an extra click.
  const [userExpanded, setUserExpanded] = useState<boolean>(
    childActive || depth <= AUTO_EXPAND_MAX_DEPTH,
  );
  useEffect(() => {
    if (childActive) setUserExpanded(true);
  }, [childActive]);
  const expanded = userExpanded;
  const totalMemories = items.reduce((s, n) => s + (n.memory_count ?? 0), 0);
  const folderTone = folderToneClasses(depth, expanded);
  const FolderIcon = expanded ? FolderOpen : Folder;

  return (
    <div>
      <button
        onClick={() => setUserExpanded((v) => !v)}
        className="group/row relative flex items-start gap-1.5 w-full rounded-md py-1.5 pr-2 text-left text-[13px] leading-snug text-foreground/85 hover:bg-muted hover:text-foreground transition-colors"
        style={{ paddingLeft: `${ROW_BASE_PADDING + indent * INDENT_PX}px` }}
        aria-expanded={expanded}
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3 mt-1 shrink-0 text-muted-foreground/70" />
        ) : (
          <ChevronRight className="h-3 w-3 mt-1 shrink-0 text-muted-foreground/70" />
        )}
        <span className={`shrink-0 mt-0.5 ${folderTone}`}>
          <FolderIcon size={13} />
        </span>
        <span className="flex-1 min-w-0 break-words line-clamp-2 font-medium italic">{prefix}</span>
        <span className="shrink-0 mt-0.5 text-[10.5px] text-muted-foreground/55 tabular-nums">
          {items.length}
        </span>
        {totalMemories > 0 && (
          <span className="hidden group-hover/row:inline absolute right-2 top-1/2 -translate-y-1/2 text-[10.5px] text-muted-foreground/70 tabular-nums bg-muted/95 backdrop-blur-sm px-1.5 py-0.5 rounded">
            {totalMemories} memories
          </span>
        )}
      </button>
      {expanded && (
        <div>
          {items.map((node) => {
            const stripped = stripPrefix(node.title, prefix);
            return (
              <TreeNode
                key={node.id}
                node={node}
                activePageId={activePageId}
                onNavigate={onNavigate}
                indent={indent + 1}
                depth={depth + 1}
                displayTitle={stripped}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function WikiSidebar({ pages, activePageId, onNavigate, lang }: WikiSidebarProps) {
  const [topicsExpanded, setTopicsExpanded] = useState(true);
  const folderPages = pages.filter((p) => p.page_type === "folder");
  const topicPages = pages.filter((p) => p.page_type === "topic");
  const fixedPages = pages.filter((p) => p.page_type === "fixed");

  const overviewPage = fixedPages.find((p) => p.id === "overview");
  const afterTopicPages = fixedPages.filter((p) => p.id !== "overview");

  const grouped = useMemo(() => groupByPrefix(topicPages), [topicPages]);

  return (
    <nav className="px-2 pb-4">
      {overviewPage && (
        <SidebarItem
          node={overviewPage}
          isActive={activePageId === overviewPage.id}
          onClick={() => onNavigate(overviewPage.id)}
        />
      )}

      {(folderPages.length > 0 || topicPages.length > 0) && (
        <div className="mt-1">
          <button
            onClick={() => setTopicsExpanded(!topicsExpanded)}
            className="flex items-center gap-1 w-full px-3 py-1.5 text-xs font-semibold text-muted-foreground uppercase tracking-wider hover:text-foreground"
          >
            {topicsExpanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            {wikiT(lang, "topics")}
            <span className="ml-auto text-muted-foreground/70 normal-case font-normal">
              ({folderPages.length + topicPages.length})
            </span>
          </button>
          {topicsExpanded &&
            folderPages.map((folder) => (
              <TreeNode
                key={folder.id}
                node={folder}
                activePageId={activePageId}
                onNavigate={onNavigate}
                indent={1}
                depth={1}
              />
            ))}
          {topicsExpanded && folderPages.length > 0 && topicPages.length > 0 && (
            <div className="mt-1 mb-0.5 px-3 pt-1 pb-0.5 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/55">
              <span className="h-px flex-1 bg-border/50" />
              <span>Other topics ({topicPages.length})</span>
              <span className="h-px flex-1 bg-border/50" />
            </div>
          )}
          {topicsExpanded &&
            grouped.map((row) => {
              if (row.kind === "group") {
                return (
                  <PrefixGroup
                    key={row.key}
                    prefix={row.prefix}
                    items={row.items}
                    activePageId={activePageId}
                    onNavigate={onNavigate}
                    indent={1}
                    depth={1}
                  />
                );
              }
              return (
                <TreeNode
                  key={row.node.id}
                  node={row.node}
                  activePageId={activePageId}
                  onNavigate={onNavigate}
                  indent={1}
                  depth={1}
                />
              );
            })}
        </div>
      )}

      {afterTopicPages.map((page) => (
        <SidebarItem
          key={page.id}
          node={page}
          isActive={activePageId === page.id}
          onClick={() => onNavigate(page.id)}
        />
      ))}
    </nav>
  );
}
