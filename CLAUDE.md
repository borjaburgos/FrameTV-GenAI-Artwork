# CLAUDE.md

Developer guide for AI assistants working on the FrameArt codebase.

## What This Project Does

FrameArt generates AI artwork from text prompts and displays it on Samsung Frame TVs.
The pipeline is: **prompt → image generation → post-processing (16:9 crop + 4K upscale) → upload to TV → switch display**.
It ships as a CLI (`frameart`), an HTTP API (FastAPI), and a single-page web UI.

## Quick Reference

```bash
# Install for development
pip install -e ".[dev,api]"

# Run all 84 tests
pytest

# Lint (must pass before push)
ruff check frameart/ tests/

# Auto-fix lint issues
ruff check --fix frameart/ tests/

# Start the API server locally
frameart serve --host 127.0.0.1 --port 8000
```

## Repository Layout

```
frameart/                       # Main package
├── __init__.py                 # __version__ = "0.1.0"
├── cli.py                      # Click CLI — top-level group + subcommands
├── api.py                      # FastAPI HTTP server — sync + async endpoints
├── pipeline.py                 # Core orchestration: generate → postprocess → upload → switch
├── config.py                   # Pydantic-settings config: YAML + env vars + CLI overrides
├── postprocess.py              # 16:9 crop + 3840x2160 resize (PIL)
├── artifacts.py                # File storage: source.png, final.png, meta.json per job
├── jobs.py                     # In-memory async job queue (ThreadPoolExecutor)
├── static/index.html           # Web UI (single-page, no build step)
├── providers/                  # Pluggable image generation adapters
│   ├── base.py                 # ImageProvider ABC + GeneratedImage dataclass
│   ├── registry.py             # Lazy-loaded name → class mapping
│   ├── openai_adapter.py       # DALL-E 3 / gpt-image-1 via httpx
│   └── ollama_adapter.py       # Local Ollama integration
├── tv/                         # Samsung Frame TV control
│   ├── controller.py           # WebSocket control via samsungtvws — pair, upload, switch
│   └── discovery.py            # UPnP/SSDP auto-discovery
└── upscalers/                  # Pluggable image upscaling adapters
    ├── base.py                 # Upscaler ABC
    ├── registry.py             # Lazy-loaded name → class mapping
    ├── none_upscaler.py        # Built-in Pillow LANCZOS (no external service)
    ├── local_http.py           # LAN upscaler (e.g., Real-ESRGAN)
    └── remote_http.py          # Remote upscaler service

tests/                          # pytest suite — 84 tests, all mocked, no network I/O
├── test_api.py                 # FastAPI endpoints (TestClient + mocks)
├── test_pipeline.py            # Prompt normalization
├── test_postprocess.py         # Aspect ratio + resolution enforcement
├── test_config.py              # Settings, YAML loading, env vars
├── test_artifacts.py           # File I/O and metadata
├── test_providers.py           # Provider registry
├── test_upscalers.py           # Upscaler registry
├── test_jobs.py                # Job queue, eviction, thread safety
└── test_discovery.py           # SSDP discovery

docs/                           # Deployment guides
├── LXC.md                      # Proxmox LXC setup
└── VM.md                       # Proxmox VM setup
```

## Architecture

### Data Flow

```
CLI/API request
  → pipeline.run_generate()
      1. normalize_prompt()           # append style preset + "16:9" aspect hint
      2. provider.generate()          # call OpenAI/Ollama via registry
      3. postprocess()                # smart crop to 16:9, resize to 3840x2160
      4. save source.png, final.png, meta.json
  → pipeline.run_apply() (optional)
      5. tv_ctrl.upload_image()       # WebSocket binary upload to TV
      6. tv_ctrl.switch_art()         # switch displayed artwork
  → PipelineResult                    # dataclass with job_id, paths, timings, error
```

### Key Patterns

**Pluggable registries (Strategy pattern)** — Providers (`providers/registry.py`) and upscalers (`upscalers/registry.py`) each maintain a lazy-loaded `_REGISTRY` dict mapping names to classes. `get_provider(name, config)` / `get_upscaler(name, config)` instantiate on demand. To add a new adapter: implement the ABC, add its import to `_populate_registry()`, register by name.

**Pydantic configuration** — Three tiers in priority order: CLI args > `FRAMEART_*` env vars > `config.yaml`. `Settings` inherits from `pydantic_settings.BaseSettings`. All loading goes through `load_settings(**overrides)` in `config.py`.

**Dataclass results** — Pipeline functions return `PipelineResult` (dataclass) with an `.error` field. They catch exceptions internally and set `result.error = str(e)` instead of re-raising. Callers check `.error` to decide exit code (CLI) or HTTP status (API).

**Async job queue** — `jobs.py` has a `JobStore` backed by `ThreadPoolExecutor(max_workers=2)`. Jobs follow lifecycle: pending → running → completed/failed. Module-level `job_store` singleton is used by the API. Finished jobs auto-evict at 200 (FIFO).

**Artifact organization** — Stored by date: `<data_dir>/artifacts/YYYY/MM/DD/<job_id>/`. Each job directory contains `source.png`, `final.png`, `meta.json`. Job IDs are `HHMMSS-<uuid4_hex8>`.

**TV upload** — `tv/controller.py` auto-detects the Samsung art API version. API 0.97 (2018/2019 Frame TVs) uses a single WebSocket binary frame. Newer APIs (2020+) use a D2D TCP socket handshake. All uploads convert to JPEG first (PNG at 4K can be 15-25 MB, which the TV rejects). Retries with exponential backoff (2s, 4s, 8s).

## Configuration

Config file search order:
1. `$FRAMEART_CONFIG` (env var)
2. `./config.yaml`
3. `~/.config/frameart/config.yaml`
4. `/etc/frameart/config.yaml`

See `config.example.yaml` for the full template. Key models in `config.py`:

- `Settings` — top-level (`data_dir`, `default_provider`, `default_upscaler`, `tvs`, `providers`, `upscalers`, `auto_aspect_hint`)
- `TVProfile` — per-TV config (`ip`, `port=8002`, `name="FrameArt"`, `token_file`, `ssl=True`)
- `ProviderConfig` — per-provider config (`api_key`, `base_url`, `model`, `timeout=120`)
- `UpscalerConfig` — per-upscaler config (`base_url`, `api_key`, `timeout=120`)

Environment variables use `FRAMEART_` prefix with `__` for nesting (e.g., `FRAMEART_DEFAULT_PROVIDER=ollama`).

## CLI Commands

```
frameart [--version] [--verbose|-v] [--debug] [--data-dir PATH]
  generate              # Text prompt → image (no TV upload)
  apply                 # Upload existing image to TV
  generate-and-apply    # Full pipeline: prompt → image → TV
  tv
    status              # Check TV reachability, art mode, current artwork
    pair                # Pair with TV (saves token for future use)
    discover            # SSDP auto-discovery of Samsung TVs on LAN
    list-art            # List artworks stored on TV
  list                  # Show recent generated artifacts
  cleanup               # Delete old artifacts by age
  serve                 # Start HTTP API server (requires [api] extra)
```

`--debug` and `--verbose` flags can appear on either the top-level group or any subcommand — the `_ensure_logging()` helper handles idempotent setup. CLI subcommands use **deferred imports** (inside the function body) to keep startup fast.

## HTTP API Endpoints

The API requires the `api` extra (`pip install ".[api]"`).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check (`{"status":"ok","version":"..."}`) |
| GET | `/styles` | Available style presets (dict) |
| POST | `/generate` | Sync image generation |
| POST | `/generate-and-apply` | Sync generate + upload + switch on TV |
| POST | `/apply` | Sync upload existing image to TV |
| GET | `/tv/status` | Check TV connection, art mode status |
| GET | `/tv/discover` | SSDP discovery of Samsung TVs |
| GET | `/jobs` | List recent jobs (from artifact metadata) |
| GET | `/jobs/{id}/image` | Serve final processed image (PNG) |
| POST | `/async/generate` | Async generation → returns `{job_id, status}` |
| POST | `/async/generate-and-apply` | Async full pipeline |
| POST | `/async/apply` | Async upload |
| GET | `/jobs/{id}/status` | Poll async job status and result |
| GET | `/` | Web UI (single-page HTML) |

## Testing

### Running Tests

```bash
pytest                           # All tests (84 on main)
pytest tests/test_pipeline.py    # Single file
pytest -k "test_health"          # Filter by name
pytest --cov                     # Coverage report
```

### Test Conventions

- **Test classes** group related tests (e.g., `TestNormalizePrompt`, `TestHealth`, `TestAsyncGenerate`)
- **`unittest.mock.patch`** for all external I/O — no network calls in tests
- **`FakePipelineResult`** dataclass in `test_api.py` mirrors `PipelineResult` for mocking
- **`TestClient`** from FastAPI for HTTP endpoint tests (no running server)
- **No shared conftest.py** — each test file defines its own helpers (`_fake_result()`, etc.)
- **`tmp_path`** (pytest built-in fixture) for file system tests
- **Polling pattern** for async job tests: submit, then loop with `time.sleep(0.05)` up to 50 iterations checking status

### Source → Test Mapping

| Source Module | Test File |
|---------------|-----------|
| `pipeline.py` | `test_pipeline.py` |
| `postprocess.py` | `test_postprocess.py` |
| `config.py` | `test_config.py` |
| `artifacts.py` | `test_artifacts.py` |
| `providers/registry.py` | `test_providers.py` |
| `upscalers/registry.py` | `test_upscalers.py` |
| `jobs.py` | `test_jobs.py` |
| `tv/discovery.py` | `test_discovery.py` |
| `api.py` | `test_api.py` |

## Code Conventions

### Imports
- **Every module** starts with `from __future__ import annotations`
- Import order: stdlib → third-party → local (enforced by ruff `I` rule)
- CLI subcommands use **deferred imports** inside the function body to avoid loading heavy deps at startup

### Type Annotations
- Full PEP 484 type hints on all function signatures
- Union types use `|` syntax (Python 3.10+): `str | None`, `Path | None`
- Built-in generics (PEP 585): `dict[str, Any]`, `list[str]`, `tuple[int, int]`

### Naming
- `snake_case` for functions/variables
- `PascalCase` for classes
- `UPPER_CASE` for module constants (`TARGET_WIDTH`, `MAX_RETRIES`, `MAX_COMPLETED_JOBS`)
- `_` prefix for private functions (`_compute_crop_box`, `_get_provider_instance`, `_retry`)

### Docstrings
- Module-level docstrings at top of every `.py` file
- Google/NumPy-style sections: `Parameters`, `Returns`, `Raises`
- Omitted on trivially obvious functions

### Error Handling
- Pipeline functions **catch exceptions** and set `result.error = str(e)` — they never raise to the caller
- CLI checks `result.error` → exits with code 1
- API checks `result.error` → raises `HTTPException(500)` with structured detail
- Logging via `logger = logging.getLogger(__name__)` per module

### Section Comments
- Modules use `# --- Section Name ---` comments with dashed lines to separate logical sections (e.g., routes, helpers, CLI subgroups)

## Linting

Configured in `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py310"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "SIM"]
```

Rule groups: pycodestyle errors/warnings (E/W), pyflakes (F), isort import ordering (I), pep8-naming (N), pyupgrade (UP), flake8-bugbear (B), flake8-simplify (SIM).

Run: `ruff check frameart/ tests/` — must pass clean before merge.

## CI Pipeline

`.github/workflows/ci.yml` triggers on push to `main` and PRs targeting `main`.

**test** job (matrix: Python 3.10, 3.11, 3.12):
1. `pip install ".[dev,api]"`
2. `ruff check frameart/ tests/`
3. `pytest --tb=short -v`

**docker** job (depends on test):
1. `docker build -t frameart:test .`
2. `docker run --rm frameart:test --version`

## Docker

- `Dockerfile` — multi-stage from `python:3.12-slim`, installs system deps for Pillow, creates non-root `frameart` user, entrypoint is `frameart`
- `docker-compose.yml` — two services:
  - `frameart` — one-shot CLI commands (`docker compose run --rm frameart <cmd>`)
  - `frameart-api` — long-running server on port 8000 with healthcheck

## Dependencies

**Core** (always installed):
- `click` — CLI framework
- `Pillow` — image processing
- `httpx` — HTTP client
- `websocket-client` + `samsungtvws` — Samsung TV WebSocket API
- `pyyaml` — YAML config parsing
- `pydantic` + `pydantic-settings` — data validation and settings

**Optional extras:**
- `[dev]` — `pytest`, `pytest-cov`, `ruff`
- `[api]` — `fastapi`, `uvicorn`

## Style Presets

Defined in `config.py` as `STYLE_PRESETS` dict. Available: `abstract`, `kid_drawing`, `watercolor`, `bw_photo`, `oil_painting`, `pixel_art`, `impressionist`, `minimalist`. Each maps to a prompt suffix appended during `normalize_prompt()`.

## Common Development Tasks

### Adding a New Image Provider
1. Create `frameart/providers/my_adapter.py`
2. Subclass `ImageProvider` from `providers/base.py` — implement `generate()` and `name` property
3. Register in `providers/registry.py` → add import and name mapping in `_populate_registry()`
4. Add config section to `config.example.yaml` under `providers:`
5. Add tests in `tests/test_providers.py`

### Adding a New Upscaler
1. Create `frameart/upscalers/my_upscaler.py`
2. Subclass `Upscaler` from `upscalers/base.py` — implement `upscale()` and `name` property
3. Register in `upscalers/registry.py` → `_populate_registry()`
4. Add tests in `tests/test_upscalers.py`

### Adding a CLI Command
1. Define in `cli.py` with `@main.command()` or under a group (e.g., `@tv.command()`)
2. Apply `@_debug_option` and `@_verbose_option` decorators
3. Call `_ensure_logging(ctx)` as the first line
4. Use deferred imports for heavy deps
5. Use `_print_result()` for pipeline results, `click.secho()` for direct output

### Adding an API Endpoint
1. Define Pydantic request/response models in `api.py`
2. Add route with `@app.get()` or `@app.post()`
3. For async: use `job_store.submit()` and return `AsyncJobResponse`
4. Add tests in `test_api.py` using `TestClient` + `unittest.mock.patch`

### Adding a Style Preset
1. Add entry to `STYLE_PRESETS` dict in `config.py`
2. Test coverage is automatic via `test_config.py::TestStylePresets`

## Debugging

```bash
# Full debug logging (includes samsungtvws WebSocket wire protocol)
frameart --debug generate-and-apply --prompt "..." --tv-ip 1.2.3.4

# Verbose logging (INFO level)
frameart -v generate --prompt "..."

# Debug flags work on subcommands too
frameart generate-and-apply --debug --prompt "..."
```

Logs are written to `<data_dir>/logs/frameart.log`. In debug mode, `samsungtvws` and `websocket` library loggers are also activated.

## Sensitive Files

The `.gitignore` excludes: `*.token`, `.env`, `secrets/`, `config.yaml`. API keys should be set via environment variables (e.g., `OPENAI_API_KEY`) rather than config files. Token files are auto-created during TV pairing at `<data_dir>/secrets/<ip>.token`.
