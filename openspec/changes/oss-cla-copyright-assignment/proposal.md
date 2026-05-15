## Why

Beever Atlas is opening to the public. Without clear IP posture the project
cannot relicense, cannot defend its brand, and cannot pass M&A or funding
due diligence. Copyright assignment was chosen over DCO-only and Apache ICLA
because only full assignment gives the company unilateral relicensing
capability. A two-phase rollout avoids shipping unreviewed legal text to
`main`: Phase A establishes ownership, trademark, and forward-looking CLA
intent without adding contributor friction; Phase B (follow-up PR, gated on
legal review) ships the actual CLA and enforcement tooling.

## What Changes

- `LICENSE` copyright line updated to "Copyright 2026 Beever AI Limited"
- `NOTICE` copyright line updated to match LICENSE; trademark reference to
  TRADEMARK.md added
- `TRADEMARK.md` created: unregistered-mark policy scoped to Beever-family
  marks only (Beever, Beever Atlas, Beever AI, the Beever logo); Votee-family
  marks explicitly excluded
- `CONTRIBUTING.md` updated: forward-looking CLA note added (RES-232
  tracking reference, interim Apache 2.0 terms, 4-week target); false
  "CI enforces the sign-off" claim replaced with honest state
- `.omc/plans/cla-draft-v1.md` written off-main for lawyer review (not
  committed to `main`)
- This openspec record (5 files)

## Capabilities

### New Capabilities

- `legal-ownership-attribution`: Explicit copyright ownership by Beever AI
  Limited declared in LICENSE and NOTICE. All subsequent files reference a
  single authoritative owner string, making relicensing and M&A due diligence
  tractable.
- `trademark-policy`: Unregistered trademark policy for Beever-family marks
  published in TRADEMARK.md. Covers permitted and prohibited uses in plain
  English. Votee-family marks explicitly out of scope. Contact address
  provided for licensing inquiries.
- `contributor-agreement-posture`: Forward-looking CLA policy documented in
  CONTRIBUTING.md. States current interim position (Apache 2.0, contributors
  retain copyright), RES-232 tracking reference, and 4-week target for CLA
  finalization. Does not create any obligation or restriction in Phase A.

### Modified Capabilities

(none -- this is a policy-only change)

## Impact

- **Runtime / code:** Zero. No source files (.ts, .py, .js, etc.) are
  modified. No build, test, or deployment changes.
- **Contributor flow:** No new friction in Phase A. Contributors continue
  to submit PRs under Apache 2.0 terms. Phase B will add a one-time CLA
  sign-off requirement, communicated in advance.
- **Legal posture:** Becomes explicit and consistent. Copyright ownership,
  trademark rights, and contribution terms are now documented in dedicated
  files that are straightforward to locate and audit.
