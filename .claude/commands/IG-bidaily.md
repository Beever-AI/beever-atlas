---
description: IG-bidaily — daily 2-brand social tile generator (Votee + Beever)
argument-hint: [both|votee|beever|status|refill]
---

# IG-bidaily — Votee + Beever Daily Social Tiles

Generate ready-to-post social image drafts on demand. One Votee tile (Vivid Spectrum look) and
one Beever tile (Monolith Precision look) per run, each with a caption. **Draft only — the team
publishes.** Renderer + queues live in `.claude/social-factory/` (run from repo root).

## Mode (from `$ARGUMENTS`, default `both`)
- `both` (default) — one Votee + one Beever tile
- `votee` / `beever` — just that brand
- `status` — report unused idea counts; render nothing
- `refill` — top up both queues with ~8 fresh ideas each, then stop

## Run steps
1. **Render** (output dir is gitignored):
   ```bash
   python3 .claude/social-factory/social_factory.py --brand votee  --outdir .claude/social-factory/output
   python3 .claude/social-factory/social_factory.py --brand beever --outdir .claude/social-factory/output
   ```
   Each pops the next unused queue idea, writes a 1080×1350 PNG, and marks the idea used.
2. **Auto-refill on empty**: if a run prints `QUEUE_EMPTY` (exit 2), add ~8 ideas to
   `.claude/social-factory/queues/queue-<brand>.json` (`used: false`) then re-run.
   - Votee voice = Sovereign AI / ex-banker ROI / Cantonese pride. No banned phrases
     ("chatbots", "cloud-native", generic AI hype).
   - Beever = "wiki-first knowledge intelligence for team channels". "Beever Atlas, from Beever AI".
   - Idea schema: `{"id","kicker","head":[["line","dark"],["line","accent"]],"sub":["l1","l2"],"used":false}`.
     Headlines 2–3 words; accent line is the emphasis (orange for Votee, black for Beever).
3. **Caption** each tile in house voice (hook → value → link/contact → hashtags) + a comment-keyword CTA.
4. **Persist**: the script writes `used` flags back to the queue JSONs. Commit the updated
   `queue-*.json` so a future run never repeats an idea.
5. **Present** both PNGs with captions; report idea IDs used + remaining counts.

## Brand logo (Beever)
- Beever tile's bottom mark MUST be the official beaver-head crest at
  `.claude/social-factory/assets/logos/beever.png` — NOT a plain teardrop/drop. (Source of truth:
  repo `assets/logo-400.png`, background made transparent.)
- Votee tiles keep the geometric diamond-X mark (drawn in code).

## Guardrails
- **Never publish.** Output stays local; the team posts manually.
- One tile per brand per invocation unless asked.
- `status` → read the two queue JSONs, report counts, render nothing.
