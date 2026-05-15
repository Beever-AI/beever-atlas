## Context

Beever Atlas is transitioning from a closed internal tool to a public
open-source project. Three IP questions must be answered before that
transition is complete:

1. **Who owns the copyright?** The Apache 2.0 license header must name an
   authoritative legal entity, not a diffuse "contributors" collective, so
   the company can relicense, dual-license, or defend IP in M&A and funding
   due diligence without chasing individual contributor consents.

2. **What trademarks does the company hold?** Without a documented policy,
   third parties can name forks after the project, register confusingly
   similar domains, or imply endorsement. The company needs a public record
   of the marks it claims, even before formal registration.

3. **What are contributors agreeing to?** DCO `Signed-off-by` trailers
   certify origin but do not transfer copyright. The company's IP strategy
   requires copyright assignment with a license-back. That CLA cannot go
   live until a lawyer approves the text, so Phase A documents the interim
   state honestly.

## Goals / Non-Goals

**Goals:**
- Update the copyright line in LICENSE and NOTICE to name "Beever AI Limited"
- Add a trademark reference in NOTICE pointing to TRADEMARK.md
- Publish a plain-English trademark policy for Beever-family marks
- Document the interim contribution IP posture in CONTRIBUTING.md
- Write a lawyer-facing CLA draft off-main for Phase B

**Non-Goals:**
- Shipping CLA.md to `main` before legal review (Phase B)
- Installing CLA-bot or DCO CI enforcement (Phase B)
- Filing trademark registrations (external, jurisdiction-specific)
- Retroactively auditing pre-existing external contributions

## Decisions

### D1: Copyright Assignment with License-Back (over DCO-only and Apache ICLA)

**Choice:** Adopt Harmony HA-CAA-I-ANY (Individual Copyright Assignment
Agreement) as the CLA template, supplemented with FSF-style license-back
language.

**Rationale:** Only copyright assignment gives the company unilateral
relicensing capability. DCO-only and Apache ICLA (license grant only) both
leave copyright with individual contributors, requiring unanimous consent
for any future relicensing event -- unworkable at scale.

**License-back is non-negotiable** (see D2). The combination of assignment
plus license-back is the Canonical / Google model and is widely accepted
in commercial OSS.

**Alternatives considered:**
- *DCO only*: Rejected -- no copyright transfer; fails M&A due diligence.
- *Apache ICLA*: Rejected -- license grant only; same relicensing problem.
- *In-bound = out-bound (Apache 2.0)*: Rejected -- company cannot relicense.

### D2: License-Back is Non-Negotiable

**Choice:** The CLA must grant contributors a perpetual, worldwide,
non-exclusive, royalty-free, irrevocable license back to their own
contributions.

**Rationale:** Copyright assignment without a license-back is hostile to
contributors. The Elasticsearch pre-fork is a cautionary example: SSPL
relicensing without a license-back caused a hard community fracture.
Contributors must be able to use their own code in other projects.

### D3: Ontario, Canada Governing Law

**Choice:** The CLA governing law clause specifies the Province of Ontario
and the federal laws of Canada applicable therein, with exclusive
jurisdiction of the courts of Ontario.

**Rationale:** Beever AI Limited is incorporated in Toronto, Ontario,
Canada. Using the company's home jurisdiction is conventional, defensible,
and avoids ambiguity about which law applies. Ontario has a well-developed
body of intellectual-property case law, making it a practical venue for
IP-related disputes.

### D4: A/B Phase Split -- CLA Off-Main Until Legal Review

**Choice:** Phase A ships ownership and trademark documentation. The CLA
draft lives in `.omc/plans/cla-draft-v1.md` (gitignored) until a qualified
IP lawyer approves the text. Phase B ships CLA.md + CLA-bot.

**Rationale:** Shipping a DRAFT CLA to `main` creates contributor chilling
effect without legal benefit. Contributors may refuse to submit PRs if they
see an unreviewed CLA. Keeping the draft off-main during legal review is
the responsible approach. The `.omc/` directory is listed in `.gitignore`
so the draft is never accidentally committed.

**Consequences:** Between Phase A and Phase B, contributions are implicitly
Apache 2.0 licensed. Contributors retain their copyright during this period.
CONTRIBUTING.md makes this explicit.

### D5: Beever-Family Trademark Scope Only

**Choice:** TRADEMARK.md covers only "Beever", "Beever Atlas", "Beever AI",
and the Beever logo. Votee-family marks are explicitly excluded with a
one-line rationale.

**Rationale:** Overreaching trademark claims (claiming marks the company
does not actually use in this repository) invite challenge and erode
community trust. Votee-family marks belong to separate Votee legal entities
and are not relevant to this repository.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| CLA enforced prematurely -- maintainer treats the draft as binding before legal review | Medium | High | CLA.md does not exist on `main`. CONTRIBUTING.md explicitly states CLA is "under development". |
| CLA draft contains hallucinated clauses with no legal basis | Medium | Medium | Every clause in the draft MUST cite a Harmony or FSF source via HTML comment. Acceptance check requires >= 8 citations. |
| Trademark list overreaches -- claiming marks not used in this repo | Low | Medium | Scoped to Beever-family only. All marks labeled TM (unregistered). |
| Phase B delayed indefinitely -- legal review never happens | Medium | Medium | CONTRIBUTING.md commits to a 4-week target. Follow-up issue tracked in RES-232. |
