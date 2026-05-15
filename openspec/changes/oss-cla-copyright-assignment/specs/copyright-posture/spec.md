## ADDED Requirements

### Requirement: Explicit copyright ownership declared in LICENSE
The LICENSE file SHALL identify "Beever AI Limited" as the copyright holder
on line 1, and all subsequent lines of the Apache 2.0 license text SHALL
remain byte-identical to the upstream Apache 2.0 template.

#### Scenario: LICENSE line 1 names the correct legal entity
- **WHEN** the LICENSE file is read from the repository root
- **THEN** line 1 SHALL be exactly `Copyright 2026 Beever AI Limited`
- **AND** lines 2 onwards SHALL be byte-identical to the Apache License,
  Version 2.0 standard text

### Requirement: NOTICE declares trademark policy and consistent copyright
The NOTICE file SHALL contain the updated copyright line naming
"Beever AI Limited" and SHALL reference TRADEMARK.md for the full trademark
policy. The third-party attribution block below the separator SHALL remain
unchanged.

#### Scenario: NOTICE copyright line matches LICENSE
- **WHEN** the NOTICE file is read from the repository root
- **THEN** it SHALL contain the string "Copyright 2026 Beever AI Limited"
- **AND** it SHALL contain the string "TRADEMARK.md"

#### Scenario: Third-party attribution block is preserved verbatim
- **WHEN** the NOTICE file is compared against the prior version
- **THEN** the third-party dependency list (from the separator line
  onwards) SHALL be byte-identical to the previous version

### Requirement: Trademark policy covers Beever-family marks and excludes Votee marks
TRADEMARK.md SHALL list the Beever-family marks (Beever, Beever Atlas,
Beever AI, the Beever logo) with TM designation, explicitly state that
Votee-family marks are not claimed in this repository, and provide a
contact address for licensing inquiries.

#### Scenario: TRADEMARK.md uses TM, never (R)
- **WHEN** TRADEMARK.md is scanned for registered trademark symbols
- **THEN** it SHALL NOT contain "(R)" or the Unicode registered trademark
  character (U+00AE)

#### Scenario: TRADEMARK.md explicitly excludes Votee marks
- **WHEN** TRADEMARK.md is read
- **THEN** it SHALL contain a statement that Votee-family marks are not
  claimed in this repository (e.g., "not claimed in this repository")

#### Scenario: TRADEMARK.md provides a contact address
- **WHEN** TRADEMARK.md is read
- **THEN** it SHALL contain the string "legal@beever.ai"

### Requirement: CONTRIBUTING.md states interim contribution IP posture
CONTRIBUTING.md SHALL include a forward-looking CLA note that references
RES-232, states that contributions are accepted under Apache 2.0 terms
during the interim period, and does NOT contain any sign-off ritual,
affirmation language, or claim that CI enforces DCO sign-off.

#### Scenario: Forward-looking CLA note is present
- **WHEN** CONTRIBUTING.md is read
- **THEN** it SHALL contain the string "under development" in the context
  of the CLA
- **AND** it SHALL contain the string "RES-232"

#### Scenario: No pseudo-enforcement language present
- **WHEN** CONTRIBUTING.md is scanned for enforcement claims
- **THEN** it SHALL NOT contain the phrase "I have read and agree to the CLA"
- **AND** it SHALL NOT contain the phrase "CI enforces the sign-off"

### Requirement: CLA draft is available off-main for lawyer review
The file `.omc/plans/cla-draft-v1.md` SHALL exist in the working tree,
SHALL NOT be tracked by git (the `.omc/` directory is gitignored), SHALL
contain a DRAFT banner, at least 8 HTML source citations referencing
Harmony or FSF precedent, an Ontario, Canada governing law clause, and a
license-back clause. It SHALL NOT contain enforceability claims.

#### Scenario: CLA draft has DRAFT banner and source citations
- **WHEN** .omc/plans/cla-draft-v1.md is read
- **THEN** it SHALL contain the string "DRAFT"
- **AND** it SHALL contain at least 8 occurrences of "<!-- source:"
- **AND** it SHALL contain the string "Ontario"
- **AND** it SHALL contain a license-back clause (string "license-back"
  or "License-Back")
