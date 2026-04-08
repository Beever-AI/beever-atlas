import { useState } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";
import type { WikiPageNode } from "@/lib/types";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";

interface WikiSidebarProps {
  pages: WikiPageNode[];
  activePageId: string;
  onNavigate: (pageId: string) => void;
}

interface SidebarItemProps {
  node: WikiPageNode;
  isActive: boolean;
  onClick: () => void;
  indent?: number;
}

function SidebarItem({ node, isActive, onClick, indent = 0 }: SidebarItemProps) {
  const fullTitle = [node.section_number, node.title].filter(Boolean).join(" ");

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <button
            onClick={onClick}
            aria-label={fullTitle}
            className={`flex items-center gap-2 w-full rounded-md px-3 py-1.5 text-left text-sm transition-colors ${
              isActive
                ? "bg-primary/10 text-primary border-l-2 border-primary font-medium"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            }`}
            style={{ paddingLeft: `${12 + indent * 16}px` }}
          >
            <span className="text-xs text-muted-foreground/70 font-mono shrink-0 min-w-[1.5rem]">{node.section_number}</span>
            <span className="truncate">{node.title}</span>
            {node.memory_count > 0 && (
              <span className="ml-auto text-xs text-muted-foreground/70">{node.memory_count}</span>
            )}
          </button>
        }
      />
      <TooltipContent side="right" className="text-xs">
        {fullTitle}
      </TooltipContent>
    </Tooltip>
  );
}

/** Check if any descendant of a node matches the active page ID */
function hasActiveChild(node: WikiPageNode, activePageId: string): boolean {
  for (const child of node.children) {
    if (child.id === activePageId) return true;
    if (hasActiveChild(child, activePageId)) return true;
  }
  return false;
}

interface TopicItemWithChildrenProps {
  node: WikiPageNode;
  activePageId: string;
  onNavigate: (pageId: string) => void;
  indent: number;
}

function TopicItemWithChildren({ node, activePageId, onNavigate, indent }: TopicItemWithChildrenProps) {
  const isActive = activePageId === node.id;
  const hasChildren = node.children.length > 0;
  const childIsActive = hasActiveChild(node, activePageId);
  const [userExpanded, setUserExpanded] = useState(false);
  const expanded = childIsActive || userExpanded;

  return (
    <div>
      <div className="flex items-center">
        {hasChildren && (
          <button
            onClick={() => setUserExpanded((prev) => !prev)}
            className="p-0.5 text-muted-foreground/60 hover:text-foreground shrink-0"
            style={{ marginLeft: `${8 + indent * 16}px` }}
            aria-label={expanded ? "Collapse sub-topics" : "Expand sub-topics"}
          >
            {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          </button>
        )}
        <div className="flex-1 min-w-0">
          <SidebarItem
            node={node}
            isActive={isActive}
            onClick={() => onNavigate(node.id)}
            indent={hasChildren ? 0 : indent + 1}
          />
        </div>
      </div>
      {hasChildren && expanded && (
        <div>
          {node.children.map((child) => (
            <SidebarItem
              key={child.id}
              node={child}
              isActive={activePageId === child.id}
              onClick={() => onNavigate(child.id)}
              indent={indent + 2}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function WikiSidebar({ pages, activePageId, onNavigate }: WikiSidebarProps) {
  const [topicsExpanded, setTopicsExpanded] = useState(true);
  const topicPages = pages.filter((p) => p.page_type === "topic");
  const fixedPages = pages.filter((p) => p.page_type === "fixed");

  const overviewPage = fixedPages.find((p) => p.id === "overview");
  const afterTopicPages = fixedPages.filter((p) => p.id !== "overview");

  return (
    <nav className="px-2 pb-4">
      {overviewPage && (
        <SidebarItem
          node={overviewPage}
          isActive={activePageId === overviewPage.id}
          onClick={() => onNavigate(overviewPage.id)}
        />
      )}

      {topicPages.length > 0 && (
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
            Topics
            <span className="ml-auto text-muted-foreground/70 normal-case font-normal">
              ({topicPages.length})
            </span>
          </button>
          {topicsExpanded &&
            topicPages.map((page) => (
              <TopicItemWithChildren
                key={page.id}
                node={page}
                activePageId={activePageId}
                onNavigate={onNavigate}
                indent={1}
              />
            ))}
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
