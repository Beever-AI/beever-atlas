"""Single-funnel wrappers around ``litellm.acompletion`` / ``litellm.aembedding``.

Every LLM call in the codebase routes through these helpers so the
:class:`~beever_atlas.services.llm_throttle.LLMThrottle` is the single
point of rate-limit accounting. Callers pass the provider explicitly —
this module never tries to infer the provider from the model string,
because LiteLLM accepts both prefixed (``gemini/...``) and bare
(``gpt-4o``) forms and the calling layer always knows which is which.

Side benefit: the singular dispatch path makes future cross-provider
features (failover, cost accounting, prompt-cache integration) trivial
to layer in without re-touching every call site.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from beever_atlas.services.llm_throttle import get_llm_throttle

logger = logging.getLogger(__name__)


def _estimate_completion_tokens(messages: Any) -> int:
    """Rough token estimate for a chat-completion request.

    LiteLLM exposes ``token_counter`` but it imports tiktoken eagerly and
    bumps cold-start by ~200ms. The 4-chars-per-token heuristic is
    conservative enough for throttle accounting (we'd rather over-budget
    by 2x than miss a 429 by under-budgeting). Floor of 1000 to cover
    response tokens which the bucket should account for too.
    """
    try:
        return max(len(str(messages)) // 4, 1000)
    except Exception:  # noqa: BLE001 — never crash on a token guess
        return 1000


def _estimate_embedding_tokens(payload: str | list[Any]) -> int:
    """Rough token estimate for an embedding request."""
    try:
        if isinstance(payload, str):
            return max(len(payload) // 4, 100)
        # list of strings (or mixed)
        total = sum(len(str(s)) for s in payload)
        return max(total // 4, 100)
    except Exception:  # noqa: BLE001
        return 100


def normalize_litellm_model(model_string: str) -> str:
    """Return a LiteLLM-compatible model identifier.

    Bare ``gemini-*`` strings (the legacy format read from ``LLM_FAST_MODEL`` /
    ``agent_model_config``) get normalised to ``gemini/<model>``. Already-prefixed
    strings (``openai/...``, ``anthropic/...``, ``ollama_chat/...``) pass through.

    Mirrors the inline normalisation inside
    :func:`beever_atlas.llm.model_resolver.resolve_model_object` so call sites
    that build their own ``dispatch_completion`` request can share the same
    prefix logic without depending on the resolver's LiteLlm wrapping path.
    """
    if "/" in model_string:
        return model_string
    if model_string.startswith("gemini-"):
        return f"gemini/{model_string}"
    return model_string


# Presets whose ``base_url`` carries an OpenAI-compatible shim by default.
# These speak the OpenAI HTTP shape (``POST <base>/chat/completions``) regardless
# of who's serving the other side, so routing them through LiteLLM's ``openai``
# provider sidesteps every native-API quirk (Gemini's ``/v1beta`` path, Ollama's
# ``/api/chat`` path, etc.).
_OPENAI_COMPAT_PRESETS: frozenset[str] = frozenset(
    {"vllm", "lmstudio", "openrouter", "litellm_proxy", "custom"}
)

# Embedding-only presets — chat dispatch is undefined for them; callers must
# route to ``dispatch_embedding`` instead. Kept here so ``route_for_endpoint``
# can refuse gracefully rather than emit a bogus model string.
_EMBEDDING_ONLY_PRESETS: frozenset[str] = frozenset({"jina_ai", "voyage", "cohere"})


def route_for_endpoint(
    preset: str,
    base_url: str | None,
    model: str,
) -> tuple[str, str, bool]:
    """Resolve ``(litellm_provider, litellm_model_id, drop_base_url)`` for a chat
    dispatch against an Endpoint with the given ``(preset, base_url, model)``.

    The third element ``drop_base_url`` is True when the caller MUST omit
    ``api_base`` from the LiteLLM call (the native-Gemini case — LiteLLM's
    ``gemini`` provider routes through Google's default host and breaks if a
    custom ``api_base`` is supplied).

    Routing matrix — picks the LiteLLM provider that talks the same HTTP shape
    as the actual server on the other end:

    * ``google_ai`` + base_url contains ``/openai/`` → ``openai`` provider, bare
      model (LiteLLM POSTs ``<base>/chat/completions`` to Google's OpenAI-compat
      shim — the native-Gemini path would 404 on ``/v1beta/openai/`` because it
      appends its own native path).
    * ``google_ai`` + any other base_url → ``gemini`` provider, ``gemini/<model>``,
      drop ``api_base`` (let LiteLLM hit Google's native default host).
    * ``ollama`` / ``ollama_chat`` + base_url ends in ``/v1`` → ``openai`` provider,
      bare model (Ollama's OpenAI-compat shim accepts ``POST /v1/chat/completions``).
      LiteLLM's ``ollama_chat`` provider POSTs to ``<base>/api/chat`` — with a
      ``/v1`` base_url that resolves to ``/v1/api/chat`` which 404s.
    * ``ollama`` / ``ollama_chat`` + native base_url → ``ollama_chat`` provider,
      ``ollama_chat/<model>`` (native ``/api/chat`` path).
    * Any preset in ``_OPENAI_COMPAT_PRESETS`` (vLLM, LM Studio, OpenRouter,
      LiteLLM Proxy, Custom) → ``openai`` provider, bare model.
    * Operator-supplied fully-prefixed model (``foo/bar``, not ``models/bar``)
      → trust the prefix, pass through.
    * Any other preset → use ``preset_to_provider(preset)`` and ``<provider>/<model>``;
      ``base_url`` is honoured (these accept their native URLs and operators may
      set a regional endpoint).
    * Embedding-only presets (jina_ai, voyage, cohere) — raise ``ValueError``;
      caller should route to ``dispatch_embedding`` instead.
    """
    from beever_atlas.llm.endpoints import preset_to_provider
    from beever_atlas.llm.model_resolver import SUPPORTED_PROVIDERS

    if preset in _EMBEDDING_ONLY_PRESETS:
        raise ValueError(
            f"route_for_endpoint: preset {preset!r} is embedding-only; "
            "route to dispatch_embedding instead of chat dispatch"
        )

    # Strip Gemini's native-API ``models/`` discovery prefix.
    bare_model = model.removeprefix("models/")
    base = (base_url or "").rstrip("/")

    # Preset-driven routing comes first so OpenAI-compat shims (vLLM, LM Studio,
    # OpenRouter, LiteLLM Proxy, Custom, plus the Google/Ollama OpenAI-compat
    # paths) reach LiteLLM's ``openai`` provider even when the model id carries
    # an HF-org slash (``meta-llama/Llama-3.3-70B``) — those slashes are NOT
    # LiteLLM provider prefixes and trusting them would mis-route. The Gemini
    # ``models/`` prefix has already been stripped above.

    if preset == "google_ai":
        if "/openai/" in (base_url or ""):
            return "openai", bare_model, False
        # Native Gemini path — drop the base_url so LiteLLM uses Google's default.
        return "gemini", f"gemini/{bare_model}", True

    if preset in ("ollama", "ollama_chat"):
        if base.endswith("/v1"):
            return "openai", bare_model, False
        return "ollama_chat", f"ollama_chat/{bare_model}", False

    if preset in _OPENAI_COMPAT_PRESETS:
        return "openai", bare_model, False

    # Operator-supplied fully-prefixed LiteLLM id wins — but only when the
    # prefix is a real LiteLLM provider (not e.g. an HF-org slash). Without
    # this gate, a vLLM Endpoint configured with ``meta-llama/Llama-3.3-70B``
    # was returning ``("meta-llama", ...)`` which then failed at dispatch.
    if "/" in bare_model:
        head = bare_model.split("/", 1)[0]
        if head in SUPPORTED_PROVIDERS or head == "ollama":
            return head, bare_model, False
        # Fall through — let the preset's native provider claim the model id.

    # Native LiteLLM provider (anthropic, mistral, groq, openai, …).
    provider = preset_to_provider(preset)
    return provider, f"{provider}/{bare_model}", False


def sniff_provider(model_string: str) -> str:
    """Extract the LiteLLM provider prefix from a model identifier.

    For prefixed strings (``openai/gpt-4o-mini``) returns ``openai``. For bare
    ``gemini-*`` strings returns ``gemini``. Other unprefixed strings default
    to ``gemini`` (the historical Atlas default; this matches the behaviour of
    the throttle which keyed on provider name before the per-Endpoint rework
    in PR-B).
    """
    if "/" in model_string:
        return model_string.split("/", 1)[0]
    if model_string.startswith("gemini-"):
        return "gemini"
    return "gemini"


def _is_ollama_connect_error(exc: BaseException, provider: str) -> bool:
    """Detect a connection failure against an Ollama Endpoint.

    Triggers a force-invalidation of ``LLMProvider._check_ollama_cached`` so
    a restarted daemon recovers without waiting the full 30s TTL window.
    See `agent-llm-provider-pluggable` design D8.
    """
    if not provider.startswith("ollama"):
        return False
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout))


def _is_429(exc: BaseException) -> bool:
    """Detect rate-limit errors from LiteLLM and from raw HTTP responses.

    LiteLLM wraps provider errors in ``litellm.RateLimitError``. Some
    providers (Gemini via the genai client path) surface 429 as a
    ``google.api_core.exceptions.ResourceExhausted`` with no LiteLLM
    wrapping; we still want the throttle to learn from those, so we
    sniff the exception's ``status_code`` / ``code`` attribute and the
    error message as a backstop.
    """
    try:
        import litellm  # type: ignore[import-untyped]

        if isinstance(exc, litellm.RateLimitError):
            return True
    except Exception:  # noqa: BLE001 — litellm import-time issues
        pass
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code == 429:
        return True
    msg = str(exc).lower()
    # ``rate_concurrency_limit_exceeded`` is Jina's per-tier cap on
    # in-flight requests (returned as a non-429 HTTP status with a
    # provider-specific code). It is functionally a rate-limit signal
    # — wait for in-flight requests to complete, then retry. Without
    # this branch the re-embed migration died silently the moment we
    # exceeded the free-tier ceiling of 2 concurrent embed calls.
    rate_limit_phrases = (
        "429",
        "rate limit",
        "rate_limit",
        "rate-limit",
        "concurrency limit",
        "concurrency_limit",
        "concurrency-limit",
        "too many requests",
        "rate_concurrency",
    )
    return any(phrase in msg for phrase in rate_limit_phrases)


def _split_model_for_litellm(provider: str, model: str) -> tuple[str, str]:
    """Return ``(litellm_model, litellm_custom_provider)`` for a dispatch call.

    LiteLLM accepts two equivalent forms for routing to a specific provider:

    * ``litellm.acompletion(model="<provider>/<id>", ...)`` — provider inferred
      from the prefix.
    * ``litellm.acompletion(model="<id>", custom_llm_provider="<provider>", ...)``
      — provider passed explicitly; bare ``<id>`` accepted.

    PR15 collapses to the second form **for every dispatch** because LiteLLM
    silently mis-routes when the bare model string happens to match an entry
    in its native model registry (e.g. ``gemini-2.5-flash`` reaches LiteLLM's
    native gemini provider, ignoring our OpenAI-compat ``api_base``) — and
    fails outright when the bare string matches nothing (e.g. ``gemma4:e2b``
    → ``LLM Provider NOT provided``). Always-pass-``custom_llm_provider`` is
    the only path LiteLLM treats as authoritative.

    Behaviour:
      * Matching ``<provider>/`` prefix → strip it; keep ``provider``. This is
        the canonical happy path: dispatch callers historically built
        ``<provider>/<id>`` strings (matching the ``route_for_endpoint``
        return) and we now collapse them to the bare form.
      * Non-matching slash → leave the model untouched, keep the routed
        ``provider``. The slash is treated as **part of the model id**, not
        as a provider hint. This covers two real cases:
          - HF-org-style ids on OpenAI-compat shims (vLLM, OpenRouter, etc.)
            like ``meta-llama/Llama-3.3-70B``. The shim accepts the slash
            verbatim and the LiteLLM provider we want is the OpenAI SDK.
          - OpenRouter's vendor-prefixed model ids like
            ``anthropic/claude-3-opus``. The route table sets
            ``provider=openai`` (route through LiteLLM's OpenAI SDK to
            OpenRouter's ``/v1/chat/completions``) and the model id stays
            ``anthropic/claude-3-opus`` because that's the OpenRouter
            model name — NOT a request for LiteLLM's native Anthropic SDK.
      * No slash → pass through bare; keep ``provider``.

    Net effect: ``provider`` (the routed value) always wins. ``model`` is the
    bare canonical id LiteLLM should receive.
    """
    if "/" in model:
        head, rest = model.split("/", 1)
        if head == provider:
            return rest, provider
        # Non-matching slash — the routed provider is authoritative; the slash
        # is part of the model id. See docstring for why this is correct for
        # vLLM / OpenRouter / vendor-prefixed proxies.
        return model, provider
    return model, provider


async def _acompletion_assembled_stream(litellm: Any, *, messages: Any, **kwargs: Any) -> Any:
    """Run a STREAMED completion and reassemble it into one ``ModelResponse``.

    Issue #223: a long, non-streaming completion (e.g. a 32k-token wiki page
    compile) sits idle past the ~130s edge-proxy ceiling and the peer closes the
    socket → ``aiohttp.ServerDisconnectedError``. Streaming keeps the socket warm
    with incremental chunks. We collect the chunks and rebuild them via
    ``litellm.stream_chunk_builder`` so the return value is the SAME response
    shape (``.choices[0].message.content`` + ``.usage``) that non-streaming
    callers already consume — every call site stays unchanged.
    """
    kwargs.pop("stream", None)  # this helper owns the stream flag
    stream = await litellm.acompletion(messages=messages, stream=True, **kwargs)
    chunks = [chunk async for chunk in stream]
    assembled = litellm.stream_chunk_builder(chunks, messages=messages)
    if assembled is None:
        raise RuntimeError("dispatch_completion(stream=True): stream produced no chunks")
    return assembled


async def dispatch_completion(
    *,
    provider: str,
    model: str,
    messages: list[Any],
    endpoint_id: str | None = None,
    timeout: float | None = None,
    stream: bool = False,
    **kwargs: Any,
) -> Any:
    """Throttle-gated wrapper around ``litellm.acompletion``.

    Caller passes ``provider`` explicitly (the static provider prefix —
    e.g. ``"gemini"``, ``"openai"``, ``"ollama"``) so the throttle keys
    on the rate-limited entity rather than the LiteLLM model string.

    ``endpoint_id`` is optional — when provided, the throttle bucket key
    becomes ``f"{provider}:{endpoint_id}"`` so two same-provider Endpoints
    get independent rate-limit state. Backward-compatible with PR-A callers
    that pass only ``(provider, model, messages)``.

    ``timeout`` is optional — when set, forwarded to LiteLLM as ``timeout=``
    (seconds). LiteLLM's openai SDK defaults to ~600s for connect+read; the
    Test Connection probe path passes a short value (~15s) so a hung probe
    fails fast with a clear error. Agent / ingestion dispatch paths leave
    this unset (long completions can legitimately exceed 60s).

    PR15: also forwards ``custom_llm_provider=<provider>`` to LiteLLM so
    OpenAI-compat routing (Google AI's ``/openai/`` shim, Ollama's ``/v1``
    shim, vLLM, LM Studio, OpenRouter, LiteLLM Proxy) reaches LiteLLM's
    ``openai`` SDK path regardless of how the bare model string looks —
    without this kwarg, ``gemma4:e2b`` raised ``LLM Provider NOT provided``
    and ``gemini-2.5-flash`` mis-routed to native Gemini ignoring api_base.
    """
    import litellm  # type: ignore[import-untyped]

    from beever_atlas.services.llm_call_log import _DISPATCH_OWNS_RECORDING, record_call

    throttle = get_llm_throttle()
    est_tokens = _estimate_completion_tokens(messages)
    litellm_model, litellm_provider = _split_model_for_litellm(provider, model)
    if timeout is not None:
        kwargs["timeout"] = timeout
    # Capture pre-call state so the recorder sees provider/model even if dispatch
    # raises before completing.
    api_base_for_log = kwargs.get("api_base") if isinstance(kwargs.get("api_base"), str) else None
    consumer_for_log = kwargs.pop("_log_consumer", None)
    started_at = time.monotonic()
    # Tell the ``CustomLogger`` in services/llm_call_log that we own the
    # ring-buffer entry for this call. The CustomLogger fires from inside
    # ``litellm.acompletion``'s await frame; without this contextvar guard,
    # every dispatch_completion call would record TWICE (once here, once
    # from the callback). Reset in ``finally`` so concurrent dispatch calls
    # on the same event loop each see their own value.
    _owns_recording_token = _DISPATCH_OWNS_RECORDING.set(True)
    try:
        async with throttle.acquire(provider, est_tokens, endpoint_id=endpoint_id):
            try:
                if stream:
                    # Issue #223 — stream long completions (e.g. 32k-token wiki
                    # page compiles) so the socket never idles into the ~130s
                    # edge-proxy disconnect. Reassembled to the normal shape.
                    response = await _acompletion_assembled_stream(
                        litellm,
                        model=litellm_model,
                        messages=messages,
                        custom_llm_provider=litellm_provider,
                        **kwargs,
                    )
                else:
                    response = await litellm.acompletion(
                        model=litellm_model,
                        messages=messages,
                        custom_llm_provider=litellm_provider,
                        **kwargs,
                    )
            except BaseException as exc:
                record_call(
                    started_at=started_at,
                    kind="completion",
                    consumer=consumer_for_log,
                    provider=litellm_provider,
                    model=litellm_model,
                    api_base=api_base_for_log,
                    exc=exc,
                )
                if _is_429(exc):
                    throttle.report_429(provider, endpoint_id=endpoint_id)
                elif _is_ollama_connect_error(exc, provider):
                    # Force-invalidate the Ollama health cache so the next
                    # ``resolve_model`` re-probes immediately rather than waiting
                    # out the 30s TTL. Defensive: never let this crash dispatch.
                    try:
                        from beever_atlas.llm.provider import get_llm_provider

                        get_llm_provider().invalidate_ollama_cache()
                    except Exception:  # noqa: BLE001
                        pass
                # Per-Endpoint circuit breaker: a non-429 failure is a real
                # outage signal — record it so the Endpoint's breaker can trip
                # and the resolver can route Assignments with a fallback away
                # from it. 429s are rate-limit, not outage — handled by the
                # throttle cooldown above, not the breaker. Defensive.
                if endpoint_id and not _is_429(exc):
                    await _record_breaker_failure(endpoint_id, exc)
                raise
            # Some providers return a 429 inline on the response body without
            # raising. Sniff status_code on the response just in case.
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                throttle.report_429(provider, endpoint_id=endpoint_id)
            elif endpoint_id:
                # Clean response — record success so a recovering Endpoint's
                # half-open probe closes the breaker.
                await _record_breaker_success(endpoint_id)
            record_call(
                started_at=started_at,
                kind="completion",
                consumer=consumer_for_log,
                provider=litellm_provider,
                model=litellm_model,
                api_base=api_base_for_log,
                response=response,
            )
            return response
    finally:
        _DISPATCH_OWNS_RECORDING.reset(_owns_recording_token)


async def _record_breaker_failure(endpoint_id: str, exc: BaseException) -> None:
    """Record a failure against the per-Endpoint circuit breaker. Never raises."""
    try:
        from beever_atlas.services.circuit_breaker import get_breaker_for_endpoint

        await get_breaker_for_endpoint(endpoint_id).record_failure(exc)
    except Exception:  # noqa: BLE001
        pass


async def _record_breaker_success(endpoint_id: str) -> None:
    """Record a success against the per-Endpoint circuit breaker. Never raises."""
    try:
        from beever_atlas.services.circuit_breaker import get_breaker_for_endpoint

        await get_breaker_for_endpoint(endpoint_id).record_success()
    except Exception:  # noqa: BLE001
        pass


async def dispatch_assignment(
    *,
    assignment: Any,
    messages: list[Any],
    **call_kwargs: Any,
) -> Any:
    """Preferred dispatch path for the new Endpoint+Assignment data model.

    Accepts a :class:`beever_atlas.llm.assignments.ResolvedAssignment` (typed as
    ``Any`` here to avoid a circular import) and forwards every per-call param
    to ``dispatch_completion``:

      * ``provider`` ← ``assignment.provider``
      * ``model`` ← ``assignment.litellm_model``
      * ``endpoint_id`` ← ``assignment.endpoint_id`` (per-Endpoint throttle key)
      * ``api_base`` ← ``assignment.base_url`` when set
      * ``api_key`` ← ``assignment.api_key`` when set (otherwise LiteLLM reads
        the provider-default env var)
      * ``aws_credentials`` / ``vertex_credentials`` ← when ``auth_type``
        is ``aws_iam`` / ``google_sa``
      * ``extra_headers`` ← merged from Endpoint + Assignment
      * ``temperature`` / ``max_tokens`` / ``response_format`` ← when set

    Caller-supplied ``call_kwargs`` override Assignment defaults (preserves
    agent code that needs tight control — e.g. an agent with its own JSON
    schema overrides the Assignment's ``response_format``).
    """
    kwargs: dict[str, Any] = {}
    if assignment.base_url:
        kwargs["api_base"] = assignment.base_url
    if assignment.api_key:
        kwargs["api_key"] = assignment.api_key
    elif (
        assignment.provider == "openai"
        and not getattr(assignment, "aws_credentials", None)
        and not getattr(assignment, "vertex_credentials", None)
    ):
        # OpenAI-compat routing with no api_key (e.g. ``auth_type=none`` for a
        # local Ollama / vLLM / LM Studio endpoint). LiteLLM's ``openai``
        # provider 400s client-side without a key even when the upstream
        # ignores it; pass a placeholder. Native LiteLLM providers (anthropic,
        # gemini, etc.) take their own path — leave them untouched.
        kwargs["api_key"] = "placeholder-no-auth"
    if getattr(assignment, "aws_credentials", None):
        kwargs["aws_access_key_id"] = assignment.aws_credentials["access_key_id"]
        kwargs["aws_secret_access_key"] = assignment.aws_credentials["secret_access_key"]
        kwargs["aws_region_name"] = assignment.aws_credentials["region"]
    if getattr(assignment, "vertex_credentials", None):
        kwargs["vertex_credentials"] = assignment.vertex_credentials["sa_json"]
    if assignment.extra_headers:
        kwargs["extra_headers"] = dict(assignment.extra_headers)
    if assignment.temperature is not None:
        kwargs["temperature"] = assignment.temperature
    if assignment.max_tokens is not None:
        kwargs["max_tokens"] = assignment.max_tokens
    if assignment.response_format is not None:
        # OpenAI shape: ``{"type": "json_object"}`` for JSON mode, ``{"type": "text"}`` otherwise.
        kwargs["response_format"] = {
            "type": ("json_object" if assignment.response_format == "json" else "text")
        }

    # Caller kwargs win — they're the most specific intent.
    kwargs.update(call_kwargs)
    # PR-λ: thread the consumer name into the call log so the debug UI can
    # show "qa_agent" instead of just "openai/gemini-3.1-flash-lite". Popped
    # back off by ``dispatch_completion`` before it reaches LiteLLM.
    if getattr(assignment, "consumer", None):
        kwargs["_log_consumer"] = assignment.consumer

    return await dispatch_completion(
        provider=assignment.provider,
        model=assignment.litellm_model,
        messages=messages,
        endpoint_id=assignment.endpoint_id,
        **kwargs,
    )


async def dispatch_embedding(
    *,
    provider: str,
    model: str,
    input: str | list[Any],
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Throttle-gated wrapper around ``litellm.aembedding``.

    ``timeout`` is optional — when set, forwarded to LiteLLM as ``timeout=``
    (seconds). Mirrors :func:`dispatch_completion`; the Test Connection probe
    path passes ~15s so a hung embedding probe fails fast.

    PR15: forwards ``custom_llm_provider=<provider>`` to LiteLLM for the same
    reason as :func:`dispatch_completion` — guarantees the routed provider
    matches the caller's intent regardless of bare-model-string heuristics.
    """
    import litellm  # type: ignore[import-untyped]

    from beever_atlas.services.llm_call_log import _DISPATCH_OWNS_RECORDING, record_call

    throttle = get_llm_throttle()
    est_tokens = _estimate_embedding_tokens(input)
    litellm_model, litellm_provider = _split_model_for_litellm(provider, model)
    if timeout is not None:
        kwargs["timeout"] = timeout
    api_base_for_log = kwargs.get("api_base") if isinstance(kwargs.get("api_base"), str) else None
    started_at = time.monotonic()
    # See ``dispatch_completion`` — same contextvar guard so the CustomLogger
    # in the ring buffer doesn't double-record this embedding call.
    _owns_recording_token = _DISPATCH_OWNS_RECORDING.set(True)
    # Per-call ``drop_params`` overrides any global state and bypasses most
    # of LiteLLM's pre-flight validation for provider-specific kwargs.
    kwargs.setdefault("drop_params", True)
    # PR-ζ.2: LiteLLM has a HARDCODED check (utils.py L3306-3315) that
    # raises ``UnsupportedParamsError`` when ``custom_llm_provider="openai"``
    # AND the model name doesn't contain ``text-embedding-3`` AND
    # ``dimensions`` is in the kwargs. The only escape hatch is
    # ``allowed_openai_params=["dimensions"]`` — ``drop_params`` does NOT
    # bypass this specific check.
    #
    # Affected paths in production: any non-OpenAI embedding model routed
    # through the ``openai`` provider for its OpenAI-compat shim — Gemini's
    # ``text-embedding-004`` (default 768-dim), Jina's ``jina-embeddings-*``
    # (variable dim), etc. These shims DO accept ``dimensions=`` natively;
    # LiteLLM's check is overly strict.
    if litellm_provider == "openai" and "dimensions" in kwargs:
        bare = litellm_model.split("/", 1)[-1]
        if "text-embedding-3" not in bare:
            allowed = list(kwargs.get("allowed_openai_params") or [])
            if "dimensions" not in allowed:
                allowed.append("dimensions")
            kwargs["allowed_openai_params"] = allowed
    try:
        async with throttle.acquire(provider, est_tokens):
            try:
                response = await litellm.aembedding(
                    model=litellm_model,
                    input=input,
                    custom_llm_provider=litellm_provider,
                    **kwargs,
                )
            except BaseException as exc:
                record_call(
                    started_at=started_at,
                    kind="embedding",
                    consumer=None,
                    provider=litellm_provider,
                    model=litellm_model,
                    api_base=api_base_for_log,
                    exc=exc,
                )
                if _is_429(exc):
                    throttle.report_429(provider)
                raise
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                throttle.report_429(provider)
            record_call(
                started_at=started_at,
                kind="embedding",
                consumer=None,
                provider=litellm_provider,
                model=litellm_model,
                api_base=api_base_for_log,
                response=response,
            )
            return response
    finally:
        _DISPATCH_OWNS_RECORDING.reset(_owns_recording_token)


__all__ = [
    "dispatch_completion",
    "dispatch_embedding",
    "dispatch_assignment",
    "normalize_litellm_model",
    "route_for_endpoint",
    "sniff_provider",
]
