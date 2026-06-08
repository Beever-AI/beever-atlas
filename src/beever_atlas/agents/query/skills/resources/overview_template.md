# Entity Overview Card Template

Render a structured overview of ONE named entity (company / product / tool / project /
technology / concept) from `search_channel_facts` and/or `search_external_knowledge`
evidence. Works for both internal (channel) and external (web) answers.

## Format

```
**<Entity>** is <one-sentence what-it-is TL;DR>. [src:src_xxx]

**Quick facts**
- **<Label>:** <value> [src:src_xxx]
- **<Label>:** <value> [src:src_xxx]
- **<Label>:** <value> [src:src_xxx]

### <Facet heading, e.g. What it does>
- <fact-bearing sentence> [src:src_xxx]
- <fact-bearing sentence> [src:src_xxx]

### <Facet heading, e.g. Notable>
- <fact-bearing sentence> [src:src_xxx]
```

## Rules

- TL;DR: bold the entity name, one sentence, answers "what is it" before anything else.
- **Quick facts**: 2-6 bold-label bullets, one attribute per line. Include only the
  attributes the evidence supports — common ones: Founded, HQ / Location, Focus, Type,
  Owner / Maker, License, Released, Used for. OMIT any you don't have evidence for.
- DATA REPRESENTATION: bold-label bullets ONLY. Do NOT use a markdown table — it does
  not render on all chat platforms (e.g. Slack).
- 1-3 `###` sections after Quick facts, each a distinct facet, each with 2-4 bullets.
  Every bullet is a complete, fact-bearing sentence — never a one-word bullet.
- Enumerations: if each item carries its own detail (a fact, role, or date), give each
  its own bullet. A bare name-only list (e.g. partner/client names with no per-item
  detail) stays as ONE grouped bullet — never explode it into one-word bullets.
- Cite every claim with `[src:...]`. For external-knowledge answers, still cite the
  external sources and keep the honesty signal the tone rules require.
- Never invent a fact to fill the template. Fewer accurate bullets beat padded ones.

## Example

```
**Votee AI** is a Hong Kong-based company building tailored AI solutions for
businesses. [src:src_aaa1111111]

**Quick facts**
- **Founded:** 2012 [src:src_aaa1111111]
- **HQ:** Hong Kong [src:src_bbb2222222]
- **Focus:** generative AI and OCR [src:src_aaa1111111]

### What they do
- Build generative-AI and OCR products that help companies optimize processes and
  improve customer experience. [src:src_aaa1111111]
- Open-sourced Beever Atlas, an LLM knowledge base, with their Toronto lab Beever AI on
  May 8, 2026. [src:src_ccc3333333]

### Notable collaborators
- KPMG, Bloomberg, GoToDoctor, Amazon, and Bayer. [src:src_bbb2222222]
- Hong Kong University of Science and Technology. [src:src_bbb2222222]
```
