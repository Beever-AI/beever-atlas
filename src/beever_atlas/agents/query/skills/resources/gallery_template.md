# Media Gallery Template

Render image/file attachments returned by `search_media_references` as a markdown gallery. This section is MANDATORY whenever the tool returned ≥1 hit with a non-empty `media_urls` or `link_urls`.

## Format (copy exactly)

```
## Media

- ![<caption>](<first_url_from_media_urls_or_link_urls>)
  **<caption>** — <one-line context> [src:src_xxx inline]
- ![<caption>](<first_url_from_media_urls_or_link_urls>)
  **<caption>** — <one-line context> [src:src_xxx inline]
```

## Rules

- The `## Media` heading is REQUIRED. Place it at the end of the answer.
- Each bullet MUST open with a markdown image line `![caption](url)` — this is how the UI renders thumbnails. A bullet with only bold text + a `[src:...]` chip will render as plain text with no image.
- `url` = `media_urls[0]` if present, else `link_urls[0]`. Never invent a URL.
- `src_xxx` = the `_src_id` field from the tool result. Always include `inline` so PDFs and links are upgraded to preview cards.
- Caption = filename or short descriptive label from the tool result.
- Context line ≤ 80 characters; describes *why* this media is relevant.
- Group related media under one `##` heading; if ≥ 6 items, split with `### <sub-topic>` sub-headings while keeping the top-level `## Media`.
- Do NOT fold media into prose. The gallery section is separate from the prose answer above it.
- If the tool returned no media, omit the section entirely and say "No media attachments found for this query." in one sentence.

## Example

```
## Media

- ![login-v1.png](https://files.example.com/login-v1.png)
  **login-v1.png** — first mockup from @alice, August. [src:src_aaa1111111 inline]
- ![architecture.pdf](https://drive.example.com/architecture.pdf)
  **architecture.pdf** — system architecture spec. [src:src_bbb2222222 inline]
- ![demo-video](https://youtu.be/xyz)
  **Demo walkthrough** — end-to-end flow recorded by @bob. [src:src_ccc3333333 inline]
```
