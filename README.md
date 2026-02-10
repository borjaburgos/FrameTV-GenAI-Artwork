# FrameArt

Generate AI artwork from text prompts and display it on Samsung Frame TVs.

**prompt -> 4K art -> upload -> display**

FrameArt is a self-hosted CLI tool that accepts a text description, generates an image using AI (local or remote providers), enforces 16:9 at 3840x2160, and uploads it directly to a Samsung The Frame TV over your local network.

## Features

- **Multiple AI providers**: OpenAI DALL-E (working), Ollama/local models (working), Gemini and Anthropic (stubs)
- **Automatic post-processing**: Smart crop to 16:9, upscale/downscale to 4K UHD
- **Samsung Frame TV integration**: Upload art and switch display via websocket API
- **Style presets**: abstract, oil_painting, watercolor, kid_drawing, and more
- **Pluggable upscalers**: Built-in Pillow LANCZOS, local HTTP (Real-ESRGAN), or remote services
- **Artifact management**: Date-organized storage with full metadata tracking
- **Docker and LXC support**: Run anywhere on your home network

## Quickstart

### Install from source

```bash
git clone https://github.com/borjaburgos/FrameTV-GenAI-Artwork.git
cd FrameTV-GenAI-Artwork
pip install .
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

## Usage

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

## Configuration Reference

FrameArt reads configuration from (in priority order):

1. CLI flags
2. Environment variables (`FRAMEART_` prefix)
3. `config.yaml` file

See [`config.example.yaml`](config.example.yaml) for all options.

### Key environment variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for DALL-E |
| `OLLAMA_BASE_URL` | Ollama server URL (default: `http://localhost:11434`) |
| `FRAMEART_DATA_DIR` | Data directory (default: `/data/frameart`) |
| `FRAMEART_CONFIG` | Path to config file |
| `FRAMEART_DEFAULT_PROVIDER` | Default image provider |

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

## Docker

### docker-compose

```bash
# Edit docker-compose.yml with your settings, then:
docker compose run --rm frameart generate-and-apply \
    --prompt "a peaceful zen garden" \
    --tv-ip 192.168.1.100
```

For Samsung TV control, you may need host networking:

```yaml
# In docker-compose.yml, uncomment:
network_mode: host
```

### Build and run directly

```bash
docker build -t frameart .

docker run --rm \
    -e OPENAI_API_KEY="$OPENAI_API_KEY" \
    -v frameart_data:/data/frameart \
    frameart generate --prompt "a sunset"
```

## LXC

See [docs/LXC.md](docs/LXC.md) for running FrameArt in a Proxmox LXC container.

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

## Troubleshooting

### TV not reachable

- Verify the TV is powered on (not in deep standby).
- Ping the TV: `ping 192.168.1.100`
- Check that ports 8001 (WS) and 8002 (WSS) are accessible.
- If running in Docker, try `network_mode: host`.
- Visit `http://<TV_IP>:8001/api/v2/` in a browser — you should get a JSON response.

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

## Development

```bash
# Install with dev dependencies
pip install ".[dev]"

# Run tests
pytest

# Lint
ruff check frameart/ tests/
```

## Architecture

```
frameart/
  cli.py              # Click CLI commands
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

## License

GPL-3.0 — see [LICENSE](LICENSE).
