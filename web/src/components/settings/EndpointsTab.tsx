import { useMemo, useState } from "react";
import { Plus } from "lucide-react";
import { useEndpoints } from "@/hooks/useEndpoints";
import { useAssignments } from "@/hooks/useAssignments";
import { useToast } from "@/hooks/useToast";
import { PRESET_LABELS, type UpdateEndpointRequest } from "@/lib/aiSetup";
import {
  EndpointCard,
  type EndpointDiscoverResult,
  type EndpointTestResult,
} from "./EndpointCard";
import { AddEndpointPanel } from "./AddEndpointPanel";
import { EndpointsEmptyState } from "./EndpointsEmptyState";
import { ToastViewport } from "./ToastViewport";

/**
 * The Endpoint catalog page (``/settings/endpoints``). Composes the
 * ``EndpointCard`` grid (one per endpoint) + a single ``AddEndpointPanel``
 * *modal* (page-level) used for both create — when ``showAdd`` — and edit —
 * when ``editingId`` is set — + ``useToast``/``ToastViewport``. Uses
 * ``useEndpoints`` for CRUD/test/discover/model-edit and ``useAssignments``
 * *read-only* (the "used by N agents" line + a friendly "in use by …" message
 * when a delete is blocked).
 */
export function EndpointsTab() {
  const ep = useEndpoints();
  const asn = useAssignments();
  const { toasts, show: showToast, dismiss: dismissToast } = useToast();

  const [showAdd, setShowAdd] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
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
      setTestResults((p) => ({
        ...p,
        [id]: {
          ok: r.ok,
          latency_ms: r.latency_ms,
          error: r.error,
          // PR-β/γ: pass through the probed model + kind so the card can
          // surface "Test passed (probed jina-embeddings-v4, 187 ms)".
          probed_model: r.probed_model ?? null,
          probed_kind: r.probed_kind ?? null,
        },
      }));
      await ep.refetch();
      // PR-γ: when the backend told us which model it hit, weave that into
      // the success toast so the operator doesn't have to guess.
      const okMsg = r.probed_model
        ? `Test passed (probed ${r.probed_model}, ${r.latency_ms} ms)`
        : `Connection OK · ${r.latency_ms}ms`;
      showToast(r.ok ? okMsg : `Test failed: ${r.error ?? "unknown"}`, r.ok ? "info" : "error");
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
        setDiscoverResults((p) => ({
          ...p,
          [id]: {
            ok: true,
            count: r.models.length,
            error: null,
            // PR-γ: surface the by_kind + dropped_breakdown so the card can
            // render the richer Discover summary.
            by_kind: r.by_kind,
            dropped_breakdown: r.dropped_breakdown,
          },
        }));
        showToast(`Found ${r.models.length} models — added`);
      } else if (r.ok) {
        setDiscoverResults((p) => ({
          ...p,
          [id]: { ok: true, count: 0, error: null, by_kind: r.by_kind, dropped_breakdown: r.dropped_breakdown },
        }));
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

  async function handleUpdate(id: string, fallbackName: string, req: UpdateEndpointRequest) {
    setBusyId(id);
    try {
      // Let a failure propagate — ``AddEndpointPanel`` catches it and surfaces
      // the message inline (the modal stays open).
      await ep.update(id, req);
      setEditingId(null);
      showToast(`'${req.name?.trim() || fallbackName}' updated`);
    } finally {
      setBusyId(null);
    }
  }

  // Persist a model-list edit made directly on a card (chip ✕ / "+ add model").
  // Mirrors ``handleDiscover``'s ``ep.update(id, { models })`` write.
  async function handleUpdateModels(id: string, name: string, models: string[]) {
    setBusyId(id);
    try {
      await ep.update(id, { models });
      showToast(`'${name}' models updated`);
    } catch (e: any) {
      showToast(e?.message ?? "Failed to update models", "error");
    } finally {
      setBusyId(null);
    }
  }

  // PR-γ: promote one of ``advanced_models`` into the regular model list by
  // appending it to ``manually_kept``. The backend's Discover already
  // preserves ``manually_kept`` across re-Discover; calling ``ep.update``
  // directly with the extended array triggers the same merge logic so the
  // promoted model lands in ``models[]`` and is re-classified as kept.
  async function handlePromoteAdvanced(id: string, model: string) {
    const endpoint = ep.endpoints.find((x) => x.id === id);
    if (!endpoint) return;
    const next = Array.from(new Set([...(endpoint.manually_kept ?? []), model]));
    setBusyId(id);
    try {
      await ep.update(id, { manually_kept: next });
      showToast(`Promoted '${model}' to the model list`);
    } catch (e: any) {
      showToast(e?.message ?? `Failed to promote ${model}`, "error");
      // Re-throw so the card can clear its in-flight state.
      throw e;
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
  const editingEndpoint = editingId ? ep.endpoints.find((e) => e.id === editingId) ?? null : null;

  function openAdd() {
    setShowAdd(true);
    setEditingId(null);
    setPresetError(null);
  }

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

      {/* Header row — count + "Add endpoint" (opens the modal) */}
      {!noEndpoints && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground">
            {ep.endpoints.length} {ep.endpoints.length === 1 ? "endpoint" : "endpoints"} configured
          </span>
          <button
            type="button"
            onClick={openAdd}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90"
          >
            <Plus className="w-4 h-4" />
            Add endpoint
          </button>
        </div>
      )}

      {presetError && (
        <div className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive flex items-center gap-2 flex-wrap">
          <span>{presetError}</span>
          <button
            type="button"
            onClick={openAdd}
            className="ml-auto rounded border border-destructive/40 px-2 py-0.5 font-medium hover:bg-destructive/15"
          >
            Add endpoint
          </button>
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
        <EndpointsEmptyState onAdd={openAdd} onApplyPreset={handleApplyPreset} busy={asn.isLoading} />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 items-start">
          {ep.endpoints.map((e) => (
            <EndpointCard
              key={e.id}
              endpoint={e}
              usedByCount={usedByCount[e.id] ?? 0}
              usedByConsumers={consumersUsing(e.id)}
              busy={busyId === e.id}
              testResult={testResults[e.id] ?? null}
              discoverResult={discoverResults[e.id] ?? null}
              onEdit={() => {
                setEditingId(e.id);
                setShowAdd(false);
                setPresetError(null);
              }}
              onTest={() => handleTest(e.id)}
              onDiscover={() => handleDiscover(e.id)}
              onDelete={() => handleDelete(e.id, e.name)}
              onUpdateModels={(models) => handleUpdateModels(e.id, e.name, models)}
              onPromoteAdvanced={(model) => handlePromoteAdvanced(e.id, model)}
            />
          ))}
        </div>
      )}

      {/* Add / edit modal — page-level, one at a time */}
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
      {editingEndpoint && (
        <AddEndpointPanel
          mode="edit"
          existing={editingEndpoint}
          onCancel={() => setEditingId(null)}
          onUpdate={(req) => handleUpdate(editingEndpoint.id, editingEndpoint.name, req)}
        />
      )}

      <ToastViewport toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
