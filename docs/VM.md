# Running FrameArt in a Proxmox VM

This guide covers running FrameArt inside a virtual machine on Proxmox VE. A VM provides full OS isolation and is a good choice when you want to run FrameArt alongside other services, or when your environment requires a full Linux kernel (e.g., for GPU passthrough with local Ollama models).

For a lighter-weight alternative, see [LXC.md](LXC.md).

## Prerequisites

- Proxmox VE host (tested on 8.x)
- An ISO for Debian 12 (Bookworm) or Ubuntu 24.04 LTS uploaded to Proxmox storage
- Network access from the VM to the Samsung Frame TV (same subnet recommended)

## VM Setup

### 1. Create the VM

In the Proxmox web UI:

1. Click **Create VM**.
2. **General**: Name it `frameart`, pick a VM ID (e.g., 300).
3. **OS**: Select your Debian 12 or Ubuntu 24.04 ISO.
4. **System**: Defaults are fine. Enable **Qemu Agent** if you want `qm guest exec`.
5. **Disks**: 16 GB is plenty (8 GB for OS + room for artifacts). Use **VirtIO Block** for best performance.
6. **CPU**: 2 cores minimum. Image generation is network-bound (waiting on OpenAI), so CPU isn't critical unless running Ollama locally.
7. **Memory**: 2048 MB minimum. If running Ollama with local models, allocate 8 GB+.
8. **Network**: Bridge to `vmbr0` (or whichever bridge is on the same subnet as your TV). Use **VirtIO** NIC.

Or via the CLI on the Proxmox host:

```bash
qm create 300 \
    --name frameart \
    --cores 2 \
    --memory 2048 \
    --scsihw virtio-scsi-single \
    --scsi0 local-lvm:16,iothread=1 \
    --cdrom local:iso/debian-12.8.0-amd64-netinst.iso \
    --net0 virtio,bridge=vmbr0 \
    --boot order=scsi0 \
    --agent enabled=1

qm start 300
```

### 2. Install the OS

Complete the Debian/Ubuntu installer via the Proxmox noVNC console. A minimal server install (no desktop environment) is all you need. When prompted:

- Set the hostname to `frameart`.
- Create a user named `frameart`.
- Use guided partitioning (entire disk).

After installation, remove the ISO from the CD drive and reboot.

### 3. Install system dependencies

SSH into the VM or use the Proxmox console:

```bash
sudo apt-get update
sudo apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    libffi-dev \
    git
```

### 4. Create the data directory

```bash
sudo mkdir -p /data/frameart
sudo chown frameart:frameart /data/frameart
```

### 5. Install FrameArt

```bash
# As the frameart user
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

### 6. Configure

```bash
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

### 7. Set API keys

```bash
# Add to ~/.bashrc
echo 'export OPENAI_API_KEY="sk-..."' >> ~/.bashrc
source ~/.bashrc
```

## Usage

### CLI

```bash
source ~/.venv/frameart/bin/activate

# Pair with TV (one-time)
frameart tv pair --tv-ip 192.168.1.100

# Generate and display
frameart generate-and-apply \
    --prompt "Central Park in autumn" \
    --style oil_painting \
    --tv my_frame
```

### HTTP API server

```bash
source ~/.venv/frameart/bin/activate

# Start the API server (binds to all interfaces)
frameart serve --host 0.0.0.0 --port 8000
```

Then from any machine on the network:

```bash
curl http://192.168.1.50:8000/health
curl -X POST http://192.168.1.50:8000/generate-and-apply \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a sunset over the ocean"}'
```

## Running as a systemd Service

For a persistent API server that starts on boot, create a systemd unit:

```ini
# /etc/systemd/system/frameart-api.service
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
sudo systemctl daemon-reload
sudo systemctl enable --now frameart-api
sudo systemctl status frameart-api

# Check logs
sudo journalctl -u frameart-api -f
```

### Scheduled art changes (optional)

You can also run one-shot CLI commands on a schedule using a timer:

```ini
# /etc/systemd/system/frameart-daily.service
[Unit]
Description=Daily FrameArt generation
After=network.target

[Service]
Type=oneshot
User=frameart
WorkingDirectory=/home/frameart/FrameTV-GenAI-Artwork
Environment=FRAMEART_DATA_DIR=/data/frameart
Environment=OPENAI_API_KEY=sk-...
ExecStart=/home/frameart/.venv/frameart/bin/frameart generate-and-apply --prompt "daily art" --tv my_frame
```

```ini
# /etc/systemd/system/frameart-daily.timer
[Unit]
Description=Change Frame TV art daily

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now frameart-daily.timer
```

## Networking Notes

- The VM must be on the same subnet as the Samsung Frame TV, or have a route to it.
- Port 8001 (WS) and 8002 (WSS) must be reachable from the VM to the TV.
- If the VM is bridged to `vmbr0` (the same bridge as your LAN), no extra configuration is needed.
- If you want to reach the API from outside the LAN, set up port forwarding or a reverse proxy on the host.
- If you use a firewall on the VM, allow inbound TCP 8000 (API) and outbound TCP 8001/8002 (TV WebSocket).

## GPU Passthrough (optional, for Ollama)

If you want to run local image generation models via Ollama inside the VM, you can pass through a GPU:

1. Enable IOMMU on the Proxmox host (`intel_iommu=on` or `amd_iommu=on` in GRUB).
2. Identify your GPU's PCI address: `lspci | grep -i vga`.
3. Add it to the VM: `qm set 300 --hostpci0 01:00,pcie=1`.
4. Inside the VM, install NVIDIA drivers and Ollama.
5. Configure FrameArt to use the Ollama provider:

```yaml
default_provider: ollama
providers:
  ollama:
    base_url: "http://localhost:11434"
    model: "sdxl"
    timeout: 300
```

## Troubleshooting

- **"Connection refused" to TV**: Check that the TV is on and reachable (`ping <TV_IP>`). Verify ports 8001/8002 are not blocked.
- **Pillow build fails**: Make sure `libjpeg62-turbo-dev` and `zlib1g-dev` are installed before `pip install`.
- **Permission denied on /data/frameart**: Ensure the directory is owned by the `frameart` user.
- **API not reachable from other machines**: Check that you passed `--host 0.0.0.0` and that no firewall is blocking port 8000.
- **VM can't reach the TV**: Verify the VM's network bridge is on the same subnet. Check with `ip addr` and try `ping <TV_IP>`.

## System Dependencies Reference

| Package | Purpose |
|---------|---------|
| python3, python3-pip, python3-venv | Python runtime |
| python3-dev | Build Python C extensions |
| libjpeg62-turbo-dev | JPEG support for Pillow |
| zlib1g-dev | PNG support for Pillow |
| libffi-dev | Foreign function interface (for websocket crypto) |
