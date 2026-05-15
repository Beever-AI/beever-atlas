"""``open_questions`` module — bullet list with raised-on dates.

Format: ``- **(raised YYYY-MM-DD)** Question text [N]``
The raised date helps readers spot stale questions.
"""

from __future__ import annotations

from typing import Any


def render(data: dict[str, Any]) -> str:
    questions = data.get("questions") or []
    if not isinstance(questions, list) or not questions:
        return ""

    lines: list[str] = []
    for q in questions:
        if not isinstance(q, dict):
            # Allow plain-string entries as a graceful fallback.
            text = str(q or "").strip()
            if text:
                lines.append(f"- {text}")
            continue
        text = (q.get("question") or q.get("text") or "").strip()
        if not text:
            continue
        raised = (q.get("raised") or q.get("date") or "").strip()
        cite = (q.get("citations") or "").strip()
        prefix = f"**(raised {raised})** " if raised else ""
        suffix = f" {cite}" if cite else ""
        lines.append(f"- {prefix}{text}{suffix}")
    return "\n".join(lines)
