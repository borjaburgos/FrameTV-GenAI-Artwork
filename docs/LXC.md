# Running FrameArt in an LXC Container

This guide covers running FrameArt directly in a Debian/Ubuntu-based LXC container on Proxmox. LXC containers are lighter weight than full VMs and are a great fit for a dedicated, single-purpose FrameArt deployment.

For full OS isolation or GPU passthrough (e.g., for local Ollama models), see [VM.md](VM.md).

## Prerequisites

- Proxmox VE host (tested on 8.x)
- LXC container with Debian 12 (Bookworm) or Ubuntu 22.04+
- Network access to Samsung Frame TV (same subnet recommended)

## Container Setup

### 1. Create the LXC container

```bash
# On the Proxmox host
pct create 200 local:vztmpl/debian-12-standard_12.2-1_amd64.tar.zst \
    --hostname frameart \
    --cores 2 \
    --memory 2048 \
    --rootfs local-lvm:8 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1

pct start 200
pct enter 200
```

### 2. Install system dependencies

```bash
apt-get update
apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libffi-dev \
    git
```

### 3. Create a dedicated user

```bash
useradd -m -s /bin/bash frameart
mkdir -p /data/frameart
chown frameart:frameart /data/frameart
```

### 4. Install FrameArt

```bash
su - frameart

# Clone the repo
git clone https://github.com/borjaburgos/FrameTV-GenAI-Artwork.git
cd FrameTV-GenAI-Artwork

# Create virtual environment
python3 -m venv ~/.venv/frameart
source ~/.venv/frameart/bin/activate

# Install with API dependencies
pip install ".[api]"
```

### 5. Configure

```bash
# Copy and edit config
cp config.example.yaml config.yaml
nano config.yaml
```

Set your TV IP, provider API keys, and data directory:

```yaml
data_dir: /data/frameart
tvs:
  my_frame:
    ip: "192.168.1.100"
    token_file: "/data/frameart/secrets/my_frame.token"
```

### 6. Set API keys

```bash
# Add to ~/.bashrc or a secrets file
export OPENAI_API_KEY="sk-..."
```

## CLI Usage

```bash
# Activate the virtual environment
source ~/.venv/frameart/bin/activate

# Pair with TV (one-time)
frameart tv pair --tv-ip 192.168.1.100

# Generate and display
frameart generate-and-apply \
    --prompt "Central Park in autumn" \
    --style oil_painting \
    --tv my_frame
```

## HTTP API Server

Start the API server so external systems (Home Assistant, Siri, etc.) can trigger art generation over the network:

```bash
source ~/.venv/frameart/bin/activate

# Start on all interfaces
frameart serve --host 0.0.0.0 --port 8000
```

Then from any machine on the network:

```bash
# Health check
curl http://<LXC_IP>:8000/health

# Generate and display art
curl -X POST http://<LXC_IP>:8000/generate-and-apply \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a serene Japanese garden at sunset"}'

# Interactive API docs
# Open http://<LXC_IP>:8000/docs in a browser
```

## Running as a systemd Service

### API server (persistent, starts on boot)

Create `/etc/systemd/system/frameart-api.service`:

```ini
[Unit]
Description=FrameArt API Server
After=network.target

[Service]
Type=simple
User=frameart
WorkingDirectory=/home/frameart/FrameTV-GenAI-Artwork
Environment=FRAMEART_DATA_DIR=/data/frameart
Environment=OPENAI_API_KEY=sk-...
ExecStart=/home/frameart/.venv/frameart/bin/frameart serve --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now frameart-api
systemctl status frameart-api

# Check logs
journalctl -u frameart-api -f
```

### Scheduled art changes (optional)

Create `/etc/systemd/system/frameart.service`:

```ini
[Unit]
Description=FrameArt Service
After=network.target

[Service]
Type=oneshot
User=frameart
WorkingDirectory=/home/frameart/FrameTV-GenAI-Artwork
Environment=FRAMEART_DATA_DIR=/data/frameart
ExecStart=/home/frameart/.venv/frameart/bin/frameart generate-and-apply --prompt "daily art" --tv my_frame

[Install]
WantedBy=multi-user.target
```

Use a systemd timer for scheduled art changes:

```ini
# /etc/systemd/system/frameart.timer
[Unit]
Description=Change Frame TV art daily

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now frameart.timer
```

## System Dependencies Reference

| Package | Purpose |
|---------|---------|
| python3, python3-pip, python3-venv | Python runtime |
| python3-dev | Build Python C extensions |
| libjpeg62-turbo-dev | JPEG support for Pillow |
| zlib1g-dev | PNG support for Pillow |
| libffi-dev | Foreign function interface (for websocket crypto) |

## Networking Notes

- The LXC container must be on the same subnet as the Samsung Frame TV, or have a route to it.
- Port 8001 (WS) and 8002 (WSS) must be reachable from the container to the TV.
- If using a bridged network (`vmbr0`), no extra configuration is needed.
- If using a NAT or routed setup, ensure the TV subnet is routable.
- If you want to reach the API from outside the LAN, set up port forwarding or a reverse proxy on the Proxmox host.

## Troubleshooting

- **"Connection refused" to TV**: Check that the TV is on and reachable (`ping <TV_IP>`). Ensure ports 8001/8002 are not blocked.
- **Pillow build fails**: Make sure `libjpeg62-turbo-dev` and `zlib1g-dev` are installed before `pip install`.
- **Permission denied on /data/frameart**: Ensure the directory is owned by the `frameart` user.
- **API not reachable from other machines**: Check that you passed `--host 0.0.0.0` and that no firewall is blocking port 8000 in the container or on the Proxmox host.
