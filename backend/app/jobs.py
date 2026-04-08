"""Background jobs: sync + bulk modify with cooperative cancel."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import database as db
from . import gmail_service as gs
from .config import settings as app_settings


class JobStatus(str, Enum):
    running = "running"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class AdaptiveSyncPacer:
    """Tune chunk pause and parallelism: ramp on success streak, backoff on rate limits."""

    def __init__(self, initial_pause: float, max_workers_cap: int) -> None:
        self.max_workers_cap = max(1, min(max_workers_cap, 16))
        # Start below the user's cap and ramp up until quota errors, then backoff.
        self.max_workers = max(1, (self.max_workers_cap + 1) // 2)
        p = max(0.0, min(60.0, float(initial_pause)))
        self.pause_chunk = p if p <= 0 else min(60.0, p * 1.25)
        self.success_streak = 0

    def backoff(self, rate_limit_events: int) -> None:
        self.success_streak = 0
        ev = max(1, min(rate_limit_events, 8))
        mult = 1.2 + 0.08 * float(ev)
        self.pause_chunk = min(60.0, self.pause_chunk * mult)
        self.max_workers = max(1, self.max_workers - 1)

    def ramp(self) -> None:
        self.success_streak += 1
        if self.success_streak >= 5:
            self.success_streak = 0
            self.pause_chunk = max(0.0, self.pause_chunk * 0.9)
            self.max_workers = min(self.max_workers_cap, self.max_workers + 1)


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = JobStatus.running
    phase: str = ""
    processed: int = 0
    total_hint: int | None = None
    message: str = ""
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Optional per-request overrides for sync (see SyncBody); None = use app settings.
    sync_rate: dict[str, Any] | None = None
    # Rolling 60s Gmail API request count (sync only); updated during sync.
    requests_per_minute: float | None = None
    adaptive_pause_seconds: float | None = None
    adaptive_workers: int | None = None
    # trash_queue job: rolling log for UI (newest appended; trimmed in worker)
    deleted_recent: list[dict[str, str]] = field(default_factory=list)
    queue_remaining: int = 0


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._trash_pending: deque[str] = deque()
        self._trash_pending_set: set[str] = set()
        self._trash_queue_lock = threading.Lock()
        self._trash_queue_job_id: str | None = None
        self._trash_queue_event = threading.Event()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def to_public(self, job: Job) -> dict[str, Any]:
        pct: float | None = None
        if job.total_hint and job.total_hint > 0:
            pct = min(100.0, 100.0 * job.processed / job.total_hint)
        return {
            "id": job.id,
            "kind": job.kind,
            "status": job.status.value,
            "phase": job.phase,
            "processed": job.processed,
            "total_hint": job.total_hint,
            "percent": pct,
            "message": job.message,
            "error": job.error,
            "requests_per_minute": job.requests_per_minute,
            "adaptive_pause_seconds": job.adaptive_pause_seconds,
            "adaptive_workers": job.adaptive_workers,
            "deleted_recent": job.deleted_recent[-50:] if job.deleted_recent else [],
            "queue_remaining": job.queue_remaining,
        }

    def cancel(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if not j or j.status != JobStatus.running:
            return False
        if j.kind == "trash_queue":
            with self._trash_queue_lock:
                self._trash_pending.clear()
                self._trash_pending_set.clear()
            self._trash_queue_event.set()
        j.cancel_event.set()
        j.message = "Cancelling…"
        return True

    def _finish(self, job: Job, status: JobStatus, msg: str = "", err: str | None = None) -> None:
        job.status = status
        job.message = msg
        job.error = err
        job.updated_at = time.time()

    async def start_sync(
        self,
        creds_dict: dict[str, Any],
        q: str,
        sync_rate: dict[str, Any] | None = None,
    ) -> str:
        jid = str(uuid.uuid4())
        job = Job(
            id=jid,
            kind="sync",
            phase="listing",
            message="Starting sync…",
            sync_rate=sync_rate,
        )
        self._jobs[jid] = job
        asyncio.create_task(self._run_sync(job, creds_dict, q))
        return jid

    async def _run_sync(self, job: Job, creds_dict: dict[str, Any], q: str) -> None:
        loop = asyncio.get_event_loop()

        def work() -> None:
            conn = db.get_connection()
            sr = job.sync_rate or {}

            def sync_opt(name: str, default: Any) -> Any:
                v = sr.get(name)
                return default if v is None else v

            list_page_size = int(sync_opt("gmail_list_page_size", app_settings.gmail_list_page_size))
            list_page_size = max(1, min(500, list_page_size))
            parallel_workers = int(sync_opt("gmail_parallel_workers", app_settings.gmail_parallel_workers))
            parallel_workers = max(1, min(128, parallel_workers))
            chunk_sz = int(sync_opt("gmail_enrich_chunk_size", app_settings.gmail_enrich_chunk_size))
            chunk_sz = max(1, min(200, chunk_sz))
            pause_chunk = float(sync_opt("gmail_sync_chunk_pause_seconds", app_settings.gmail_sync_chunk_pause_seconds))
            pause_chunk = max(0.0, min(60.0, pause_chunk))
            pause_list = float(sync_opt("gmail_list_page_pause_seconds", app_settings.gmail_list_page_pause_seconds))
            pause_list = max(0.0, min(60.0, pause_list))
            max_w = min(parallel_workers, 16)
            adaptive = bool(sync_opt("gmail_adaptive_sync", app_settings.gmail_adaptive_sync))
            pacer = AdaptiveSyncPacer(pause_chunk, max_w) if adaptive else None

            try:
                creds = gs.credentials_from_token_data(creds_dict)
                service = gs.service_for_thread(creds)
                page_token = None
                job.phase = "listing"
                listed = 0
                cumulative_list_ids = 0
                job.requests_per_minute = 0.0
                if adaptive and pacer:
                    job.adaptive_pause_seconds = pacer.pause_chunk
                    job.adaptive_workers = pacer.max_workers
                else:
                    job.adaptive_pause_seconds = None
                    job.adaptive_workers = None
                rpm_lock = threading.Lock()
                req_times: deque[float] = deque()

                def record_api_call() -> None:
                    with rpm_lock:
                        now = time.time()
                        req_times.append(now)
                        while req_times and req_times[0] < now - 60.0:
                            req_times.popleft()
                        job.requests_per_minute = float(len(req_times))

                while True:
                    if job.cancel_event.is_set():
                        self._finish(job, JobStatus.cancelled, f"Stopped after {listed} messages.")
                        return
                    job.phase = "listing"
                    job.message = "Requesting message list from Gmail…"
                    job.updated_at = time.time()
                    rl_list_ct = [0]

                    def on_list_rl() -> None:
                        rl_list_ct[0] += 1

                    try:
                        resp = gs.list_message_ids(
                            service,
                            q,
                            page_token,
                            max_results=list_page_size,
                            on_rate_limit=on_list_rl if adaptive else None,
                        )
                    except gs.HttpError as e:
                        self._finish(job, JobStatus.failed, err=str(e))
                        return
                    record_api_call()
                    if pause_list > 0:
                        time.sleep(pause_list)
                    if adaptive and pacer and rl_list_ct[0] > 0:
                        pacer.backoff(rl_list_ct[0])
                        time.sleep(
                            min(
                                30.0,
                                float(app_settings.gmail_retry_initial_delay_seconds)
                                * min(rl_list_ct[0], 4),
                            )
                        )
                        job.adaptive_pause_seconds = pacer.pause_chunk
                        job.adaptive_workers = pacer.max_workers
                        job.updated_at = time.time()
                    ids = [m["id"] for m in resp.get("messages") or []]
                    cumulative_list_ids += len(ids)
                    next_page_token = resp.get("nextPageToken")
                    # resultSizeEstimate is often wrong for small queries; exact total is the sum of
                    # message IDs returned across all list pages (known once a page has no nextPageToken).
                    if next_page_token is None:
                        job.total_hint = cumulative_list_ids
                    else:
                        est = resp.get("resultSizeEstimate")
                        if isinstance(est, int) and est > 0:
                            job.total_hint = max(cumulative_list_ids, est)
                        else:
                            job.total_hint = cumulative_list_ids
                    page_token = next_page_token
                    job.phase = "fetching"
                    n_batch = len(ids)
                    hint = job.total_hint
                    if hint is not None and hint > 0:
                        approx = "~" if next_page_token else ""
                        job.message = (
                            f"Fetching metadata for {n_batch} messages in this batch "
                            f"({listed} / {approx}{hint} indexed)…"
                        )
                    else:
                        job.message = f"Fetching metadata for {n_batch} messages ({listed} indexed so far)…"
                    job.updated_at = time.time()
                    if job.cancel_event.is_set():
                        self._finish(job, JobStatus.cancelled, f"Stopped after {listed} messages.")
                        return

                    rl_lock = threading.Lock()

                    for j in range(0, len(ids), chunk_sz):
                        if job.cancel_event.is_set():
                            self._finish(job, JobStatus.cancelled, f"Stopped after {listed} messages.")
                            return
                        part = ids[j : j + chunk_sz]
                        rl_chunk = [0]

                        def on_meta_rl() -> None:
                            with rl_lock:
                                rl_chunk[0] += 1

                        def fetch_one(mid: str) -> dict[str, Any] | None:
                            try:
                                svc = gs.service_for_thread(creds)
                                full = gs.get_message_metadata(
                                    svc,
                                    mid,
                                    full_format=False,
                                    on_rate_limit=on_meta_rl if adaptive else None,
                                )
                                record_api_call()
                                return gs.message_to_row(full)
                            except gs.HttpError:
                                return None

                        workers_cap = pacer.max_workers if pacer else max_w
                        workers = min(workers_cap, max(1, len(part)))
                        with ThreadPoolExecutor(max_workers=workers) as pool:
                            chunk_rows = list(pool.map(fetch_one, part))
                        if adaptive and pacer:
                            if rl_chunk[0] > 0:
                                pacer.backoff(rl_chunk[0])
                                time.sleep(
                                    min(
                                        30.0,
                                        float(app_settings.gmail_retry_initial_delay_seconds)
                                        * min(rl_chunk[0], 4),
                                    )
                                )
                            else:
                                pacer.ramp()
                            job.adaptive_pause_seconds = pacer.pause_chunk
                            job.adaptive_workers = pacer.max_workers
                            job.updated_at = time.time()
                        hint = job.total_hint
                        for row in chunk_rows:
                            if job.cancel_event.is_set():
                                self._finish(job, JobStatus.cancelled, f"Stopped after {listed} messages.")
                                return
                            if not row:
                                continue
                            labels = row.get("label_ids") or []
                            if "TRASH" in labels:
                                db.delete_messages_by_ids(conn, [row["id"]])
                                continue
                            db.upsert_message(conn, **row)
                            listed += 1
                            job.processed = listed
                            if hint is not None and hint > 0:
                                job.message = f"Indexed {listed} / {hint} messages…"
                            else:
                                job.message = f"Indexed {listed} messages…"
                            job.updated_at = time.time()
                        dyn_pause = pacer.pause_chunk if pacer else pause_chunk
                        if dyn_pause > 0 and j + chunk_sz < len(ids):
                            time.sleep(dyn_pause)
                    conn.commit()
                    if not page_token:
                        break
                db.set_kv(conn, "last_sync_q", q)
                conn.commit()
                self._finish(job, JobStatus.completed, f"Completed: {listed} messages indexed.")
            finally:
                conn.close()

        try:
            await loop.run_in_executor(self._executor, work)
        except Exception as e:  # noqa: BLE001
            self._finish(job, JobStatus.failed, err=str(e))

    async def enqueue_trash(
        self,
        creds_dict: dict[str, Any],
        message_ids: list[str],
    ) -> dict[str, Any]:
        """Add message ids to the trash queue; one worker job processes until empty."""
        added = 0
        with self._trash_queue_lock:
            for mid in message_ids:
                if mid in self._trash_pending_set:
                    continue
                self._trash_pending.append(mid)
                self._trash_pending_set.add(mid)
                added += 1

            pending_n = len(self._trash_pending)

            if added == 0:
                if self._trash_queue_job_id:
                    jid = self._trash_queue_job_id
                    job = self._jobs.get(jid)
                    if job and job.status == JobStatus.running:
                        job.total_hint = job.processed + pending_n
                        job.queue_remaining = pending_n
                        job.message = (
                            f"Trash queue: {job.processed} deleted; "
                            f"{pending_n} waiting…"
                        )
                        job.updated_at = time.time()
                        return {"job_id": jid, "queued": 0}
                raise ValueError("No new message ids to queue (duplicates only)")

            if self._trash_queue_job_id:
                jid = self._trash_queue_job_id
                job = self._jobs.get(jid)
                if job and job.status == JobStatus.running:
                    job.total_hint = job.processed + pending_n
                    job.queue_remaining = pending_n
                    job.message = (
                        f"Trash queue: {job.processed} deleted; "
                        f"{pending_n} waiting…"
                    )
                    job.updated_at = time.time()
                    self._trash_queue_event.set()
                    return {"job_id": jid, "queued": added}

            jid = str(uuid.uuid4())
            job = Job(
                id=jid,
                kind="trash_queue",
                phase="trashing",
                message="Trash queue starting…",
                deleted_recent=[],
                queue_remaining=pending_n,
            )
            job.processed = 0
            job.total_hint = pending_n
            self._jobs[jid] = job
            self._trash_queue_job_id = jid
            self._trash_queue_event.set()
            asyncio.create_task(self._run_trash_queue(job, creds_dict))
            return {"job_id": jid, "queued": added}

    async def _run_trash_queue(self, job: Job, creds_dict: dict[str, Any]) -> None:
        loop = asyncio.get_event_loop()
        batch_size = 50
        max_recent = 50

        def _clear_trash_job_state() -> None:
            with self._trash_queue_lock:
                if self._trash_queue_job_id == job.id:
                    self._trash_queue_job_id = None
                self._trash_pending.clear()
                self._trash_pending_set.clear()

        def work() -> None:
            creds = gs.credentials_from_token_data(creds_dict)
            conn = db.get_connection()
            progress_lock = threading.Lock()
            try:
                while True:
                    if job.cancel_event.is_set():
                        _clear_trash_job_state()
                        job.queue_remaining = 0
                        self._finish(
                            job,
                            JobStatus.cancelled,
                            f"Cancelled after {job.processed} deleted.",
                        )
                        return

                    batch: list[str] = []
                    with self._trash_queue_lock:
                        while len(batch) < batch_size and self._trash_pending:
                            mid = self._trash_pending.popleft()
                            self._trash_pending_set.discard(mid)
                            batch.append(mid)
                        job.queue_remaining = len(self._trash_pending)

                    if not batch:
                        if job.cancel_event.is_set():
                            _clear_trash_job_state()
                            job.queue_remaining = 0
                            self._finish(
                                job,
                                JobStatus.cancelled,
                                f"Cancelled after {job.processed} deleted.",
                            )
                            return
                        with self._trash_queue_lock:
                            still = len(self._trash_pending)
                        if still > 0:
                            continue
                        if job.processed > 0:
                            break
                        self._trash_queue_event.wait(timeout=0.4)
                        self._trash_queue_event.clear()
                        with self._trash_queue_lock:
                            if not self._trash_pending and job.processed == 0:
                                break
                        continue

                    with self._trash_queue_lock:
                        qrem_mid = len(self._trash_pending)
                    job.total_hint = job.processed + len(batch) + qrem_mid

                    subjects: list[tuple[str, str]] = []
                    for mid in batch:
                        row = conn.execute(
                            "SELECT subject FROM messages WHERE id = ?",
                            (mid,),
                        ).fetchone()
                        subj = (row[0] if row else "") or "(no subject)"
                        subjects.append((mid, subj))

                    def trash_one(mid: str) -> None:
                        svc = gs.service_for_thread(creds)
                        gs.trash_message(svc, mid)
                        with progress_lock:
                            job.processed += 1
                            job.message = f"Deleting in Gmail… {job.processed} done"
                            job.updated_at = time.time()

                    try:
                        tw = min(8, max(1, len(batch)))
                        with ThreadPoolExecutor(max_workers=tw) as pool:
                            list(pool.map(trash_one, batch))
                    except gs.HttpError as e:
                        _clear_trash_job_state()
                        self._finish(job, JobStatus.failed, err=str(e))
                        return

                    db.delete_messages_by_ids(conn, batch)
                    conn.commit()

                    with progress_lock:
                        for mid, subj in subjects:
                            job.deleted_recent.append({"id": mid, "subject": subj})
                        while len(job.deleted_recent) > max_recent:
                            job.deleted_recent.pop(0)
                        with self._trash_queue_lock:
                            qrem = len(self._trash_pending)
                        job.queue_remaining = qrem
                        job.total_hint = job.processed + qrem
                        job.message = (
                            f"Deleted {job.processed} in Gmail"
                            + (f"; {qrem} still queued" if qrem else "")
                        )
                        job.updated_at = time.time()
                        if qrem == 0:
                            break
            finally:
                conn.close()

            job.queue_remaining = 0
            with self._trash_queue_lock:
                if self._trash_queue_job_id == job.id:
                    self._trash_queue_job_id = None
            self._finish(
                job,
                JobStatus.completed,
                f"Done: {job.processed} message(s) moved to Trash.",
            )

        try:
            await loop.run_in_executor(self._executor, work)
        except Exception as e:  # noqa: BLE001
            _clear_trash_job_state()
            self._finish(job, JobStatus.failed, err=str(e))

    async def start_bulk(
        self,
        creds_dict: dict[str, Any],
        message_ids: list[str],
        action: str,
    ) -> str:
        jid = str(uuid.uuid4())
        job = Job(id=jid, kind=f"bulk_{action}", phase="modifying", message="Starting…")
        job.total_hint = len(message_ids)
        self._jobs[jid] = job
        asyncio.create_task(self._run_bulk(job, creds_dict, message_ids, action))
        return jid

    async def _run_bulk(
        self,
        job: Job,
        creds_dict: dict[str, Any],
        message_ids: list[str],
        action: str,
    ) -> None:
        loop = asyncio.get_event_loop()
        batch_size = 50

        def work() -> None:
            creds = gs.credentials_from_token_data(creds_dict)
            service = gs.build_service(creds)
            total = len(message_ids)
            conn = db.get_connection()
            progress_lock = threading.Lock()
            try:
                if action == "trash":
                    job.phase = "trashing"
                    job.processed = 0
                    job.message = f"Trashing 0/{total} in Gmail…"
                    job.updated_at = time.time()
                    for i in range(0, len(message_ids), batch_size):
                        if job.cancel_event.is_set():
                            self._finish(
                                job,
                                JobStatus.cancelled,
                                f"Stopped after {job.processed} of {total} messages.",
                            )
                            return
                        batch = message_ids[i : i + batch_size]

                        def trash_one(mid: str) -> None:
                            svc = gs.service_for_thread(creds)
                            gs.trash_message(svc, mid)
                            with progress_lock:
                                job.processed += 1
                                job.message = (
                                    f"Trashed {job.processed}/{total} in Gmail…"
                                )
                                job.updated_at = time.time()

                        try:
                            tw = min(8, max(1, len(batch)))
                            with ThreadPoolExecutor(max_workers=tw) as pool:
                                list(pool.map(trash_one, batch))
                            db.delete_messages_by_ids(conn, batch)
                            conn.commit()
                            job.message = (
                                f"Trashed {job.processed}/{total} in Gmail; "
                                f"removed {len(batch)} from local cache (batch)."
                            )
                            job.updated_at = time.time()
                        except gs.HttpError as e:
                            self._finish(job, JobStatus.failed, err=str(e))
                            return
                    self._finish(
                        job,
                        JobStatus.completed,
                        f"Done: {total} message(s) in Gmail Trash and removed from local cache.",
                    )
                    return

                job.phase = "modifying"
                done = 0
                verb = {
                    "archive": "Archiving",
                    "read": "Marking read",
                    "unread": "Marking unread",
                }.get(action, "Processing")
                for i in range(0, len(message_ids), batch_size):
                    if job.cancel_event.is_set():
                        self._finish(
                            job,
                            JobStatus.cancelled,
                            f"Stopped after {done} of {total} messages.",
                        )
                        return
                    batch = message_ids[i : i + batch_size]
                    try:
                        if action == "archive":
                            gs.batch_modify(service, batch, remove_labels=["INBOX"])
                            db.remove_label_from_messages(conn, batch, "INBOX")
                        elif action == "unread":
                            gs.batch_modify(service, batch, add_labels=["UNREAD"])
                            db.add_label_to_messages(conn, batch, "UNREAD")
                        elif action == "read":
                            gs.batch_modify(service, batch, remove_labels=["UNREAD"])
                            db.remove_label_from_messages(conn, batch, "UNREAD")
                        else:
                            self._finish(job, JobStatus.failed, err=f"Unknown action {action}")
                            return
                        conn.commit()
                    except gs.HttpError as e:
                        self._finish(job, JobStatus.failed, err=str(e))
                        return
                    done += len(batch)
                    job.processed = done
                    job.message = f"{verb} {done}/{total} in Gmail; local cache updated…"
                    job.updated_at = time.time()
                self._finish(
                    job,
                    JobStatus.completed,
                    f"Done: {done} messages in Gmail; local cache labels updated.",
                )
            finally:
                conn.close()

        try:
            await loop.run_in_executor(self._executor, work)
        except Exception as e:  # noqa: BLE001
            self._finish(job, JobStatus.failed, err=str(e))


job_manager = JobManager()
