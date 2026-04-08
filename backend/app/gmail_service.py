"""Gmail API helpers (sync client; use from thread pool for async routes)."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httplib2
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import settings

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def credentials_from_token_data(data: dict[str, Any]) -> Credentials:
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id") or settings.google_client_id,
        client_secret=data.get("client_secret") or settings.google_client_secret,
        scopes=data.get("scopes") or SCOPES,
    )


def save_credentials_to_dict(creds: Credentials) -> dict[str, Any]:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }


def _is_quota_or_rate_limit_error(err: HttpError) -> bool:
    status = getattr(err.resp, "status", None)
    if status == 429:
        return True
    if status != 403:
        return False
    raw = getattr(err, "content", b"") or b""
    if b"Quota exceeded" in raw or b"rateLimitExceeded" in raw or b"userRateLimitExceeded" in raw:
        return True
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        for e in data.get("error", {}).get("errors", []) or []:
            if e.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
                return True
    except (json.JSONDecodeError, TypeError):
        pass
    return False


def execute_gmail_request(
    request: Any,
    *,
    on_rate_limit: Callable[[], None] | None = None,
) -> Any:
    """Run a googleapiclient request.execute() with backoff on quota / rate limits."""
    delay = float(settings.gmail_retry_initial_delay_seconds)
    cap = float(settings.gmail_retry_max_delay_seconds)
    attempts = int(settings.gmail_retry_max_attempts)
    last: HttpError | None = None
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as e:
            last = e
            if _is_quota_or_rate_limit_error(e) and on_rate_limit:
                on_rate_limit()
            if not _is_quota_or_rate_limit_error(e) or attempt >= attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, cap)
    assert last is not None
    raise last


def build_service(creds: Credentials):
    """Use AuthorizedHttp so we get both OAuth credentials and a custom socket timeout."""
    http = httplib2.Http(timeout=settings.gmail_http_timeout_seconds)
    authed_http = AuthorizedHttp(creds, http=http)
    return build("gmail", "v1", http=authed_http, cache_discovery=False)


_tls = threading.local()


def service_for_thread(creds: Credentials):
    """One Gmail API client per OS thread — shared clients are not thread-safe."""
    if getattr(_tls, "gmail", None) is None:
        _tls.gmail = build_service(creds)
    return _tls.gmail


def b64url_decode(data: str) -> str:
    if not data:
        return ""
    pad = (-len(data)) % 4
    padded = data + ("=" * pad)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_address_header(headers: list[dict[str, str]], name: str) -> str:
    name_l = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_l:
            return h.get("value") or ""
    return ""


def message_to_row(full: dict[str, Any]) -> dict[str, Any]:
    mid = full.get("id", "")
    thread_id = full.get("threadId", "")
    internal_date = int(full.get("internalDate", 0))
    snippet = full.get("snippet") or ""
    payload = full.get("payload") or {}
    headers = payload.get("headers") or []
    if isinstance(headers, list):
        hdr_list = [{"name": h.get("name", ""), "value": h.get("value", "")} for h in headers]
    else:
        hdr_list = []
    subject = parse_address_header(hdr_list, "Subject")
    from_addr = parse_address_header(hdr_list, "From")
    to_addr = parse_address_header(hdr_list, "To")
    cc_addr = parse_address_header(hdr_list, "Cc")
    label_ids = full.get("labelIds") or []
    size_estimate = int(full.get("sizeEstimate") or 0)
    mime = (payload.get("mimeType") or "").lower()
    parts = payload.get("parts") or []
    has_attachment = "multipart/mixed" in mime or "multipart/related" in mime
    if parts:
        for p in parts:
            if str(p.get("filename") or "").strip():
                has_attachment = True
                break
            for sp in p.get("parts") or []:
                if str(sp.get("filename") or "").strip():
                    has_attachment = True
                    break
    return {
        "id": mid,
        "thread_id": thread_id,
        "internal_date": internal_date,
        "snippet": snippet,
        "subject": subject,
        "from_addr": from_addr,
        "to_addr": to_addr,
        "cc_addr": cc_addr,
        "label_ids": label_ids,
        "size_estimate": size_estimate,
        "has_attachment": has_attachment,
    }


def _walk_parts_for_body(payload: dict[str, Any], out: dict[str, str | None]) -> None:
    """Fill out['html'] / out['plain'] from MIME parts (prefer first html, first plain)."""
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    raw = body.get("data")
    if raw:
        text = b64url_decode(str(raw))
        if text:
            if "text/html" in mime and not out.get("html"):
                out["html"] = text
            elif "text/plain" in mime and not out.get("plain"):
                out["plain"] = text
    for part in payload.get("parts") or []:
        _walk_parts_for_body(part, out)


def extract_preview(full: dict[str, Any]) -> dict[str, Any]:
    """Parse full message resource into UI-friendly preview (headers + html/plain)."""
    payload = full.get("payload") or {}
    headers = payload.get("headers") or []
    hdr_list: list[dict[str, str]] = []
    if isinstance(headers, list):
        hdr_list = [{"name": h.get("name", ""), "value": h.get("value", "")} for h in headers]

    subject = parse_address_header(hdr_list, "Subject")
    from_addr = parse_address_header(hdr_list, "From")
    to_addr = parse_address_header(hdr_list, "To")
    date_hdr = parse_address_header(hdr_list, "Date")

    bodies: dict[str, str | None] = {"html": None, "plain": None}
    mime_root = (payload.get("mimeType") or "").lower()
    body_obj = payload.get("body") or {}
    if body_obj.get("data") and "multipart" not in mime_root:
        text = b64url_decode(str(body_obj.get("data")))
        if text:
            if "text/html" in mime_root:
                bodies["html"] = text
            else:
                bodies["plain"] = text
    _walk_parts_for_body(payload, bodies)

    html = bodies.get("html")
    plain = bodies.get("plain")
    if not html and plain:
        esc = plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f"<pre style='white-space:pre-wrap;font-family:inherit'>{esc}</pre>"

    snippet = full.get("snippet") or ""

    return {
        "subject": subject,
        "from": from_addr,
        "to": to_addr,
        "date": date_hdr,
        "snippet": snippet,
        "html": html,
        "plain_text": plain or "",
    }


def sanitize_html_for_iframe(html: str) -> str:
    """Strip script tags and on* handlers for safer srcDoc display."""
    if not html:
        return ""
    s = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.I | re.S)
    s = re.sub(r"\son\w+\s*=\s*([\"'])[^\"']*\1", "", s, flags=re.I)
    s = re.sub(r"\son\w+\s*=\s*[^\s>]+", "", s, flags=re.I)
    return s


def list_message_ids(
    service,
    q: str,
    page_token: str | None,
    max_results: int = 100,
    *,
    on_rate_limit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_results, "includeSpamTrash": True}
    if q:
        kwargs["q"] = q
    if page_token:
        kwargs["pageToken"] = page_token
    return execute_gmail_request(
        service.users().messages().list(**kwargs),
        on_rate_limit=on_rate_limit,
    )


def get_message_metadata(
    service,
    msg_id: str,
    full_format: bool = False,
    *,
    on_rate_limit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if full_format:
        return execute_gmail_request(
            service.users().messages().get(userId="me", id=msg_id, format="full"),
            on_rate_limit=on_rate_limit,
        )
    return execute_gmail_request(
        service.users()
        .messages()
        .get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "To", "Subject", "Cc", "Date"],
        ),
        on_rate_limit=on_rate_limit,
    )


def batch_modify(
    service,
    ids: list[str],
    *,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> None:
    body: dict[str, Any] = {"ids": ids}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels
    execute_gmail_request(service.users().messages().batchModify(userId="me", body=body))


def trash_message(service, msg_id: str) -> None:
    """Move a message to Trash (batchModify cannot add the TRASH system label)."""
    execute_gmail_request(service.users().messages().trash(userId="me", id=msg_id))


__all__ = [
    "execute_gmail_request",
    "SCOPES",
    "credentials_from_token_data",
    "save_credentials_to_dict",
    "build_service",
    "service_for_thread",
    "message_to_row",
    "extract_preview",
    "sanitize_html_for_iframe",
    "list_message_ids",
    "get_message_metadata",
    "batch_modify",
    "trash_message",
    "HttpError",
]
