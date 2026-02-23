# FrameArt

Generate AI artwork from text prompts and display it on Samsung Frame TVs.

**prompt -> 4K art -> upload -> display**

FrameArt is a self-hosted tool that accepts a text description, generates an image using AI (local or remote providers), enforces 16:9 at 3840x2160, and uploads it directly to a Samsung The Frame TV over your local network. It ships as both a **CLI** and an **HTTP API** so you can drive it from a terminal, a voice assistant (Siri, Home Assistant), or any automation platform.

## Features

- **Multiple AI providers**: OpenAI DALL-E (working), Ollama/local models (working), Gemini and Anthropic (stubs)
- **Automatic post-processing**: Smart crop to 16:9, upscale/downscale to 4K UHD
- **Samsung Frame TV integration**: Upload art and switch display via WebSocket API
- **HTTP API**: FastAPI server for programmatic access — perfect for voice agents and Home Assistant
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

### Check TV status

```bash
frameart tv status --tv livingroom_frame
```

### List artworks on the TV

```bash
frameart tv list-art --tv livingroom_frame
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

# Or bind to all interfaces on a custom port
frameart serve --host 0.0.0.0 --port 8000
```

Interactive API docs are available at `http://localhost:8000/docs` once the server is running.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/generate-and-apply` | Full pipeline: prompt to TV display |
| `POST` | `/generate` | Generate image only (no TV upload) |
| `POST` | `/apply` | Upload an existing image to the TV |
| `GET` | `/tv/status` | Check TV connection and art mode |
| `GET` | `/jobs` | List recent jobs |
| `GET` | `/jobs/{job_id}/image` | Serve the final processed image |
| `GET` | `/styles` | List available style presets |
| `GET` | `/health` | Liveness check |

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
curl -X POST http://localhost:8000/apply \
  -H "Content-Type: application/json" \
  -d '{"image_path": "/data/frameart/artifacts/2025/01/15/120000-abc123/final.png", "tv_ip": "192.168.1.100"}'
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
   - Headers: `Content-Type: application/json`
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
| `OLLAMA_BASE_URL` | Ollama server URL (default: `http://localhost:11434`) |
| `FRAMEART_DATA_DIR` | Data directory (default: `/data/frameart` or `~/.local/share/frameart`) |
| `FRAMEART_CONFIG` | Path to config file |
| `FRAMEART_DEFAULT_PROVIDER` | Default image provider |
| `FRAMEART_DEFAULT_UPSCALER` | Default upscaler |
| `FRAMEART_LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`) |

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

---

## Deployment

### Docker

#### docker-compose (recommended)

```bash
# Edit docker-compose.yml with your settings, then:
docker compose up -d frameart-api
```

The `docker-compose.yml` includes two service profiles:

- **`frameart`** — one-shot CLI commands
- **`frameart-api`** — long-running HTTP API server on port 8000

```bash
# CLI one-shot
docker compose run --rm frameart generate-and-apply \
    --prompt "a peaceful zen garden" \
    --tv-ip 192.168.1.100

# API server (runs in background)
docker compose up -d frameart-api
curl http://localhost:8000/health
```

For Samsung TV control, you need host networking so the container can reach the TV's WebSocket port:

```yaml
# In docker-compose.yml, uncomment:
network_mode: host
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
- If running in Docker, try `network_mode: host`.
- Visit `http://<TV_IP>:8001/api/v2/` in a browser — you should get a JSON response.

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

### Provider API errors

- **OpenAI**: Ensure `OPENAI_API_KEY` is set and valid. DALL-E 3 requires a paid account.
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
```

## Architecture

```
frameart/
  cli.py              # Click CLI commands
  api.py              # FastAPI HTTP server
  pipeline.py         # Core orchestration: generate -> postprocess -> upload -> switch
  config.py           # Configuration management (YAML + env vars + CLI flags)
  postprocess.py      # 16:9 crop + 4K resize logic
  artifacts.py        # File storage and metadata
  providers/
    base.py           # ImageProvider interface
    registry.py       # Provider name -> class mapping
    openai_adapter.py # DALL-E integration
    ollama_adapter.py # Local model integration
    gemini_adapter.py # Stub
    anthropic_adapter.py # Stub
  upscalers/
    base.py           # Upscaler interface
    registry.py       # Upscaler name -> class mapping
    none_upscaler.py  # Pillow LANCZOS (no external service)
    local_http.py     # LAN upscaler (e.g., Real-ESRGAN)
    remote_http.py    # Remote upscaler service
  tv/
    controller.py     # Samsung Frame TV: pair, upload, switch, status
```

---

## Known Limitations and Roadmap

### Missing Today

- **No authentication on the HTTP API.** The server binds to `127.0.0.1` by default, but if you expose it on `0.0.0.0` there is no auth layer. For LAN-only use this is typically fine; for anything else, put it behind a reverse proxy with authentication (e.g., Caddy, nginx + basic auth, or Authelia).
- **No HTTPS on the API server.** Same recommendation: use a reverse proxy to terminate TLS.
- **No background/async jobs.** Image generation (especially DALL-E 3) can take 15-30 seconds. The HTTP API blocks until the pipeline completes. Callers should set an appropriate timeout (120s recommended). A future version could add async job submission with polling.
- **No rate limiting.** Each request triggers an AI provider API call. If the endpoint is exposed without rate limiting, it could rack up provider costs quickly.
- **No image upload via HTTP multipart.** The `/apply` endpoint takes a filesystem path, not a file upload. This works for local and Docker-volume use cases but not for remote clients sending image bytes over HTTP.
- **Gemini and Anthropic providers are stubs.** Only OpenAI and Ollama are fully implemented.
- **No TV auto-discovery.** You must know the TV's IP address. Samsung TVs advertise via UPnP/SSDP, which could be used for zero-config discovery.
- **No web UI.** The interactive docs at `/docs` work for testing, but there is no dedicated frontend.

### Potential Future Work

- Async job queue with `POST /jobs` returning a job ID and `GET /jobs/{id}` for polling
- API key / bearer token authentication
- HTTP multipart image upload for `/apply`
- UPnP/SSDP TV auto-discovery
- Webhook/callback on job completion
- Scheduling (cron-like "change art every morning")
- Multi-TV fan-out (upload to all TVs at once)
- Web UI for prompt entry and gallery browsing

---

## License

GPL-3.0 — see [LICENSE](LICENSE).
