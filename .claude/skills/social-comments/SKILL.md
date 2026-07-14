---
name: social-comments
description: Generate a batch of ready-to-paste engagement comments for a social media post (LinkedIn/IG). Use when the user shares a post (text or link) and wants comments to seed/boost engagement. Default output is ~60 plain lines, no numbers, blank line between each.
---

# Social Comments Generator (`/social-comments`)

## Input
- User pastes the post text (or a link). If login-gated (LinkedIn/IG/SCMP), ask them to paste the text.
- Optional args: a number (default 60) and/or a style steer ("questions", "technical", "B2B/professional", "memey", "congrats/replies").

## Output format
- Plain lines only. No numbering/labels/headers. One blank line between each comment. Copy-paste ready.
- End with a one-line count + offer for another batch / different skew.

## Buckets (mix, weighted to the post)
- CTA-keyword seeds — if the post says Comment "XYZ", seed many lines that are just that keyword (some with emoji).
- Relatable reactions to the pain point.
- Supportive / congratulatory (event/people posts).
- Insight-adding (one substantive sentence).
- Genuine questions that pull replies.
- Short & punchy / emoji.

## Voice & guardrails
- Votee = sovereign AI / ex-banker ROI / Cantonese pride. Beever = wiki-first knowledge intelligence for team chat. Natural + human, not corporate.
- Vary length/emoji so it reads like real people. No banned phrases for Votee. Beever Atlas is "from Beever AI", not a Votee product.
- Keep claims consistent with the post; don't invent facts. Drafts to seed — the user posts them.
