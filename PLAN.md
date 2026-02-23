# Implementation Plan: Async Jobs, TV Auto-Discovery, Web UI

## Feature 1: Async Job Queue

**Goal**: Long-running pipeline calls return immediately with a job ID. Clients poll `GET /jobs/{id}` for status.

### New files
- `frameart/jobs.py` — in-memory job store + background thread executor

### Changes
- `frameart/api.py` — add async variants of endpoints, add `GET /jobs/{id}` detail endpoint
- `frameart/config.py` — no changes needed (jobs are in-memory, ephemeral)

### Design
- `JobStore` class with a `dict[str, Job]` keyed by job_id
- `Job` dataclass: `id`, `status` (pending/running/completed/failed), `request` (the original params), `result` (PipelineResult | None), `error`, `created_at`, `started_at`, `completed_at`
- A `ThreadPoolExecutor(max_workers=2)` runs pipeline functions in background threads (not asyncio — the pipeline is synchronous and I/O-bound on external APIs)
- `submit_job(func, *args)` → creates a Job, submits to executor, returns job_id
- Existing sync endpoints stay as-is for backwards compatibility
- New async endpoints: `POST /async/generate`, `POST /async/generate-and-apply`, `POST /async/apply` — return `{"job_id": "...", "status": "pending"}` immediately
- `GET /jobs/{job_id}` returns full job status + result when complete
- Jobs are kept in memory; they don't survive server restarts (fine for v1)

### Tests
- `tests/test_jobs.py` — unit tests for JobStore
- `tests/test_api.py` — add tests for async endpoints

---

## Feature 2: TV Auto-Discovery (UPnP/SSDP)

**Goal**: `frameart tv discover` finds Samsung Frame TVs on the LAN without knowing the IP.

### New files
- `frameart/tv/discovery.py` — SSDP M-SEARCH + REST device info filtering

### Changes
- `frameart/cli.py` — add `tv discover` subcommand
- `frameart/api.py` — add `GET /tv/discover` endpoint

### Design
- Send SSDP M-SEARCH to `239.255.255.250:1900` for `urn:samsung.com:device:RemoteControlReceiver:1`
- Parse responses for LOCATION header → extract IP
- For each IP, call `http://<IP>:8001/api/v2/` to get device info
- Filter for `FrameTVSupport: "true"`
- Return list of `{ip, name, model, frame_tv_support}` dicts
- CLI prints a formatted table; API returns JSON array
- Timeout of 3 seconds for SSDP (TVs respond within 1-2s typically)
- No new dependencies needed (just stdlib `socket` + existing `httpx`)

### Tests
- `tests/test_discovery.py` — unit tests with mocked socket

---

## Feature 3: Web UI

**Goal**: A single-page web interface for generating art and browsing the gallery, served from the same FastAPI server.

### New files
- `frameart/static/index.html` — single HTML file with embedded CSS + JS (no build step)

### Changes
- `frameart/api.py` — mount static files, add a `GET /` redirect or serve index.html

### Design
- Single self-contained HTML file (~300 lines) with:
  - **Generate tab**: prompt input, style dropdown, TV selector, "Generate" button, live status
  - **Gallery tab**: grid of recent jobs with thumbnails (from `/jobs/{id}/image`), click to view full-size
  - **TV tab**: discover TVs, show status, pair button hint
- Uses vanilla JS (fetch API) — no React/Vue/npm build step
- Polls `GET /jobs/{id}` every 2s when an async job is running
- Responsive layout (works on phone for Siri Shortcut follow-up)
- FastAPI serves it via `StaticFiles` mount at `/static` and a root `GET /` that returns the HTML
- Dark theme by default (art looks better on dark backgrounds)

### Tests
- `tests/test_api.py` — verify `GET /` returns 200 with HTML content-type
