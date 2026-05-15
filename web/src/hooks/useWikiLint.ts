import { useCallback, useState } from "react";
import { api } from "@/lib/api";

export interface LintFinding {
  severity: "info" | "warning" | "error";
  category: string;
  page_id: string;
  section_id?: string;
  message: string;
  suggested_action?: string;
}

export interface LintReport {
  channel_id: string;
  target_lang: string;
  findings: LintFinding[];
  pages_scanned: number;
  generated_at: string;
}

export function useWikiLint(channelId: string | null | undefined) {
  const [report, setReport] = useState<LintReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runLint = useCallback(async () => {
    if (!channelId) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.post<LintReport>(
        `/api/channels/${channelId}/wiki/lint`,
      );
      setReport(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Lint failed");
    } finally {
      setLoading(false);
    }
  }, [channelId]);

  const clear = useCallback(() => {
    setReport(null);
    setError(null);
  }, []);

  return { report, loading, error, runLint, clear };
}
