import { AlertTriangle } from "lucide-react";

export interface WikiTension {
  fact_id: string;
  contradicts_fact_id: string;
  summary?: string;
  detected_at?: string;
}

interface Props {
  tensions: WikiTension[] | undefined;
  onResolve?: (factId: string, contradictsFactId: string) => void;
}

/**
 * Surfaces contradictions detected between extracted facts on a wiki
 * page. The contradiction detector flags pairs of facts that disagree;
 * this section renders them inline so the reader sees the disagreement
 * in context rather than discovering it only via a query.
 *
 * Renders nothing when the page has no tensions — keeps the page clean
 * for the common case.
 */
export function TensionsSection({ tensions, onResolve }: Props) {
  if (!tensions || tensions.length === 0) return null;

  return (
    <section className="mt-6 rounded-lg border border-amber-200 dark:border-amber-900/50 bg-amber-50/60 dark:bg-amber-950/20 px-4 py-3">
      <header className="flex items-center gap-2 mb-2">
        <AlertTriangle size={16} className="text-amber-600 dark:text-amber-500" />
        <h3 className="text-sm font-semibold text-amber-900 dark:text-amber-200">
          Tensions
          <span className="ml-2 text-xs font-normal text-amber-700/80 dark:text-amber-300/80">
            ({tensions.length} unresolved)
          </span>
        </h3>
      </header>
      <ul className="space-y-2">
        {tensions.map((t, i) => (
          <li
            key={`${t.fact_id}-${t.contradicts_fact_id}-${i}`}
            className="text-sm text-amber-900 dark:text-amber-100"
          >
            <p className="leading-snug">
              {t.summary || (
                <>
                  Fact <code className="text-xs font-mono">{t.fact_id.slice(0, 8)}</code>{" "}
                  contradicts{" "}
                  <code className="text-xs font-mono">
                    {t.contradicts_fact_id.slice(0, 8)}
                  </code>
                </>
              )}
            </p>
            {onResolve && (
              <button
                onClick={() => onResolve(t.fact_id, t.contradicts_fact_id)}
                className="mt-1 text-xs text-amber-700 dark:text-amber-300 hover:underline"
              >
                Mark resolved
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
