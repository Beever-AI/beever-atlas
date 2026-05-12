import { useMemo, useState } from "react";
import { Plus } from "lucide-react";
import { useEndpoints } from "@/hooks/useEndpoints";
import { useAssignments } from "@/hooks/useAssignments";
import { useToast } from "@/hooks/useToast";
import { PRESET_LABELS } from "@/lib/aiSetup";
import {
  EndpointCard,
  type EndpointDiscoverResult,
  type EndpointTestResult,
} from "./EndpointCard";
import { AddEndpointPanel } from "./AddEndpointPanel";
import { EndpointsEmptyState } from "./EndpointsEmptyState";
import { ToastViewport } from "./ToastViewport";

/**
 * The Endpoint catalog page (``/settings/endpoints``). Composes the PR2
 * ``EndpointCard`` (one per endpoint) + ``AddEndpointPanel`` (inline, behind an
 * "Add endpoint" button) + ``useToast``/``ToastViewport``. Uses ``useEndpoints``
 * for CRUD/test/discover and ``useAssignments`` *read-only* (the ``usedByCount``
 * + a friendly "in use by …" message when a delete is blocked).
 */
export function EndpointsTab() {
  const ep = useEndpoints();
  const asn = useAssignments();
  const { toasts, show: showToast, dismiss: dismissToast } = useToast();

  const [showAdd, setShowAdd] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, EndpointTestResult | null>>({});
  const [discoverResults, setDiscoverResults] = useState<Record<string, EndpointDiscoverResult | null>>({});
  const [presetError, setPresetError] = useState<string | null>(null);

  // Read-only assignment usage map: how many assignments (primary OR fallback)
  // point at each endpoint.
  const usedByCount = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const a of asn.assignments) {
      if (a.endpoint_id) counts[a.endpoint_id] = (counts[a.endpoint_id] ?? 0) + 1;
      if (a.fallback_endpoint_id)
        counts[a.fallback_endpoint_id] = (counts[a.fallback_endpoint_id] ?? 0) + 1;
    }
    return counts;
  }, [asn.assignments]);

  // "in use by: …" — assignment consumers referencing a given endpoint.
  function consumersUsing(id: string): string[] {
    return asn.assignments
      .filter((a) => a.endpoint_id === id || a.fallback_endpoint_id === id)
      .map((a) => a.consumer);
  }

  async function handleTest(id: string) {
    setBusyId(id);
    setTestResults((p) => ({ ...p, [id]: null }));
    try {
      const r = await ep.test(id);
      setTestResults((p) => ({ ...p, [id]: { ok: r.ok, latency_ms: r.latency_ms, error: r.error } }));
      await ep.refetch();
      showToast(r.ok ? `Connection OK · ${r.latency_ms}ms` : `Test failed: ${r.error ?? "unknown"}`, r.ok ? "info" : "error");
    } catch (e: any) {
      setTestResults((p) => ({ ...p, [id]: { ok: false, latency_ms: null, error: e?.message ?? "test failed" } }));
      showToast(e?.message ?? "Test failed", "error");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDiscover(id: string) {
    setBusyId(id);
    setDiscoverResults((p) => ({ ...p, [id]: null }));
    try {
      const r = await ep.discover(id);
      if (r.ok && r.models.length > 0) {
        await ep.update(id, { models: r.models });
        setDiscoverResults((p) => ({ ...p, [id]: { ok: true, count: r.models.length, error: null } }));
        showToast(`Found ${r.models.length} models — added`);
      } else if (r.ok) {
        setDiscoverResults((p) => ({ ...p, [id]: { ok: true, count: 0, error: null } }));
        showToast("No models discovered", "error");
      } else {
        setDiscoverResults((p) => ({ ...p, [id]: { ok: false, count: 0, error: r.error } }));
        showToast(`Discover failed: ${r.error ?? "unknown"}`, "error");
      }
    } catch (e: any) {
      setDiscoverResults((p) => ({ ...p, [id]: { ok: false, count: 0, error: e?.message ?? "discover failed" } }));
      showToast(e?.message ?? "Discover failed", "error");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(id: string, name: string) {
    setBusyId(id);
    try {
      await ep.remove(id);
      setTestResults((p) => {
        const next = { ...p };
        delete next[id];
        return next;
      });
      setDiscoverResults((p) => {
        const next = { ...p };
        delete next[id];
        return next;
      });
      showToast(`Deleted '${name}'`);
    } catch (e: any) {
      const detail = e?.detail;
      const code = detail && typeof detail === "object" ? detail.error : undefined;
      if (code === "endpoint_in_use_as_primary_or_fallback") {
        const using = consumersUsing(id);
        const list = using.length > 0 ? using.join(", ") : "one or more agents";
        showToast(`Can't delete '${name}' — in use by: ${list}. Reassign those first.`, "error");
      } else {
        showToast(e?.message ?? "Failed to delete endpoint", "error");
      }
    } finally {
      setBusyId(null);
    }
  }

  async function handleApplyPreset(presetKey: string) {
    setPresetError(null);
    try {
      const result = await asn.applyPreset(presetKey);
      await ep.refetch();
      const label = PRESET_LABELS[presetKey] ?? presetKey;
      showToast(`Applied '${label}' — ${result.diff.length} updated`);
    } catch (e: any) {
      const detail = e?.detail;
      const code = detail && typeof detail === "object" ? detail.error : undefined;
      if (code === "preset_requirements_not_met") {
        const provider = (detail?.provider as string | undefined) ?? null;
        const msg = provider
          ? `This preset needs a ${provider} endpoint — add one first.`
          : "This preset needs an endpoint that isn't configured yet — add one first.";
        setPresetError(msg);
        showToast(msg, "error");
      } else {
        showToast(e?.message ?? "Failed to apply preset", "error");
      }
    }
  }

  const noEndpoints = !ep.isLoading && ep.endpoints.length === 0;

  return (
    <div className="space-y-4">
      {/* Intro */}
      <p className="text-sm text-muted-foreground max-w-3xl">
        An endpoint is a model provider you've connected — an API base URL + key, or a local Ollama.
        Add the ones you want, then point agents and the embedding model at them on the other tabs.
      </p>

      {ep.error && (
        <div className="text-xs text-destructive bg-destructive/10 rounded-md px-3 py-2">{ep.error}</div>
      )}

      {/* Add-endpoint affordance — inline expanding panel */}
      {!noEndpoints && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">
            {ep.endpoints.length} {ep.endpoints.length === 1 ? "endpoint" : "endpoints"} configured
          </span>
          {!showAdd && (
            <button
              type="button"
              onClick={() => setShowAdd(true)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90"
            >
              <Plus className="w-4 h-4" />
              Add endpoint
            </button>
          )}
        </div>
      )}

      {showAdd && (
        <AddEndpointPanel
          onCancel={() => setShowAdd(false)}
          onCreate={async (req) => {
            await ep.create(req);
            setShowAdd(false);
            setPresetError(null);
            showToast(`Endpoint '${req.name}' added`);
          }}
        />
      )}

      {presetError && (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive flex items-center gap-2 flex-wrap">
          <span>{presetError}</span>
          {!showAdd && (
            <button
              type="button"
              onClick={() => setShowAdd(true)}
              className="ml-auto rounded border border-destructive/40 px-2 py-0.5 font-medium hover:bg-destructive/15"
            >
              Add endpoint
            </button>
          )}
        </div>
      )}

      {/* List / empty state */}
      {ep.isLoading ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-32 rounded-xl bg-muted/40 animate-pulse" />
          ))}
        </div>
      ) : noEndpoints ? (
        !showAdd && (
          <EndpointsEmptyState
            onAdd={() => setShowAdd(true)}
            onApplyPreset={handleApplyPreset}
            busy={asn.isLoading}
          />
        )
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {ep.endpoints.map((e) => (
            <EndpointCard
              key={e.id}
              endpoint={e}
              usedByCount={usedByCount[e.id] ?? 0}
              busy={busyId === e.id}
              testResult={testResults[e.id] ?? null}
              discoverResult={discoverResults[e.id] ?? null}
              onTest={() => handleTest(e.id)}
              onDiscover={() => handleDiscover(e.id)}
              onDelete={() => handleDelete(e.id, e.name)}
            />
          ))}
        </div>
      )}

      <ToastViewport toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
