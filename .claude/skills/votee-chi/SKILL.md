---
name: votee-chi
description: Reply in Chinese for THIS invocation only. When /votee-chi is summoned, answer that single request in Chinese. All other turns stay in English by default. Use when the user wants a Chinese answer for one message.
---

# votee-chi — reply in Chinese (this turn only)

- When `/votee-chi` is invoked, reply to **that request in Chinese**.
- This is **per-invocation only** — it does NOT switch a persistent mode. The very next turn (if `/votee-chi` is not used again) goes **back to English** automatically.
- Default is always English. Chinese only appears when `/votee-chi` is summoned.
- Default to **Simplified Chinese (简体中文)**; use **Traditional (繁體)** if the user writes in Traditional or asks for it.

## What stays unchanged (do NOT translate)
- Brand/product names: Votee AI, Votee MAGIC, Beever Atlas, Beever AI, HappyHorse, HKCanto-Eval, Cantonese LLM.
- Slash commands, code, file paths, URLs, emails, hashtags — keep verbatim in English.
- Deliverables keep their own language: if the user wants an English social post/caption/poster, write the deliverable in English; only the surrounding chat explanation is in Chinese.

## Tone
- Natural, friendly, clear Chinese — not stiff machine translation. Technical terms may stay in English where that's how the team says them (LLM, on-prem, open-source, prompt injection).
