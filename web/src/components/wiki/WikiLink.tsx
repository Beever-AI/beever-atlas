import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { buildWikiPath } from "@/lib/wikiNav";

interface WikiLinkProps {
  /** Bracketed title the LLM emitted, e.g. "Authentication". */
  title: string;
  /** Resolved slug — undefined means the link is broken. */
  slug?: string;
}

/**
 * Renders an inline `[[Page Title]]` reference.
 *
 * Resolved → a React Router `<Link>` that navigates to the channel-
 * scoped wiki page WITHOUT a full-page reload. Using `<a href>` here
 * would cause a top-level navigation that drops the SPA's auth
 * context + scroll position; `<Link>` keeps the routing client-side.
 * Unresolved → a "broken-link" badge (red dashed underline + tooltip)
 * that opens a placeholder modal explaining the page does not yet
 * exist. The modal is V1 — a future change will wire actual
 * page-creation through `propose_wiki_edit` once that surface lands.
 */
export function WikiLink({ title, slug }: WikiLinkProps) {
  const { id: channelId } = useParams<{ id: string }>();
  const [modalOpen, setModalOpen] = useState(false);

  if (slug && channelId) {
    return (
      <Link
        className="wiki-link text-primary hover:underline"
        to={buildWikiPath(channelId, slug)}
        data-testid="wiki-link"
        data-slug={slug}
      >
        {title}
      </Link>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setModalOpen(true)}
        className="inline-block text-red-500 dark:text-red-400 underline decoration-dashed decoration-2 underline-offset-2 hover:bg-red-500/10 rounded px-0.5 cursor-pointer"
        title={`No wiki page yet for "${title}". Click to suggest creation.`}
        data-testid="wiki-broken-link"
        data-title={title}
      >
        {title}
      </button>
      {modalOpen && (
        <CreatePagePlaceholderModal
          title={title}
          onClose={() => setModalOpen(false)}
        />
      )}
    </>
  );
}

interface CreatePagePlaceholderModalProps {
  title: string;
  onClose: () => void;
}

/**
 * V1 placeholder modal — explains the broken-link state and points the
 * operator to the curation flow that lands in §5. The full
 * page-creation flow (with template selection, channel-scoped slug
 * collision check, and operator approval) is intentionally deferred
 * to a follow-up change so this modal does not balloon in scope.
 */
function CreatePagePlaceholderModal({
  title,
  onClose,
}: CreatePagePlaceholderModalProps) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-background/70 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="Create wiki page"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold text-foreground">
          No page yet for &ldquo;{title}&rdquo;
        </h3>
        <p className="mt-2 text-sm text-muted-foreground">
          The wiki maintainer found a reference to this page but it has
          not been created yet. The next time the maintainer runs against
          a fact mentioning <span className="font-medium text-foreground">{title}</span>,
          a page will be auto-generated. Manual creation lands in a
          follow-up release.
        </p>
        <div className="mt-5 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-4 py-2 text-sm font-medium hover:bg-muted"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
