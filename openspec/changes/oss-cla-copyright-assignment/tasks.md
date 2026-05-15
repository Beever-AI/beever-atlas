## Phase A (this PR)

- [x] A1. Update LICENSE line 1 copyright to "Copyright 2026 Beever AI Limited"
- [x] A2. Update NOTICE copyright line to "Copyright 2026 Beever AI Limited" and add trademark reference to TRADEMARK.md
- [x] A3. Create TRADEMARK.md with Beever-family marks only (TM, not R), Votee exclusion rationale, `legal@beever.ai` contact
- [x] A4. Update CONTRIBUTING.md: forward-looking CLA note (RES-232 reference, interim Apache 2.0 terms, 4-week target) + fix false CI-enforcement claim
- [x] A5. Create openspec record (5 files: .openspec.yaml, proposal.md, design.md, tasks.md, specs/copyright-posture/spec.md)
- [x] A6. Write .omc/plans/cla-draft-v1.md off-main for lawyer review (DRAFT banner, >= 8 source citations, Ontario, Canada governing law, license-back)
- [x] A7. Run all Phase A acceptance checks and confirm pass

## Phase B (follow-up PR, gated on legal review)

- [ ] B1. Obtain qualified IP lawyer approval of .omc/plans/cla-draft-v1.md text
- [ ] B2. Finalize CLA.md from .omc/plans/cla-draft-v1.md (strip <!-- source: --> comments, remove DRAFT banner)
- [ ] B3. Update CONTRIBUTING.md: replace forward-looking note with CLA-enforcement section and link to CLA.md
- [ ] B4. Install and configure CLA-bot (.github/workflows/cla.yml or equivalent)
- [ ] B5. Add DCO CI enforcement alongside CLA-bot
- [ ] B6. Run all Phase B acceptance checks
