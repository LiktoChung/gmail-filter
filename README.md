# Gmail Filter (local, Docker)

Self-hosted web app to search Gmail with the **same query operators as the Gmail web UI**, **sync message metadata** into a local SQLite cache, view **bubble charts** by sender/domain/age, **preview** messages, and **archive / trash / mark read** in bulk—with **progress** and **force cancel** for long jobs.

**Official Gmail search operators:** [Google Help: Search operators](https://support.google.com/mail/answer/7190)

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows, macOS, or Linux)
- A Google account with Gmail
- A browser

## 1. Google Cloud: enable Gmail API and OAuth client

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a project (or pick an existing one).
2. **APIs & Services → Library** → search **Gmail API** → **Enable**.
3. **APIs & Services → OAuth consent screen**
   - User type: **External** (for a personal Google account).
   - App name, support email, developer contact.
   - Scopes: add `https://www.googleapis.com/auth/gmail.modify` (or add “Gmail API” …/auth/gmail.modify from the list).  
     Google’s consent screen may only summarize this as “read, compose, and send,” but **`gmail.modify` is what Gmail uses for [messages.trash](https://developers.google.com/gmail/api/reference/rest/v1/users.messages/trash), archive (label changes), and read/unread**—not just sending mail. If you change scopes later, **sign out and sign in again** so Google issues a new token.
   - Test users: add **your Google email** while the app is in **Testing** (required for unverified apps).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Application type: **Web application**.
   - **Authorized redirect URIs** (must match exactly):
     - `http://localhost:8000/api/auth/callback`
   - Create and copy **Client ID** and **Client Secret**.

## 2. Configure environment

In the project folder:

```bash
copy .env.example .env
```

Edit `.env`:

```env
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-secret
REDIRECT_URI=http://localhost:8000/api/auth/callback
```

## 3. Run with Docker Compose

```bash
docker compose up --build
```

Open **http://localhost:8000**

- Click **Sign in with Google** and approve access.
- Use **Advanced search** (same fields as Gmail’s “Show search options”) or **Raw Gmail query** (paste from Gmail’s search box).
- **Search Gmail** loads live results from the Gmail API (accurate `q`). By default, messages with the **TRASH** label are omitted so mail you moved to Trash does not reappear in results (Gmail’s own `in:anywhere` queries include Trash). Optional API flag: `exclude_trash=false` on `GET /api/messages` to include trashed messages.
- **Sync cache** indexes metadata into `/data` for **aggregates** (bubble chart or **list** view: group → count). Messages in **Gmail Trash** are **not** stored in the cache (and any existing row is removed on sync), so trashed mail does not come back after a resync. **Clicking** a bubble or row loads that bucket from the **cache**; **Search Gmail** still uses the live Gmail API.
- **Load all mail (full sync)** runs a cache sync with Gmail query `in:anywhere` so **all messages** in the account are indexed. The **Job** panel shows progress: the total is **exact** once Gmail’s list has no more pages (single-page syncs use the real message count, not Gmail’s often-wrong `resultSizeEstimate`). **Force cancel** if needed.
- Select messages → **Archive** / **Trash** / **Mark read/unread** (runs as a job with progress; **Force cancel** stops between batches).

### Data storage

Docker volume `gmail_data` is mounted at **`/data`** inside the container:

- `tokens.json` — OAuth tokens (keep private).
- `gmail_cache.sqlite3` — cached metadata for charts.

### Throughput (large inboxes)

The Gmail API returns up to **500** message IDs per `messages.list` call. The UI requests **100** messages per search page by default (`GMAIL_LIST_LIMIT` in the frontend) so the first page returns quickly; use **Load more** for the next page.

**Sync jobs** list up to **`GMAIL_LIST_PAGE_SIZE` per request** (default **500**, max 500) and fetch metadata in **chunks** (`GMAIL_ENRICH_CHUNK_SIZE`, default **8**) with **parallel workers** capped per chunk (default **`GMAIL_PARALLEL_WORKERS=4`**). Short **pauses** between list pages and between chunks (`GMAIL_LIST_PAGE_PAUSE_SECONDS`, `GMAIL_SYNC_CHUNK_PAUSE_SECONDS`) keep you under Gmail’s **queries per minute per user** limit. Each HTTP call uses **`GMAIL_HTTP_TIMEOUT_SECONDS`** (default **300**). On **403/429 quota** responses, the client **retries with exponential backoff** (`GMAIL_RETRY_*`).

Optional `.env` entries:

| Variable | Default | Purpose |
|----------|---------|---------|
| `GMAIL_LIST_PAGE_SIZE` | `500` | IDs fetched per `messages.list` during sync (max 500). |
| `GMAIL_PARALLEL_WORKERS` | `4` | Max concurrent `messages.get` calls within a chunk. |
| `GMAIL_ENRICH_CHUNK_SIZE` | `8` | Messages processed per chunk before a pause (sync + search enrich). |
| `GMAIL_SYNC_CHUNK_PAUSE_SECONDS` | `0.75` | Seconds to sleep between metadata chunks (not after the last chunk of a page). |
| `GMAIL_LIST_PAGE_PAUSE_SECONDS` | `0.35` | Seconds to sleep after each `messages.list` response during sync. |
| `GMAIL_RETRY_INITIAL_DELAY_SECONDS` | `2` | First backoff delay when Google returns quota / rate limit errors. |
| `GMAIL_RETRY_MAX_DELAY_SECONDS` | `120` | Max delay between retries. |
| `GMAIL_RETRY_MAX_ATTEMPTS` | `12` | Max attempts per API call (including retries). |
| `GMAIL_HTTP_TIMEOUT_SECONDS` | `300` | `httplib2` read timeout per Google API request. |

If you still see **quota exceeded** (`403` / `429`), lower **`GMAIL_PARALLEL_WORKERS`**, raise **`GMAIL_SYNC_CHUNK_PAUSE_SECONDS`** or **`GMAIL_LIST_PAGE_PAUSE_SECONDS`**, or reduce **`GMAIL_LIST_PAGE_SIZE`** (more list calls, smaller bursts per page). To load more rows per search in the UI, raise **`GMAIL_LIST_LIMIT`** in `frontend/src/App.tsx` (max 500).

## 4. Local development (optional)

**Backend** (from `backend/`, with Python 3.12+):

```bash
pip install -r requirements.txt
set DATA_DIR=.\data
set GOOGLE_CLIENT_ID=...
set GOOGLE_CLIENT_SECRET=...
set REDIRECT_URI=http://localhost:8000/api/auth/callback
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**Frontend** (from `frontend/`):

```bash
npm install
npm run build
```

Copy the contents of `frontend/dist/` into `backend/static/` (preserve the `assets/` folder), then restart uvicorn.  
Or run Vite with proxy (API on 8000):

```bash
npm run dev
```

Vite proxies `/api` to `http://127.0.0.1:8000` (see `frontend/vite.config.ts`).

## 5. Remove everything from this PC when you’re done

Use this when you no longer need the tool and want **local copies of tokens/cache gone** and **Google access revoked**.

### A. Revoke the app’s access to your Google account

1. Open [Google Account](https://myaccount.google.com/) → **Security**.
2. Under **Your connections to third-party apps & services** (wording may vary), find this app or **Gmail Filter** / your OAuth client name → **Delete** / **Revoke access**.

This invalidates tokens the app may have stored; do this even if you delete Docker data first.

### B. Stop and remove the Docker stack and its data volume

In the project folder (where `docker-compose.yml` is):

```bash
docker compose down --volumes
```

- `--volumes` removes the named volume (`gmail_data`) so **`tokens.json` and `gmail_cache.sqlite3` are deleted** from Docker’s storage.

Optional — remove the built image to free disk space:

```bash
docker image prune -f
```

Or remove only this project’s image after inspecting `docker images` (name is usually `gmail-filter-gmail-filter` or similar).

### C. Delete the project folder and secrets on disk

1. Delete the **`gmail-filter`** project directory (or move it to Recycle Bin).
2. If you created **`.env`**, it is gone with the folder. If you copied secrets elsewhere, delete those copies too.
3. If you ran the app **without Docker**, delete your local data directory (e.g. `backend/data/` or whatever you set as `DATA_DIR`) where `tokens.json` and `gmail_cache.sqlite3` lived.

### D. Optional: clean up Google Cloud (OAuth client)

If you will not use this app again, you can delete the **OAuth 2.0 Client ID** or the whole **Google Cloud project** in [Cloud Console](https://console.cloud.google.com/) → **APIs & Services → Credentials**. This does not delete Gmail; it only removes the API client you created for this tool.

After these steps, the tool’s **local state and access** should be fully removed from the machine (aside from normal OS/backup copies, if any).

## API scopes

The app uses:

- `https://www.googleapis.com/auth/gmail.modify`

This allows read/search and modify (archive, trash, labels).

## Security notes

- Do not commit `.env` or `tokens.json`.
- Revoke access anytime: Google Account → **Security** → **Third-party access** (or “Google Account permissions”).
- **Force cancel** stops **after the current batch**; already processed messages are **not** rolled back (see in-app job messages).

## Troubleshooting

| Issue | What to check |
|--------|----------------|
| `redirect_uri_mismatch` | Redirect URI in Cloud Console must match `REDIRECT_URI` exactly (including `http` vs `https`, port, path). |
| “Access blocked” / consent errors | Add your account as a **Test user** on the OAuth consent screen while in Testing mode. |
| `invalid_client` | Client ID/secret typos; recreate credentials if needed. |
| API quota / rate limits | Large syncs take time; use cancel and resume later. Bulk actions are batched (50 per request). |
| Charts empty | Run **Sync cache** so SQLite has data. Charts reflect **cached** mail, not necessarily every message in Gmail until synced. |
| Full-text body search | The Gmail API applies `q` on the server for **Search**. Local cache stores metadata/snippet; very complex body-only queries always match Gmail when you use **Search Gmail** (live API). |

## License

MIT (project template; verify compliance with Google API Terms of Service for your use case).
