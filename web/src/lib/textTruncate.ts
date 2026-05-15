/**
 * Sentence-aware text truncation.
 *
 * Most wiki cards truncate fact text to keep the UI scannable. The
 * legacy ``slice(0, N) + "..."`` approach often cut mid-word
 * ("...expressing...") which reads like data corruption. This helper
 * prefers to cut at a sentence terminator (".", "?", "!") within the
 * budget; falls back to the last whitespace boundary; and only as a
 * last resort does a hard truncate (rare — hits when the input has
 * neither sentence nor word boundaries inside ``maxLength``).
 *
 * The helper is shared across modules (key_facts,
 * cross_cutting_decisions, acronym_legend, ...) so the truncation
 * style is consistent across the page.
 */

/**
 * Truncate ``text`` to fit roughly within ``maxLength`` characters,
 * preferring sentence boundaries over word boundaries over hard cuts.
 *
 * Returns the original ``text`` unchanged when it already fits.
 *
 * @param text       Source string. Empty/null inputs return ``""``.
 * @param maxLength  Maximum length budget. The result MAY exceed this
 *                   only when a sentence terminator falls slightly
 *                   past the budget (we always cut at-or-before the
 *                   budget; never expand it).
 */
export function truncateAtSentence(
  text: string | null | undefined,
  maxLength: number,
): string {
  if (!text) return "";
  if (text.length <= maxLength) return text;
  // Slice the budget window first; we'll search for boundaries inside
  // that window so the resulting string is guaranteed ≤ maxLength.
  const slice = text.slice(0, maxLength);

  // Prefer the latest sentence terminator inside the budget. We
  // require the terminator to land past the halfway point so we
  // don't lop off most of the text just because the first sentence
  // ended early.
  const lastTerminator = Math.max(
    slice.lastIndexOf("."),
    slice.lastIndexOf("?"),
    slice.lastIndexOf("!"),
  );
  if (lastTerminator > maxLength * 0.5) {
    return slice.slice(0, lastTerminator + 1);
  }

  // Fallback: word boundary (last whitespace inside the budget).
  const lastSpace = slice.lastIndexOf(" ");
  if (lastSpace > maxLength * 0.5) {
    return slice.slice(0, lastSpace).replace(/[ ,;:]$/, "") + "…";
  }

  // Last resort: hard cut. Rare — happens only when the input has no
  // whitespace inside ``maxLength`` (single very long token).
  return slice + "…";
}
