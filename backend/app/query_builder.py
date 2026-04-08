"""Build Gmail `q` strings from structured Advanced Search fields (Gmail web parity)."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class AdvancedSearch(BaseModel):
    """Fields aligned with Gmail Show search options + common operators."""

    raw: str = Field("", description="If non-empty, used as sole q (overrides structured).")

    from_addr: str = ""
    to_addr: str = ""
    subject: str = ""
    has_words: str = ""
    not_have: str = ""
    larger: str = ""
    smaller: str = ""
    after: str = ""
    before: str = ""
    older_than: str = ""
    newer_than: str = ""
    search_in: str = "anywhere"  # anywhere, inbox, trash, spam, sent, drafts, ...
    has_attachment: bool = False
    exclude_chats: bool = False

    # Extra operator fields (optional structured controls)
    cc: str = ""
    bcc: str = ""
    deliveredto: str = ""
    label: str = ""
    category: str = ""  # primary, social, promotions, ...
    is_read: bool | None = None
    is_unread: bool | None = None
    is_starred: bool | None = None
    is_important: bool | None = None
    is_muted: bool | None = None
    is_snoozed: bool | None = None
    filename: str = ""
    rfc822msgid: str = ""

    def to_q(self) -> str:
        raw_stripped = self.raw.strip()
        if raw_stripped:
            return raw_stripped
        parts: list[str] = []

        def esc(s: str) -> str:
            return s.strip()

        if self.from_addr:
            parts.append(f"from:{_quote_if_needed(esc(self.from_addr))}")
        if self.to_addr:
            parts.append(f"to:{_quote_if_needed(esc(self.to_addr))}")
        if self.subject:
            parts.append(f"subject:{_quote_if_needed(esc(self.subject))}")
        if self.cc:
            parts.append(f"cc:{_quote_if_needed(esc(self.cc))}")
        if self.bcc:
            parts.append(f"bcc:{_quote_if_needed(esc(self.bcc))}")
        if self.deliveredto:
            parts.append(f"deliveredto:{_quote_if_needed(esc(self.deliveredto))}")
        if self.filename:
            parts.append(f"filename:{_quote_if_needed(esc(self.filename))}")
        if self.rfc822msgid:
            parts.append(f"rfc822msgid:{_quote_if_needed(esc(self.rfc822msgid))}")
        if self.label:
            parts.append(f"label:{_quote_if_needed(esc(self.label))}")
        if self.category:
            parts.append(f"category:{esc(self.category).lower()}")

        for token in _split_free_text(self.has_words):
            parts.append(token)
        for token in _split_free_text(self.not_have):
            for neg in _negate_terms(token):
                parts.append(neg)

        if self.larger:
            parts.append(f"larger:{esc(self.larger)}")
        if self.smaller:
            parts.append(f"smaller:{esc(self.smaller)}")
        if self.after:
            parts.append(f"after:{esc(self.after)}")
        if self.before:
            parts.append(f"before:{esc(self.before)}")
        if self.older_than:
            parts.append(f"older_than:{esc(self.older_than)}")
        if self.newer_than:
            parts.append(f"newer_than:{esc(self.newer_than)}")

        si = esc(self.search_in).lower()
        if si and si != "anywhere":
            parts.append(f"in:{si}")

        if self.has_attachment:
            parts.append("has:attachment")

        if self.exclude_chats:
            parts.append("-label:chats")

        if self.is_read is True:
            parts.append("is:read")
        if self.is_unread is True:
            parts.append("is:unread")
        if self.is_starred is True:
            parts.append("is:starred")
        if self.is_important is True:
            parts.append("is:important")
        if self.is_muted is True:
            parts.append("is:muted")
        if self.is_snoozed is True:
            parts.append("is:snoozed")

        return " ".join(p for p in parts if p).strip()


def _quote_if_needed(s: str) -> str:
    if not s:
        return s
    if re.search(r'[\s"]', s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _split_free_text(s: str) -> list[str]:
    s = s.strip()
    if not s:
        return []
    # Respect quoted segments
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    escape = False
    for ch in s:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch.isspace() and not in_quote:
            if buf:
                out.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [t for t in out if t]


def _negate_terms(token: str) -> list[str]:
    t = token.strip()
    if not t:
        return []
    if t.startswith("-"):
        return [t]
    return [f"-{_quote_if_needed(t)}"]


def compile_search_payload(payload: dict[str, Any]) -> str:
    """Accept dict from JSON; return canonical q."""
    if str(payload.get("raw", "")).strip():
        return AdvancedSearch(raw=str(payload["raw"])).to_q()
    structured_keys = set(AdvancedSearch.model_fields) - {"raw"}
    has_struct = any(
        k in structured_keys and payload.get(k) not in (None, "", [], False)
        for k in payload
    )
    if payload.get("q") and str(payload["q"]).strip() and not has_struct:
        return str(payload["q"]).strip()
    data = {k: v for k, v in payload.items() if k in AdvancedSearch.model_fields}
    mapped = _map_frontend_keys(data)
    return AdvancedSearch(**mapped).to_q()


def _map_frontend_keys(data: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "from": "from_addr",
        "to": "to_addr",
        "search_in": "search_in",
    }
    out: dict[str, Any] = {}
    for k, v in data.items():
        key = aliases.get(k, k)
        if key in AdvancedSearch.model_fields:
            out[key] = v
    return out
