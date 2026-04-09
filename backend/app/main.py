"""FastAPI app: Gmail local manager API + static SPA."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from . import gmail_service as gs
from .cache_buckets import row_bucket_key
from .auth_store import clear_tokens, load_tokens, save_tokens
from .config import settings
from .jobs import job_manager
from .query_builder import compile_search_payload

_executor = ThreadPoolExecutor(max_workers=4)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class CompileBody(BaseModel):
    structured: dict[str, Any] = Field(default_factory=dict)
    q: str | None = None


class SyncBody(BaseModel):
    q: str = Field("", description="Gmail query; empty uses in:anywhere")
    gmail_list_page_size: int | None = Field(
        None,
        ge=1,
        le=500,
        description="messages.list maxResults per page (overrides env default if set)",
    )
    gmail_parallel_workers: int | None = Field(
        None,
        ge=1,
        le=128,
        description="Parallel threads for metadata.get during sync",
    )
    gmail_enrich_chunk_size: int | None = Field(
        None,
        ge=1,
        le=200,
        description="Messages per fetch chunk (smaller = more frequent progress)",
    )
    gmail_sync_chunk_pause_seconds: float | None = Field(
        None,
        ge=0.0,
        le=60.0,
        description="Pause after each metadata chunk (seconds)",
    )
    gmail_list_page_pause_seconds: float | None = Field(
        None,
        ge=0.0,
        le=60.0,
        description="Pause after each messages.list page (seconds)",
    )
    gmail_adaptive_sync: bool | None = Field(
        None,
        description="If set, ramp throughput until rate limits then back off",
    )


def _sync_rate_from_body(data: SyncBody) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    if data.gmail_list_page_size is not None:
        out["gmail_list_page_size"] = data.gmail_list_page_size
    if data.gmail_parallel_workers is not None:
        out["gmail_parallel_workers"] = data.gmail_parallel_workers
    if data.gmail_enrich_chunk_size is not None:
        out["gmail_enrich_chunk_size"] = data.gmail_enrich_chunk_size
    if data.gmail_sync_chunk_pause_seconds is not None:
        out["gmail_sync_chunk_pause_seconds"] = data.gmail_sync_chunk_pause_seconds
    if data.gmail_list_page_pause_seconds is not None:
        out["gmail_list_page_pause_seconds"] = data.gmail_list_page_pause_seconds
    if data.gmail_adaptive_sync is not None:
        out["gmail_adaptive_sync"] = data.gmail_adaptive_sync
    return out or None


class BulkBody(BaseModel):
    message_ids: list[str]
    action: Literal["archive", "trash", "read", "unread"]


class TrashQueueBody(BaseModel):
    message_ids: list[str]


def _wrap_email_html(inner: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<base target="_blank" rel="noopener noreferrer">'
        "<style>"
        "body{margin:12px;font-family:system-ui,Segoe UI,Roboto,sans-serif;font-size:14px;line-height:1.45;word-break:break-word;}"
        "img{max-width:100%!important;height:auto!important;}"
        "table{max-width:100%!important;}"
        "</style></head><body>"
        f"{inner}"
        "</body></html>"
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Gmail Filter Local", version="0.1.0")

    @app.on_event("startup")
    def _startup() -> None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db.init_db()

    # --- Auth ---
    @app.get("/api/auth/status")
    def auth_status() -> dict[str, Any]:
        t = load_tokens()
        return {"authenticated": bool(t and t.get("token"))}

    @app.get("/api/auth/google")
    def auth_google() -> RedirectResponse:
        if not settings.google_client_id or not settings.google_client_secret:
            raise HTTPException(
                status_code=500,
                detail="Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env",
            )
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [settings.redirect_uri],
                }
            },
            scopes=gs.SCOPES,
            redirect_uri=settings.redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return RedirectResponse(auth_url)

    @app.get("/api/auth/callback")
    def auth_callback(code: str | None = None, error: str | None = None) -> RedirectResponse:
        if error:
            raise HTTPException(status_code=400, detail=error)
        if not code:
            raise HTTPException(status_code=400, detail="missing code")
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [settings.redirect_uri],
                }
            },
            scopes=gs.SCOPES,
            redirect_uri=settings.redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials
        data = gs.save_credentials_to_dict(creds)
        data["client_id"] = settings.google_client_id
        data["client_secret"] = settings.google_client_secret
        save_tokens(data)
        return RedirectResponse("/")

    @app.post("/api/auth/logout")
    def auth_logout() -> dict[str, str]:
        clear_tokens()
        return {"ok": "true"}

    def _creds() -> dict[str, Any]:
        t = load_tokens()
        if not t:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return t

    def _service():
        return gs.build_service(gs.credentials_from_token_data(_creds()))

    @app.post("/api/search/compile")
    def compile_q(data: CompileBody) -> dict[str, str]:
        payload = {**data.structured, "q": (data.q or "").strip()}
        return {"q": compile_search_payload(payload)}

    # --- Gmail live list (accurate q) ---
    @app.get("/api/messages")
    async def list_messages(
        q: str = Query(""),
        page_token: str | None = None,
        limit: int = Query(
            200,
            ge=1,
            le=500,
            description="messages per page (Gmail API max 500)",
        ),
        enrich: bool = Query(
            True,
            description="Fetch From/Subject/Date/Snippet per message (extra API calls)",
        ),
        exclude_trash: bool = Query(
            True,
            description="Drop messages that have the TRASH label (moved to Trash in Gmail)",
        ),
    ) -> dict[str, Any]:
        loop = asyncio.get_event_loop()

        def work() -> dict[str, Any]:
            creds = gs.credentials_from_token_data(_creds())
            service = gs.service_for_thread(creds)
            raw = gs.list_message_ids(service, q, page_token, max_results=limit)
            refs = raw.get("messages") or []
            if not enrich or not refs:
                return raw

            def enrich_one(ref: dict[str, Any]) -> dict[str, Any] | None:
                mid = ref.get("id")
                tid = ref.get("threadId")
                if not mid:
                    return ref
                try:
                    svc = gs.service_for_thread(creds)
                    meta = gs.get_message_metadata(svc, mid, full_format=False)
                    row = gs.message_to_row(meta)
                    labels = row.get("label_ids") or []
                    if exclude_trash and "TRASH" in labels:
                        return None
                    return {
                        "id": row["id"],
                        "threadId": row["thread_id"],
                        "subject": (row["subject"] or "").strip() or "(no subject)",
                        "from": row["from_addr"] or "",
                        "snippet": row["snippet"] or "",
                        "internalDate": row["internal_date"],
                        "starred": "STARRED" in labels,
                        "important": "IMPORTANT" in labels,
                        "sent": "SENT" in labels,
                        "received": "INBOX" in labels,
                        "unread": "UNREAD" in labels,
                    }
                except gs.HttpError:
                    return {
                        "id": mid,
                        "threadId": tid,
                        "subject": "",
                        "from": "",
                        "snippet": "",
                        "internalDate": 0,
                        "starred": False,
                        "important": False,
                        "sent": False,
                        "received": False,
                        "unread": False,
                    }

            enriched: list[dict[str, Any]] = []
            chunk_sz = settings.gmail_enrich_chunk_size
            max_w = min(settings.gmail_parallel_workers, 16)
            pause_chunk = float(settings.gmail_sync_chunk_pause_seconds)
            for i in range(0, len(refs), chunk_sz):
                chunk = refs[i : i + chunk_sz]
                workers = min(max_w, max(1, len(chunk)))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for row in pool.map(enrich_one, chunk):
                        if row is not None:
                            enriched.append(row)
                if pause_chunk > 0 and i + chunk_sz < len(refs):
                    time.sleep(pause_chunk)
            out = {**raw, "messages": enriched}
            return out

        return await loop.run_in_executor(_executor, work)

    @app.get("/api/messages/{message_id}")
    async def get_message(message_id: str) -> dict[str, Any]:
        loop = asyncio.get_event_loop()

        def work() -> dict[str, Any]:
            service = _service()
            full = gs.get_message_metadata(service, message_id, full_format=True)
            preview = gs.extract_preview(full)
            inner = preview.get("html") or ""
            if inner:
                inner = gs.sanitize_html_for_iframe(inner)
            elif preview.get("plain_text"):
                pt = preview["plain_text"]
                esc = (
                    pt.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                inner = f"<pre style='white-space:pre-wrap;font-family:inherit'>{esc}</pre>"
            else:
                esc = (preview.get("snippet") or "").replace("&", "&amp;")
                inner = f"<p style='color:#666'>{esc or '(No body content)'}</p>"
            doc = _wrap_email_html(inner)
            return {
                "id": full.get("id"),
                "threadId": full.get("threadId"),
                "labelIds": full.get("labelIds") or [],
                "preview": {
                    "subject": preview.get("subject") or "",
                    "from": preview.get("from") or "",
                    "to": preview.get("to") or "",
                    "date": preview.get("date") or "",
                    "snippet": preview.get("snippet") or "",
                    "plain_text": preview.get("plain_text") or "",
                    "html_document": doc,
                },
            }

        return await loop.run_in_executor(_executor, work)

    # --- Aggregates (SQLite cache) ---
    @app.get("/api/aggregates")
    def aggregates(
        group_by: Literal["domain", "sender", "age", "newsletter"] = Query("domain"),
        top_n: int = Query(
            0,
            ge=0,
            le=500_000,
            description="Max groups to return, by descending count; 0 = all groups",
        ),
    ) -> dict[str, Any]:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                "SELECT id, internal_date, from_addr, label_ids FROM messages",
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        counts: dict[str, int] = {}
        for r in rows:
            d = dict(r)
            key = row_bucket_key(group_by, d)
            counts[key] = counts.get(key, 0) + 1

        ordered = sorted(counts.items(), key=lambda x: -x[1])
        if top_n > 0:
            ordered = ordered[:top_n]
        return {
            "group_by": group_by,
            "items": [{"key": k, "count": c} for k, c in ordered],
            "cached_total": sum(counts.values()),
        }

    @app.get("/api/cache/messages")
    def cache_messages(
        group_by: Literal["domain", "sender", "age", "newsletter"] = Query(...),
        key: str = Query(..., min_length=1, max_length=2000),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0, le=10_000_000),
    ) -> dict[str, Any]:
        """List messages from the local cache for one aggregate bubble bucket (matches bubble counts)."""
        conn = db.get_connection()
        try:
            cur = conn.execute(
                """
                SELECT id, thread_id, internal_date, snippet, subject, from_addr, label_ids
                FROM messages
                ORDER BY internal_date DESC
                """,
            )
            matched = 0
            page: list[dict[str, Any]] = []
            for r in cur:
                d = db.row_to_dict(r)
                if row_bucket_key(group_by, d) != key:
                    continue
                if matched >= offset and len(page) < limit:
                    lids = d.get("label_ids") or []
                    page.append(
                        {
                            "id": d["id"],
                            "threadId": d["thread_id"],
                            "subject": (d.get("subject") or "").strip() or "(no subject)",
                            "from": d.get("from_addr") or "",
                            "snippet": d.get("snippet") or "",
                            "internalDate": int(d.get("internal_date") or 0),
                            "starred": "STARRED" in lids,
                            "important": "IMPORTANT" in lids,
                            "sent": "SENT" in lids,
                            "received": "INBOX" in lids,
                            "unread": "UNREAD" in lids,
                        }
                    )
                matched += 1
        finally:
            conn.close()

        next_offset = offset + len(page) if offset + len(page) < matched else None
        return {
            "source": "cache",
            "group_by": group_by,
            "key": key,
            "total": matched,
            "messages": page,
            "next_offset": next_offset,
        }

    # --- Sync job ---
    @app.get("/api/settings/gmail-sync")
    def gmail_sync_settings() -> dict[str, Any]:
        """Current server defaults (from env); use as form defaults for sync rate overrides."""
        return {
            "gmail_list_page_size": settings.gmail_list_page_size,
            "gmail_parallel_workers": settings.gmail_parallel_workers,
            "gmail_enrich_chunk_size": settings.gmail_enrich_chunk_size,
            "gmail_sync_chunk_pause_seconds": settings.gmail_sync_chunk_pause_seconds,
            "gmail_list_page_pause_seconds": settings.gmail_list_page_pause_seconds,
            "gmail_adaptive_sync": settings.gmail_adaptive_sync,
        }

    @app.post("/api/sync/start")
    async def sync_start(data: SyncBody) -> dict[str, str]:
        q = data.q.strip() if data.q else ""
        if not q:
            q = "in:anywhere"
        rate = _sync_rate_from_body(data)
        jid = await job_manager.start_sync(_creds(), q, sync_rate=rate)
        return {"job_id": jid}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        j = job_manager.get(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="Job not found")
        return job_manager.to_public(j)

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> dict[str, str]:
        ok = job_manager.cancel(job_id)
        if not ok:
            raise HTTPException(status_code=400, detail="Cannot cancel")
        return {"ok": "true"}

    # --- Bulk ---
    @app.post("/api/messages/bulk")
    async def bulk_messages(data: BulkBody) -> dict[str, str]:
        if not data.message_ids:
            raise HTTPException(status_code=400, detail="No ids")
        jid = await job_manager.start_bulk(_creds(), data.message_ids, data.action)
        return {"job_id": jid}

    @app.post("/api/messages/trash/queue")
    async def trash_queue(data: TrashQueueBody) -> dict[str, Any]:
        """Enqueue messages for trash; one worker processes the queue (more requests can be queued)."""
        if not data.message_ids:
            raise HTTPException(status_code=400, detail="No message ids")
        try:
            return await job_manager.enqueue_trash(_creds(), data.message_ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # --- Static + SPA fallback ---
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():

        @app.get("/{full_path:path}", response_model=None)
        async def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith("api"):
                raise HTTPException(404)
            index = STATIC_DIR / "index.html"
            return FileResponse(index)

    return app


app = create_app()
