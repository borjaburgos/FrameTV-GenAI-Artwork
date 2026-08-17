# FrameArt

Generate AI artwork from text prompts and display it on Samsung Frame TVs.

**prompt -> 4K art -> upload -> display**

FrameArt is a self-hosted tool that accepts a text description, generates an image using AI (local or remote providers), enforces 16:9 at 3840x2160, and uploads it directly to a Samsung The Frame TV over your local network. It ships as both a **CLI** and an **HTTP API** so you can drive it from a terminal, a voice assistant (Siri, Home Assistant), or any automation platform.

## Features

- **Multiple AI providers**: OpenAI, Ollama/local models, and Google Gemini image models (pluggable registry)
- **Automatic post-processing**: Smart crop to 16:9, upscale/downscale to 4K UHD
- **Samsung Frame TV integration**: Upload art and switch display via WebSocket API
- **TV auto-discovery**: Find Frame TVs on your LAN automatically via UPnP/SSDP
- **HTTP API**: FastAPI server with sync and async endpoints — ideal for voice agents and Home Assistant
- **Persistent async job queue**: Restart-safe SQLite status, results, and bounded history
- **Web UI**: Built-in browser interface with provider/model dropdowns and concurrent async job tracking
- **Managed artwork library**: Search, tags, named collections, bulk organization, and display history
- **TV groups and playlists**: Fan out to named groups and rotate ordered library artwork
- **Durable schedules and integrations**: Restart-safe intervals, signed webhooks, optional MQTT, and Home Assistant-compatible control endpoints
- **Public domain artwork support**: Search and apply art from major open-access museum collections
- **Style presets**: abstract, oil_painting, watercolor, kid_drawing, and more
- **Pluggable upscalers**: Built-in Pillow LANCZOS, local HTTP (Real-ESRGAN), or remote services
- **Artifact management**: Date-organized storage with full metadata tracking
- **2018-2025 Frame TV support**: Automatic API version detection (0.97 binary upload for older models, D2D socket for newer ones)
- **Docker, LXC, and VM support**: Run anywhere on your home network

## Quickstart

### Install from source

```bash
git clone https://github.com/borjaburgos/FrameTV-GenAI-Artwork.git
cd FrameTV-GenAI-Artwork
pip install .            # CLI only
pip install ".[api]"     # CLI + HTTP API server
```

### Install with Docker

```bash
docker build -t frameart .
docker run --rm frameart --help
```

### Configuration

```bash
# Copy the example config
cp config.example.yaml config.yaml

# Edit with your TV IP and API keys
# Config is loaded from: ./config.yaml, ~/.config/frameart/config.yaml, or /etc/frameart/config.yaml
```

Set your API key:

```bash
export OPENAI_API_KEY="sk-..."
```

### Manage providers and TVs in the web UI

Open **Settings** to add, edit, test, and remove image providers; update API keys;
choose the default provider/model; and manage persistent TV profiles. Provider keys are
never returned to the browser after saving: the UI only shows whether a key is configured.

Web-managed settings are stored below FrameArt's `data_dir`:

- `settings/managed.yaml` contains non-secret provider, default, and TV configuration.
- `secrets/provider-keys.yaml` contains provider API keys with owner-only file permissions.
- `backups/settings/` contains up to 20 owner-only recovery snapshots created automatically
  before managed changes and on demand from the UI.
- `frameart.sqlite3` contains persistent async job state, library tags/collections, and display
  history. Running work interrupted by a restart is retained as failed so it can be diagnosed
  or resubmitted.

Environment variables remain authoritative over web-managed values. When authentication is
enabled, all `/settings/*` API operations require an admin token. TVs found through **Scan
Network** or entered through **Add by IP** can be saved as persistent profiles from the TVs
page. The **Diagnostics & Backups** card runs local readiness checks, downloads a redacted
support bundle, exports/imports portable non-secret JSON settings, and restores server-side
snapshots. Provider keys never leave the server through an export or support bundle.

## Pairing with Your Frame TV

Before uploading art, you must pair FrameArt with your TV. This is a one-time setup:

1. **Ensure the TV is on** and connected to the same network as your machine.

2. **Run the pair command**:
   ```bash
   frameart tv pair --tv-ip 192.168.1.100
   ```

3. **Accept the prompt on your TV** — a popup will appear asking you to allow the connection.

4. **Token is saved** — subsequent connections are automatic. The command prints the token file path and a config snippet you can add to `config.yaml`.

### Verify the connection

```bash
frameart tv status --tv-ip 192.168.1.100
```

This shows whether the TV is reachable, whether Art Mode is supported/active, and the currently displayed artwork ID.

---

## CLI Usage

### Generate an image (no TV upload)

```bash
frameart generate \
    --prompt "Central Park in 1950s as drawn by an 8 year-old" \
    --provider openai
```

### Generate and display on Frame TV

```bash
frameart generate-and-apply \
    --prompt "the skyline of San Francisco as depicted by an abstract painter" \
    --style abstract \
    --tv livingroom_frame
```

### Upload an existing image

```bash
frameart apply \
    --image ./my_artwork.png \
    --tv-ip 192.168.1.100 \
    --matte modern_black
```

### Discover TVs on your network

```bash
frameart tv discover
frameart tv discover --frame-only  # only show Frame TVs
```

### Check TV status

```bash
frameart tv status --tv livingroom_frame
```

### List artworks on the TV

```bash
frameart tv list-art --tv livingroom_frame
```

### Display artwork that is already on the TV

Use the existing content ID to recover from an interrupted upload-and-display
operation without uploading a duplicate:

```bash
frameart tv display --tv livingroom_frame --content-id MY_F0006
```

### List generated artifacts

```bash
frameart list --limit 10
```

### Clean up old artifacts

```bash
frameart cleanup --older-than 30
frameart cleanup --older-than 30 --dry-run  # preview only
```

### Dry run (generate but don't upload)

```bash
frameart generate-and-apply \
    --prompt "a serene mountain lake at dawn" \
    --dry-run
```

### Debug mode

Prints wire-level WebSocket messages, request payloads, and timing info:

```bash
frameart generate-and-apply --debug \
    --prompt "a sunset" --tv-ip 192.168.1.100
```

---

## HTTP API

FrameArt includes a FastAPI server for programmatic access. This is the recommended integration path for voice assistants (Siri Shortcuts, Home Assistant, Alexa), cron jobs, and other automation.

### Start the server

```bash
# Install API dependencies (if not done already)
pip install ".[api]"

# Start on localhost:8000
frameart serve

# LAN access requires authentication. A persistent admin token is generated
# under the data directory when FRAMEART_ADMIN_TOKEN is not provided.
export FRAMEART_AUTH_ENABLED=true
frameart serve --host 0.0.0.0 --port 8000
```

Interactive API docs are available at `http://localhost:8000/docs` and the web UI at `http://localhost:8000/` once the server is running.

Loopback-only serving leaves authentication off by default. Non-loopback binds are refused unless authentication is enabled. Admin tokens have `read`, `control`, and `admin` access; the optional `FRAMEART_AUTOMATION_TOKEN` has `read` and `control` access but cannot delete artwork or jobs. API clients can send either `Authorization: Bearer <token>` or `X-FrameArt-Token: <token>`. The web UI prompts for a token and stores it in an HttpOnly session cookie.

### Endpoints

**Sync** (block until complete):

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/generate` | Generate image only (no TV upload) |
| `POST` | `/generate-and-apply` | Full pipeline: prompt to TV display |
| `POST` | `/upload-and-apply` | Validate and upload multipart JPG/PNG bytes |
| `POST` | `/jobs/{job_id}/apply` | Display a previously generated artifact |

**Async** (return immediately, poll for results):

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/async/generate` | Submit generation job, returns `{job_id}` |
| `POST` | `/async/generate-and-apply` | Submit generate+apply job |
| `GET` | `/jobs/{job_id}/status` | Poll job progress and result |
| `GET` | `/async/jobs` | List recent async jobs with status and metadata |

**TV and gallery**:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/tv/status` | Check TV connection and art mode |
| `GET` | `/tv/discover` | Auto-discover Samsung TVs via SSDP |
| `GET` | `/jobs` | List recent jobs |
| `GET` | `/jobs/{job_id}/image` | Serve the final processed image |
| `PUT` | `/jobs/{job_id}/tags` | Replace persistent artwork tags |
| `GET/POST` | `/library/collections` | List or create named collections |
| `POST/DELETE` | `/library/collections/{id}/items` | Add or remove collection items |
| `GET` | `/library/history` | List recent TV display history |

**Automations**:

| Method | Path | Description |
|--------|------|-------------|
| `GET/POST` | `/automation/groups` | List or create named persistent-TV groups |
| `POST` | `/automation/groups/{id}/display` | Display one library job across a group |
| `GET/POST` | `/automation/playlists` | List or create ordered library playlists |
| `GET/POST` | `/automation/schedules` | List or create restart-safe interval schedules |
| `POST` | `/automation/schedules/{id}/run` | Run one schedule immediately |
| `PUT` | `/automation/schedules/{id}/enabled` | Pause or resume a schedule |
| `GET/POST` | `/automation/webhooks` | Manage signed outbound event hooks |
| `GET` | `/automation/status` | Scheduler, MQTT, and integration readiness |

**Public domain catalog**:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/catalog/search` | Search supported museum/open-access sources |
| `POST` | `/catalog/apply` | Download an artwork, run FrameArt processing pipeline, and upload to TV |

**Misc**:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/styles` | List available style presets |
| `GET` | `/providers` | List configured providers and model options |
| `GET` | `/health` | Liveness check |
| `GET` | `/health/ready` | Storage and settings readiness check |
| `GET` | `/settings/diagnostics` | Detailed admin-only local diagnostics |
| `GET` | `/settings/export` | Download portable non-secret settings |
| `POST` | `/settings/import` | Validate and import non-secret settings |
| `GET/POST` | `/settings/backups` | List or create server-side snapshots |

### API Examples

**Generate and display on TV** (the primary use case for voice agents):

```bash
curl -X POST http://localhost:8000/generate-and-apply \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a serene Japanese garden at sunset"}'
```

**Generate with a style preset and specific TV:**

```bash
curl -X POST http://localhost:8000/generate-and-apply \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "the Golden Gate Bridge",
    "style": "watercolor",
    "tv_ip": "192.168.1.100",
    "matte": "modern_black"
  }'
```

**Generate image only (no TV):**

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a cat wearing a tiny hat", "style": "pixel_art"}'
```

**Upload an existing image:**

```bash
curl -X POST http://localhost:8000/upload-and-apply \
  -F "image=@artwork.jpg" \
  -F "tv_ip=192.168.1.100" \
  -F "matte=none"
```

**Check TV status:**

```bash
curl http://localhost:8000/tv/status?tv_ip=192.168.1.100
```

**List recent jobs:**

```bash
curl http://localhost:8000/jobs?limit=5
```

**Fetch a generated image:**

```bash
curl http://localhost:8000/jobs/120000-abc12345/image -o artwork.png
```

**List configured providers and model options:**

```bash
curl http://localhost:8000/providers
# {"default_provider":"openai","providers":[{"name":"openai","models":["gpt-image-1",...]}]}
```

**Async generation** (returns immediately, poll for status):

```bash
# Submit job
curl -X POST http://localhost:8000/async/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a stormy sea, oil painting"}'
# {"job_id":"143022-a1b2c3d4","status":"pending"}

# Poll until complete
curl http://localhost:8000/jobs/143022-a1b2c3d4/status
# {"job_id":"143022-a1b2c3d4","status":"completed","result":{...}}

# List recent async jobs
curl http://localhost:8000/async/jobs?limit=10
# [{"job_id":"143022-a1b2c3d4","status":"running","request":{"prompt":"...","provider":"openai","model":"gpt-image-1"}}]
```

**Discover TVs on the network:**

```bash
curl http://localhost:8000/tv/discover
# [{"ip":"192.168.1.100","name":"Living Room","model":"QN55LS03","frame_tv":true}]
```

**Search public-domain artwork:**

```bash
curl "http://localhost:8000/catalog/search?source=aic&q=landscape&limit=10"
```

**Apply public-domain artwork to TV:**

```bash
curl -X POST http://localhost:8000/catalog/apply \
  -H "Content-Type: application/json" \
  -d '{"source":"met","artwork_id":"436121","tv_ip":"192.168.1.100","matte":"none"}'
```

**Health check:**

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}
```

### Home Assistant Integration

You can call the FrameArt API from a Home Assistant automation or script using the `rest_command` integration:

```yaml
# configuration.yaml
rest_command:
  frameart_generate:
    url: "http://192.168.1.50:8000/generate-and-apply"
    method: POST
    headers:
      Content-Type: "application/json"
      Authorization: "Bearer YOUR_AUTOMATION_TOKEN"
    payload: '{"prompt": "{{ prompt }}"}'
    timeout: 120
```

Then use it in an automation:

```yaml
automation:
  - alias: "Morning art refresh"
    trigger:
      - platform: time
        at: "08:00:00"
    action:
      - service: rest_command.frameart_generate
        data:
          prompt: "a calm sunrise over a misty lake, watercolor style"
```

### Siri Shortcuts

Create a shortcut that sends an HTTP request:

1. Add a **"Get text from input"** or **"Ask for Input"** action for the prompt.
2. Add a **"Get Contents of URL"** action:
   - URL: `http://<frameart-host>:8000/generate-and-apply`
   - Method: POST
   - Headers: `Content-Type: application/json`, `Authorization: Bearer YOUR_AUTOMATION_TOKEN`
   - Request Body: `{"prompt": "<input>"}`
3. Trigger it with "Hey Siri, generate art" or add it to a scene.

---

## Style Presets

| Preset | Description |
|--------|-------------|
| `abstract` | Bold colors and geometric shapes |
| `kid_drawing` | Naive art, as drawn by an 8 year-old with crayons |
| `watercolor` | Soft edges and translucent washes |
| `bw_photo` | Black-and-white photograph, high contrast |
| `oil_painting` | Classical oil painting with rich textures |
| `pixel_art` | Retro video game aesthetic |
| `impressionist` | French impressionism, loose brushstrokes |
| `minimalist` | Clean lines and limited color palette |

You can also pass any freeform text as `--style` / `"style"` and it will be appended to your prompt directly.

---

## Configuration Reference

FrameArt reads configuration from (in priority order):

1. CLI flags / API request fields
2. Environment variables (`FRAMEART_` prefix)
3. `config.yaml` file

See [`config.example.yaml`](config.example.yaml) for all options.

### Key environment variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for DALL-E |
| `GOOGLE_API_KEY` | Google API key for Gemini image models |
| `GOOGLE_BASE_URL` | Override Google API base URL (default: `https://generativelanguage.googleapis.com/v1beta`) |
| `OLLAMA_BASE_URL` | Ollama server URL (default: `http://localhost:11434`) |
| `FRAMEART_DATA_DIR` | Data directory (default: `/data/frameart` or `~/.local/share/frameart`) |
| `FRAMEART_CONFIG` | Path to config file |
| `FRAMEART_DEFAULT_PROVIDER` | Default image provider |
| `FRAMEART_DEFAULT_UPSCALER` | Default upscaler |
| `FRAMEART_LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`) |
| `FRAMEART_AUTH_ENABLED` | Require scoped API tokens (`true`/`false`) |
| `FRAMEART_ADMIN_TOKEN` | Full-access API token; generated on first authenticated start when omitted |
| `FRAMEART_AUTOMATION_TOKEN` | Optional read/control token without destructive admin access |
| `FRAMEART_API_RATE_LIMIT_PER_MINUTE` | Per-client mutation limit (default: `60`) |
| `FRAMEART_MQTT_BROKER` | Optional MQTT hostname for schedule events |
| `FRAMEART_MQTT_PORT` | MQTT port (default: `1883`) |
| `FRAMEART_MQTT_USERNAME` / `FRAMEART_MQTT_PASSWORD` | Optional MQTT credentials |
| `FRAMEART_MQTT_TOPIC_PREFIX` | MQTT topic prefix (default: `frameart`) |

Install `frameart[integrations]` to enable MQTT. Schedule events publish at
`<prefix>/events/schedule.<status>`. Outbound HTTP webhooks include
`X-FrameArt-Event` and an HMAC-SHA256 `X-FrameArt-Signature`; the signing secret is
shown only when the webhook is created. Home Assistant can call a schedule's `/run`
endpoint with `Authorization: Bearer <FRAMEART_AUTOMATION_TOKEN>`.

### Multiple TVs

```yaml
tvs:
  livingroom_frame:
    ip: "192.168.1.100"
    token_file: "/data/frameart/secrets/livingroom.token"
  bedroom_frame:
    ip: "192.168.1.101"
    token_file: "/data/frameart/secrets/bedroom.token"
```

### Public domain providers

The `Public Domain` web UI tab and `/catalog/*` API currently support:

- `aic` (Art Institute of Chicago)
- `met` (The Metropolitan Museum of Art)
- `cma` (Cleveland Museum of Art)
- `europeana` (Europeana)

Optional key for Europeana (a demo key is used by default):

| Variable | Description |
|----------|-------------|
| `EUROPEANA_API_KEY` | Europeana API key (optional; defaults to `apidemo`) |

---

## Deployment

### Docker

#### docker-compose (recommended)

```bash
# Edit docker-compose.yml with your settings, then start the LAN-aware service:
docker compose --profile lan up -d frameart-api-lan
docker compose logs frameart-api-lan  # first start prints the generated admin token
```

The `docker-compose.yml` includes three services:

- **`frameart`** — one-shot CLI commands
- **`frameart-api`** — HTTP API on port 8000 using Docker bridge networking
- **`frameart-api-lan`** — recommended Linux API deployment with host networking for
  Samsung TV discovery and control

```bash
# CLI one-shot
docker compose run --rm frameart generate-and-apply \
    --prompt "a peaceful zen garden" \
    --tv-ip 192.168.1.100

# API server with SSDP discovery and Samsung TV control (runs in background)
docker compose --profile lan up -d frameart-api-lan
curl http://localhost:8000/health
```

The regular `frameart-api` service is useful when generation is all you need. Docker bridge
networking does not reliably carry the SSDP multicast traffic used by **Scan Network**. On a
Linux host, use `frameart-api-lan` whenever FrameArt needs to discover or control TVs. The LAN
service binds directly to the host's port 8000 and therefore does not use a `ports` mapping.

```bash
docker compose --profile lan up -d frameart-api-lan
```

#### Build and run directly

```bash
docker build -t frameart .

# CLI mode
docker run --rm \
    -e OPENAI_API_KEY="$OPENAI_API_KEY" \
    -v frameart_data:/data/frameart \
    frameart generate --prompt "a sunset"

# API server mode
docker run -d --name frameart-api \
    -e OPENAI_API_KEY="$OPENAI_API_KEY" \
    -e FRAMEART_AUTH_ENABLED=true \
    -v frameart_data:/data/frameart \
    -p 8000:8000 \
    --network host \
    frameart serve --host 0.0.0.0 --port 8000
```

### Proxmox VM

See [docs/VM.md](docs/VM.md) for running FrameArt in a Proxmox virtual machine. This is a good option if you want full OS isolation, or if you plan to run FrameArt alongside other services (e.g., Home Assistant) in the same VM.

### Proxmox LXC

See [docs/LXC.md](docs/LXC.md) for running FrameArt in a Proxmox LXC container. LXC is lighter weight than a VM and a good fit for a dedicated, single-purpose deployment.

---

## Data Layout

```
/data/frameart/
  artifacts/
    2025/01/15/
      143022-a1b2c3d4/
        source.png      # Raw output from provider
        final.png       # Post-processed 3840x2160
        meta.json       # Full metadata
  logs/
    frameart.log
  secrets/
    192_168_1_100.token  # Samsung TV auth tokens
```

---

## Troubleshooting

### TV not reachable

- Verify the TV is powered on (not in deep standby).
- Ping the TV: `ping 192.168.1.100`
- Check that ports 8001 (WS) and 8002 (WSS) are accessible.
- If running in Docker on Linux, use
  `docker compose --profile lan up -d frameart-api-lan`.
- Visit `http://<TV_IP>:8001/api/v2/` in a browser — you should get a JSON response.

### Scan Network fails or finds no TVs

- Confirm FrameArt and the TV are on the same subnet and client isolation is disabled on Wi-Fi.
- If running in Docker on Linux, use the `frameart-api-lan` service shown above. The regular
  bridge-networked API cannot reliably receive LAN SSDP responses.
- If multicast is blocked by the host or network, open **TVs → Add by IP** and enter the TV's
  RFC1918 address. Use **Save** on the discovered/manual TV card to turn it into a persistent
  profile, or keep it as a browser-session-only connection.

### Upload fails with error -1 on 2018/2019 Frame TVs

FrameArt automatically detects the TV's API version and uses the correct upload method:
- **API 0.97** (2018/2019 models): WebSocket binary frame upload
- **Newer APIs** (2020+): D2D socket handshake

If you still get error -1:
- Power-cycle the TV (unplug for 10 seconds).
- Re-pair: `frameart tv pair --tv-ip <IP>`
- Make sure the TV screen is on (not in standby).

### "TV mode" vs "Art mode"

Samsung Frame TVs have two modes:
- **TV mode**: Normal television display.
- **Art mode**: Displays artwork when the screen detects someone in the room.

FrameArt attempts to switch to Art Mode automatically. If it fails, press the power button once (short press) on your remote to toggle modes.

### Token invalid / pairing issues

- Delete the token file and re-pair: `rm /data/frameart/secrets/*.token`
- Re-run: `frameart tv pair --tv-ip <IP>`
- Some TVs require you to go to Settings > General > External Device Manager > Device Connection Manager and allow the device.

### Image not displaying after upload

- Check that the TV is in Art Mode: `frameart tv status --tv-ip <IP>`
- The image may need a moment to process on the TV after upload.
- Try listing artworks to confirm it uploaded: `frameart tv list-art --tv-ip <IP>`

### Public-domain image timeouts

- FrameArt first tries the full-resolution source image.
- If a provider's large source times out, FrameArt falls back to the thumbnail when available.
- In both cases, the same post-processing pipeline is applied (border trim, 16:9 crop, and 4K normalization).

### Provider API errors

- **OpenAI**: Ensure `OPENAI_API_KEY` is set and valid. DALL-E 3 requires a paid account.
- **Google**: Ensure `GOOGLE_API_KEY` is set (or `providers.google.api_key`) and your configured model supports image generation.
- **Ollama**: Ensure the Ollama server is running and reachable at the configured URL. Image generation requires a compatible model.

---

## Development

```bash
# Install with dev + api dependencies
pip install ".[dev,api]"

# Run tests
pytest

# Lint
ruff check frameart/ tests/

# Browser end-to-end tests (Node 20+; first run also needs the browser runtime)
npm ci
npx playwright install chromium
npm run test:e2e
```

Pull requests run the Python matrix, container build, and Playwright browser suite in
GitHub Actions. Failed browser runs upload a trace, screenshot, and HTML report for diagnosis.

## Architecture

```
frameart/
  cli.py              # Click CLI commands
  api.py              # FastAPI HTTP server (sync + async endpoints)
  jobs.py             # Async executor + restart-safe SQLite status history
  library.py          # Tags, collections, and TV display history
  automation.py       # TV groups, playlists, scheduler, webhooks, and MQTT
  pipeline.py         # Core orchestration: generate -> postprocess -> upload -> switch
  config.py           # Configuration management (YAML + env vars + CLI flags)
  postprocess.py      # 16:9 crop + 4K resize logic
  artifacts.py        # File storage and metadata
  static/
    index.html        # Semantic Web UI markup (no frontend build step)
    app.css           # Responsive visual design
    app.js            # UI state, API client, and interactions
  providers/
    base.py           # ImageProvider interface
    registry.py       # Provider name -> class mapping
    openai_adapter.py # DALL-E integration
    google_adapter.py # Google Gemini image generation integration
    ollama_adapter.py # Local model integration
  upscalers/
    base.py           # Upscaler interface
    registry.py       # Upscaler name -> class mapping
    none_upscaler.py  # Pillow LANCZOS (no external service)
    local_http.py     # LAN upscaler (e.g., Real-ESRGAN)
    remote_http.py    # Remote upscaler service
  tv/
    controller.py     # Samsung Frame TV: pair, upload, switch, status
    discovery.py      # UPnP/SSDP auto-discovery
```

---

## Known Limitations

- **No HTTPS on the API server.** Use a reverse proxy to terminate TLS.
- **Running jobs cannot resume mid-generation after a restart.** Their records survive and are marked failed with an interruption reason; completed results and bounded history remain available.
- **API rate limiting is per process.** This is appropriate for the supported single-worker local deployment; use a reverse proxy for distributed/global limits.

### Potential Future Work

- Calendar/cron schedule rules in addition to durable intervals
- Additional image providers (Anthropic, Stability AI)

---

## License

This project is licensed under **GNU GPL v3.0 or later**. See [LICENSE](LICENSE).

## Acknowledgements

FrameArt is built on top of excellent open-source projects, including:

- [samsungtvws](https://github.com/xchwarze/samsung-tv-ws-api) (Samsung TV WebSocket API client)
- [FastAPI](https://fastapi.tiangolo.com/)
- [Uvicorn](https://www.uvicorn.org/)
- [HTTPX2](https://github.com/Omega359/httpx2)
- [Pillow](https://python-pillow.org/)
- [Pydantic](https://docs.pydantic.dev/)
- [Click](https://click.palletsprojects.com/)

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for additional dependency notes.
