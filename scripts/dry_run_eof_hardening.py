"""Dry-run performance mock-up for the eliminate-llm-eof-errors change.

Exercises the two new code paths end-to-end **without** calling Gemini:

1. Output-aware adaptive batching — confirms throughput and parity vs. the
   pre-change (input-only) path on a realistic mixed-thread fixture.
2. Truncation-error detector — sanity-checks the widened predicate against
   every exception class the ingestion pipeline is known to raise.
3. Extractor construction under both flag settings — confirms the agents
   build cleanly with and without ``response_schema``.

Run:  uv run python scripts/dry_run_eof_hardening.py
"""

from __future__ import annotations

import json
import os
import statistics
import time
from typing import Any

from pydantic import BaseModel, ValidationError


# ---------------------------------------------------------------------------
# Fixture — realistic mixed-thread channel
# ---------------------------------------------------------------------------

def _build_fixture(n: int = 1000) -> list[dict[str, Any]]:
    """70% top-level messages, 30% thread replies, varied sizes."""
    msgs: list[dict[str, Any]] = []
    for i in range(n):
        is_reply = (i % 10 >= 7) and i > 0
        text_len = 200 if i % 3 == 0 else 600 if i % 3 == 1 else 1400
        msg: dict[str, Any] = {
            "text": f"msg {i} " + ("x" * text_len),
            "ts": str(1_700_000_000 + i),
        }
        if is_reply:
            msg["thread_ts"] = str(1_700_000_000 + i - 1)
        msgs.append(msg)
    return msgs


# ---------------------------------------------------------------------------
# Section 1 — adaptive batching parity + perf
# ---------------------------------------------------------------------------

def run_batching_benchmark() -> dict[str, Any]:
    from beever_atlas.services.adaptive_batcher import token_aware_batches

    msgs = _build_fixture(1000)

    def _time(fn, reps: int = 5) -> tuple[float, float]:
        samples = []
        for _ in range(reps):
            t0 = time.perf_counter()
            fn()
            samples.append(time.perf_counter() - t0)
        return statistics.mean(samples), max(samples)

    default_mean, default_max = _time(
        lambda: token_aware_batches(msgs, max_tokens=12_000)
    )
    output_mean, output_max = _time(
        lambda: token_aware_batches(
            msgs,
            max_tokens=12_000,
            max_output_tokens=90_000,
            max_facts_per_message=2,
        )
    )

    # Correctness checks.
    default_b = token_aware_batches(msgs, max_tokens=12_000)
    output_b = token_aware_batches(
        msgs, max_tokens=12_000, max_output_tokens=90_000
    )
    assert sum(len(b) for b in default_b) == 1000, "default dropped messages"
    assert sum(len(b) for b in output_b) == 1000, "output-aware dropped messages"

    # Tight budget behaviour.
    tight = token_aware_batches(
        msgs, max_tokens=100_000, max_output_tokens=2_000
    )
    assert len(tight) > len(default_b), "tight output budget should split more"

    return {
        "n_messages": 1000,
        "default_batches": len(default_b),
        "output_aware_batches": len(output_b),
        "default_mean_s": round(default_mean, 4),
        "default_max_s": round(default_max, 4),
        "output_mean_s": round(output_mean, 4),
        "output_max_s": round(output_max, 4),
        "overhead_pct": round((output_mean / default_mean - 1) * 100, 1),
    }


# ---------------------------------------------------------------------------
# Section 2 — truncation detector coverage
# ---------------------------------------------------------------------------

def run_truncation_detector_check() -> dict[str, Any]:
    from beever_atlas.services.batch_processor import _is_truncation_error

    class _M(BaseModel):
        x: int

    cases: list[tuple[str, Exception, bool]] = []

    try:
        _M(x="nope")  # type: ignore[arg-type]
    except ValidationError as e:
        cases.append(("ValidationError", e, True))
    try:
        json.loads("{")
    except json.JSONDecodeError as e:
        cases.append(("JSONDecodeError", e, True))

    class RemoteProtocolError(Exception):
        pass

    cases.extend(
        [
            ("RemoteProtocolError", RemoteProtocolError("peer closed"), True),
            ("max_tokens-string", RuntimeError("stop_reason=MAX_TOKENS"), True),
            ("unexpected-eof-string", RuntimeError("unexpected EOF"), True),
            ("json_invalid-string", RuntimeError("json_invalid"), True),
            ("KeyError-unrelated", KeyError("k"), False),
            ("ValueError-unrelated", ValueError("oops"), False),
        ]
    )

    results = []
    for label, exc, expected in cases:
        got = _is_truncation_error(exc)
        results.append({"case": label, "expected": expected, "got": got, "ok": got == expected})
    return {"cases": results, "all_pass": all(r["ok"] for r in results)}


# ---------------------------------------------------------------------------
# Section 3 — extractor construction under both flag settings
# ---------------------------------------------------------------------------

def run_extractor_build_check() -> dict[str, Any]:
    # Suppress Google API key requirement at import time.
    os.environ.setdefault("GOOGLE_API_KEY", "dry-run-key")

    from beever_atlas.infra.config import get_settings
    from beever_atlas.agents.ingestion.fact_extractor import create_fact_extractor
    from beever_atlas.agents.ingestion.entity_extractor import create_entity_extractor

    results: dict[str, Any] = {}
    for flag in (True, False):
        get_settings.cache_clear()
        os.environ["USE_LLM_STRUCTURED_OUTPUT"] = "true" if flag else "false"
        get_settings.cache_clear()

        # Pass explicit model to bypass the runtime LLM provider (not initialised
        # in a bare script).
        f = create_fact_extractor(model="gemini-2.5-flash")
        e = create_entity_extractor(model="gemini-2.5-flash")
        fact_cfg = f.generate_content_config
        ent_cfg = e.generate_content_config

        results[f"flag={flag}"] = {
            # ADK attaches the schema on LlmAgent.output_schema, not on generate_content_config.
            "fact_has_schema": getattr(f, "output_schema", None) is not None,
            "entity_has_schema": getattr(e, "output_schema", None) is not None,
            "fact_mime": getattr(fact_cfg, "response_mime_type", None),
            "entity_mime": getattr(ent_cfg, "response_mime_type", None),
            "fact_max_out": getattr(fact_cfg, "max_output_tokens", None),
            "entity_max_out": getattr(ent_cfg, "max_output_tokens", None),
        }

    # Cleanup → restore default (True).
    os.environ["USE_LLM_STRUCTURED_OUTPUT"] = "true"
    get_settings.cache_clear()
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 70)
    print("Dry-run: eliminate-llm-eof-errors")
    print("=" * 70)

    print("\n[1/3] Adaptive batcher — perf + parity")
    bench = run_batching_benchmark()
    for k, v in bench.items():
        print(f"  {k:<25} {v}")
    assert bench["default_mean_s"] < 1.0, "default batcher regression"
    assert bench["output_mean_s"] < 1.0, "output-aware batcher regression"
    assert bench["overhead_pct"] < 200, "output-aware overhead too large"

    print("\n[2/3] Truncation detector coverage")
    det = run_truncation_detector_check()
    for row in det["cases"]:
        ok = "✓" if row["ok"] else "✗"
        print(f"  {ok} {row['case']:<25} expected={row['expected']} got={row['got']}")
    assert det["all_pass"], "truncation detector coverage gap"

    print("\n[3/3] Extractor construction under both flag settings")
    built = run_extractor_build_check()
    for label, cfg in built.items():
        print(f"  {label}:")
        for k, v in cfg.items():
            print(f"    {k:<20} {v}")
    assert built["flag=True"]["fact_has_schema"], "flag=True should attach schema"
    assert built["flag=True"]["entity_has_schema"], "flag=True should attach schema"
    assert not built["flag=False"]["fact_has_schema"], "flag=False must not attach schema"
    assert not built["flag=False"]["entity_has_schema"], "flag=False must not attach schema"

    print("\n" + "=" * 70)
    print("OK — all dry-run checks passed")
    print("=" * 70)


if __name__ == "__main__":
    main()
