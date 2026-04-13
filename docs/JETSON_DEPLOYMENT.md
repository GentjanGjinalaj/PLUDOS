# Jetson Orin Nano: Complete Deployment Guide (Podman)

This guide walks you through deploying PLUDOS on a Jetson Orin Nano using **Podman containers**.

---

## **Pre-Deployment Checklist**

- [ ] Jetson running Ubuntu 22.04+ (L4T 35.x or later)
- [ ] WiFi connected to same network as STM32 (e.g., "Galaxy S24 Ultra")
- [ ] Git installed: `sudo apt install git`
- [ ] Jetson IP address known (run `ip -4 addr show wlan0` after WiFi connects)

---

## **Step 1: Install Container Runtime**

```bash
# Install Podman and Podman Compose
sudo apt update
sudo apt install -y podman podman-compose

# Verify installation
podman --version
podman-compose --version
```

**If `podman-compose` not found:** 
```bash
pip install podman-compose --user
```

---

## **Step 2: Clone PLUDOS Repository**

```bash
# Clone repo (replace with your actual Git URL)
git clone https://github.com/<your-username>/PLUDOS.git
cd PLUDOS

# Verify directory structure
ls -la
# Should see: client/, STM_Shuttles/, docs/, requirements.txt, etc.
```

---

## **Step 3: Get Your Jetson IP Address**

This IP must be **programmed into the STM32** so it knows where to send CoAP packets.

```bash
ip -4 addr show wlan0
```

**Example output:**
```
3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500
    inet 10.187.8.100/24 scope global dynamic noprefixroute wlan0
```

**Your Jetson IP: `10.187.8.100`**

Save this. You'll update the STM32 firmware next.

---

## **Step 4: Update STM32 Firmware (on Your Laptop)**

On your **laptop** (where STM32CubeIDE is running):

1. Open `STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c`
2. Find the line with `#define JETSON_IP`:
   ```c
   #define JETSON_IP "10.187.8.48"  // ← CHANGE THIS
   ```
3. Replace with your Jetson IP:
   ```c
   #define JETSON_IP "10.187.8.100"  // ← YOUR JETSON IP
   ```
4. Save, rebuild in STM32CubeIDE, flash to board

---

## **Step 5: Allow Container Networking (UFW Firewall)**

On Jetson, ensure CoAP and beacon ports are open:

```bash
# Allow CoAP (critical data)
sudo ufw allow 5683/udp

# Allow beacon discovery (non-critical)
sudo ufw allow 5000/udp

# Verify rules
sudo ufw status
```

---

## **Step 6: Deploy Data Engine Container**

```bash
cd client

# Build and start the data-engine service
podman-compose up --build data-engine
```

**Expected output (first 10 lines):**
```
2026-04-13 14:30:00,123 - __main__ - INFO - Starting PLUDOS Data Engine (Test Mode: False) on UDP Port 5683...
2026-04-13 14:30:00,456 - __main__ - INFO - Successfully created server context on 0.0.0.0:5683
[Listening on 0.0.0.0:5683...]
```

**Container is running.** The service will:
- Listen on port 5683 (CoAP)
- Accept packets from STM32 on your local WiFi
- Write `.parquet` files to `/app/ram_buffer` (inside container)

---

## **Step 7: Verify STM32 → Jetson Communication**

Once the container is running and STM32 is flashed with the correct Jetson IP:

**Check container logs in real-time:**
```bash
# In a new terminal, from PLUDOS/client:
podman logs pludos-data-engine -f

# You should see (after ~5 seconds):
# Received CoAP path: vib, payload size: 39
# Parsed shuttle_id=STM32-Alpha, sequence_id=0
# [INFO] NTP offset established for STM32-Alpha: 1234567 ms
```

If you see these logs, **CoAP communication is working! ✅**

---

## **Step 8: Run in Background (Optional: systemd Service)**

To keep the container running on Jetson restart:

### Option A: Simple Podman systemd Service

```bash
# Create systemd user service directory
mkdir -p ~/.config/systemd/user

# Create service file
cat > ~/.config/systemd/user/pludos-data-engine.service << 'EOF'
[Unit]
Description=PLUDOS Data Engine (CoAP Server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/warehouse1/PLUDOS/client
ExecStart=/usr/bin/podman-compose up data-engine
ExecStop=/usr/bin/podman-compose down
Restart=always
RestartSec=10s
User=warehouse1

[Install]
WantedBy=default.target
EOF

# Enable and start service
systemctl --user enable pludos-data-engine.service
systemctl --user start pludos-data-engine.service

# Verify it's running
systemctl --user status pludos-data-engine.service
```

---

## **Step 9: Access Data Files**

Data files are written inside the container to `/app/ram_buffer`. To retrieve them:

```bash
# List files inside container
podman exec pludos-data-engine ls -lh /app/ram_buffer/

# Copy a file to Jetson host
podman cp pludos-data-engine:/app/ram_buffer/mission_data_1681234567.parquet ./

# Read with Python (on Jetson, in a venv if desired)
python3 -c "import pandas as pd; df = pd.read_parquet('mission_data_1681234567.parquet'); print(df.head())"
```

---

## **Troubleshooting**

### Container won't start
```bash
# Check logs
podman-compose logs data-engine

# Rebuild from scratch
podman-compose down
podman rmi pludos-data-engine:latest
podman-compose up --build data-engine
```

### No packets from STM32
- Verify STM32 WiFi is connected to same network (open Android WiFi settings if using phone hotspot)
- Verify STM32 firmware has correct Jetson IP (see Step 4)
- Check Jetson firewall: `sudo ufw status` (5683/udp should be allowed)
- Ping test: `ping <JETSON_IP>` from another device on the same WiFi

### CoAP ACK failures in STM32 logs
- Ensure Jetson IP in STM firmware matches actual Jetson IP from `ip -4 addr show wlan0`
- Restart container: `podman-compose restart data-engine`
- Check Jetson is online: `ip a`

### "Permission denied" for systemd service
- Make sure paths in service file use absolute paths
- Use `--user` flag for systemctl (not `sudo systemctl`)
- Check user has write permission to working directory

---

## **Quick Reference: Daily Commands**

```bash
# Start data-engine on boot (systemd)
systemctl --user enable pludos-data-engine.service

# Check if running
systemctl --user status pludos-data-engine.service

# View live logs
systemctl --user status pludos-data-engine.service -u pludos-data-engine.service

# Stop service
systemctl --user stop pludos-data-engine.service

# Manual start (without systemd)
cd ~/PLUDOS/client && podman-compose up -d data-engine

# Stop manual container
cd ~/PLUDOS/client && podman-compose down
```

---

## **Environment Variables (Optional)**

You can customize behavior by passing environment variables to the container:

```bash
# In client/compose.yaml, add to data-engine service:
environment:
  - TEST_MODE=0         # 0 = production (writes to /app/ram_buffer), 1 = test
  - BUFFER_DIR=/app/ram_buffer  # Parquet output directory
```

---

## **Next Steps**

1. ✅ Deploy data-engine container
2. ✅ Verify STM32 ↔ Jetson communication
3. ⏭️ **Deploy AI worker** (federated XGBoost) — see `client/compose.yaml`
4. ⏭️ Set up Tailscale VPN for Jetson → Laptop relay (for ML model aggregation)
5. ⏭️ Configure InfluxDB + Grafana for monitoring (server/compose.yaml)

---

## **Support**

- **PLUDOS Architecture:** See [docs/ARCHITECTURE_AND_CONFIG.md](../ARCHITECTURE_AND_CONFIG.md)
- **Network Setup:** See [docs/NETWORK_SETUP.md](../NETWORK_SETUP.md)
- **Patch file details:** See [STM_Shuttles/PLUDOS_Edge_Node/tools/patches/README.md](../../STM_Shuttles/PLUDOS_Edge_Node/tools/patches/README.md)
