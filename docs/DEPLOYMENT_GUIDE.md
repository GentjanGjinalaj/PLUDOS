# PLUDOS Deployment Guide

Complete, step-by-step guide for a new engineer with the physical hardware and
a fresh clone of this repository. Follow the phases in order: server first,
then Jetson, then STM32 — that way the upstream sinks are ready before the
first packet arrives.

---

## System Topology

```
  Tier 1 (Extreme Edge)          Tier 2 (Gateway)              Tier 3 (Server)
  ─────────────────────          ─────────────────             ───────────────
  STM32U585 on B-U585I-IOT02A    Jetson Orin Nano Super        Laptop (Ubuntu)
                                 Developer Kit (8 GB)
  • Accelerometer (ISM330DLC)    • pludos-data-engine           • Flower ServerApp
  • Temp/Humidity (HTS221)         UDP listener port 5683         (flwr run .)
  • WiFi: MXCHIP EMW3080           Beacon UDP port 5000         • InfluxDB 2.7
    2.4 GHz only                 • pludos-ai-worker               port 8086
  • LPS22HH (local debug only)     XGBoost + Flower client      • Grafana
                                   AlumetProfiler                 port 3000
         raw UDP 5683 ─────────►  • pludos-alumet-relay
         (24-byte PludosTelemetry)  (INA3221, dormant)
                                        gRPC over Tailscale ──►  flwr run .
                                        InfluxDB HTTP  ────────►  port 8086
```

**Data flow summary (ADR-015 v2):**
1. STM32 streams 0.1 Hz `PludosTelemetry` packets in IDLE; transitions to
   MOVING when accelerometer deviation > 0.05 g² for 500 ms.
2. In MOVING the STM32 emits one 24-byte UDP packet per accelerometer
   sample to `udp://<gateway>:5683` at 10 Hz, fire-and-forget. No ACK, no
   retry, no SRAM buffer.
3. Gateway parses each packet, derives the absolute timestamp via per-shuttle
   NTP offset, and appends to a per-shuttle in-memory list.
4. When the shuttle stays in state==IDLE for `MISSION_END_IDLE_S` (default 30 s)
   after any MOVING run, the gateway writes one Parquet file for that shuttle
   and a `stm_mission` InfluxDB point. Per-shuttle buffer pressure flushes
   are also possible (`SHUTTLE_SOFT_LIMIT` / `SHUTTLE_HARD_LIMIT`).
5. `flwr run .` triggers an FL round: each Jetson loads the most recent
   Parquet files, trains XGBoost, ships booster bytes over gRPC/Tailscale.
6. `AlumetProfiler` writes 10 Hz power telemetry to InfluxDB during training.

---

## Phase 1: Bare-Metal Setup (STM32)

### 1.1 Prerequisites

| Tool | Version | Notes |
|---|---|---|
| STM32CubeIDE | 1.14+ | Primary build + flash IDE |
| arm-none-eabi-gcc | 13.3.rel1 | GNU Tools for STM32; installed by CubeIDE |
| ST-Link USB driver | — | Bundled with CubeIDE |

The project is a CubeMX-generated STM32CubeIDE project located at
`STM_Shuttles/PLUDOS_Edge_Node/`. **Do not edit the `.ioc` file directly.**
If a peripheral or pin needs changing, use STM32CubeIDE's Device Configuration
Tool GUI and regenerate.

### 1.2 Set WiFi and Network Credentials

The firmware reads credentials from a gitignored header. Create it from the
committed template:

```bash
cd STM_Shuttles/PLUDOS_Edge_Node/Core/Inc
cp wifi_credentials.h.example wifi_credentials.h
```

Edit `wifi_credentials.h` and fill in your values:

```c
#define WIFI_SSID     "YourHotspotSSID"    /* 2.4 GHz only — MXCHIP does not support 5 GHz */
#define WIFI_PASSWORD "YourPassword"
#define JETSON_IP     "10.x.x.x"          /* Jetson's wlan0 IP — used only if beacon times out */
#define SHUTTLE_ID    1U                   /* 1-based integer, UNIQUE per device (1, 2, 3 …) */
```

> **Security:** `wifi_credentials.h` is gitignored. Never commit it. The
> `.example` template is safe to commit and already is.

### 1.3 Build the Firmware

**Option A — STM32CubeIDE (recommended):**

1. `File → Open Projects from File System…` → select
   `STM_Shuttles/PLUDOS_Edge_Node/`.
2. Right-click the project → `Build Project`.
3. Artifact: `STM_Shuttles/PLUDOS_Edge_Node/Debug/PLUDOS_Edge_Node.elf`

**Option B — CLI (requires matching path):**

The CubeMX-generated `Debug/makefile` hard-codes the linker script path to
the original developer machine. Before using it on a different machine, update
line 67 of `STM_Shuttles/PLUDOS_Edge_Node/Debug/makefile`:

```makefile
# Change /home/ggjinalaj/... to your absolute checkout path, e.g.:
-T"/home/<you>/PLUDOS/STM_Shuttles/PLUDOS_Edge_Node/STM32U585AIIXQ_FLASH.ld"
```

Then build:

```bash
cd STM_Shuttles/PLUDOS_Edge_Node/Debug
make clean && make -j4
```

### 1.4 Flash the Board

Connect the B-U585I-IOT02A to your laptop via the ST-Link USB connector (the
port labelled **CN18**, not the USB-C power port).

**STM32CubeIDE:** `Run → Run` (or `Run → Debug`) — the IDE flashes the `.elf`
automatically.

**ST-Link Utility (GUI alternative):**
Open `STM32 ST-LINK Utility`, connect to target, open
`STM_Shuttles/PLUDOS_Edge_Node/Debug/PLUDOS_Edge_Node.elf`, then
`Target → Program & Verify`.

### 1.5 Verify: UART Boot Log

Connect a serial terminal (e.g. `minicom`, `screen`, PuTTY) to the board's
ST-Link virtual COM port at **115200 baud, 8N1, no flow control**.

Expected boot sequence within ~5 seconds:

```
[NETWORK] WiFi init sequence starting...
[NETWORK] Performing WiFi module hard reset...
[NETWORK] WiFi SPI bus registered successfully
[NETWORK] Connecting to WiFi: 'YourHotspotSSID'
[NETWORK] SUCCESS! IP: 10.x.x.x
[SENSOR] State machine initialized. State: IDLE
```

If WiFi hangs at "Connecting…", see `docs/WIFI_FIX_AND_BUILD.md` for the
known EXTI ISR routing fix.

---

## Phase 2: Edge Node Setup (Jetson Orin Nano)

### 2.1 Host Prerequisites

The Jetson is a container runtime host only — no Python virtualenv needed on
the host itself.

```bash
# Update and install container runtime
sudo apt update
sudo apt install -y podman podman-compose git

# If podman-compose is not found via apt:
pip install podman-compose --user

# Verify
podman --version
podman-compose --version
```

Minimum requirements: Ubuntu 22.04 (L4T r35.x), WiFi on the same 2.4 GHz
network as the STM32 shuttle(s).

### 2.2 Clone the Repository

```bash
git clone https://github.com/<your-username>/PLUDOS.git
cd PLUDOS
```

### 2.3 Get the Jetson's IP Address

This IP must match `JETSON_IP` in the STM32 firmware (`wifi_credentials.h`).

```bash
ip -4 addr show wlan0
# Look for: inet 10.x.x.x/24
```

Note the address. Update `JETSON_IP` in
`STM_Shuttles/PLUDOS_Edge_Node/Core/Inc/wifi_credentials.h`, rebuild, and
reflash if you haven't already.

### 2.4 Configure the Environment File

```bash
cd client
cp .env.example .env
```

Edit `client/.env`. Mandatory values:

```bash
# Tailscale auth key — required only for AI-worker (vpn profile)
TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx

# InfluxDB — must match the server's InfluxDB instance (Phase 3)
INFLUXDB_URL=http://<server-tailscale-ip>:8086
INFLUXDB_TOKEN=pludos-secret-token
INFLUXDB_ORG=pludos
INFLUXDB_BUCKET=alumet_energy
```

The full set of tunable environment variables is documented in
`client/.env.example` with defaults shown. All variables are optional
except `TS_AUTHKEY` (required when running the `vpn` profile for FL rounds)
and the `INFLUXDB_*` block (required for energy telemetry to reach the server).

### 2.5 Firewall Notes

The Jetson (JetPack 6 / Ubuntu 22.04) does **not** have `ufw` installed by
default. No firewall configuration is needed: the `data-engine` container
runs with `network_mode: host`, so ports 5683 and 5000 are bound directly
on the host network interface and are reachable from the WiFi subnet.

If `ufw` is installed and active, allow the ports:

```bash
sudo ufw allow 5683/udp   # raw UDP — 24-byte PludosTelemetry (ADR-016 v3)
sudo ufw allow 5000/udp   # UDP broadcast beacon (zero-touch provisioning)
sudo ufw status
```

### 2.6 Start the Data Engine

The `data-engine` service is always-on: it listens for STM32 packets
regardless of whether an FL round is running.

> **`podman-compose` path:** on JetPack 6 the tool is installed via `pip install
> --user podman-compose` and lives at `~/.local/bin/podman-compose`. It is not
> on the system `PATH` when connecting via SSH without a login shell. Use the
> full path (`~/.local/bin/podman-compose`) or add `~/.local/bin` to `PATH` in
> `~/.bashrc`.

```bash
cd ~/PLUDOS/client

# First run (or after any code change): build the image, then start.
~/.local/bin/podman-compose up --build -d data-engine

# IMPORTANT: after a code change, 'restart' reuses the old image.
# You must stop + remove the container so the new image is used:
podman stop pludos-data-engine
podman rm   pludos-data-engine
~/.local/bin/podman-compose up -d data-engine

# Live logs:
podman logs -f pludos-data-engine
```

**Healthy log output after STM32 connects:**

```
[BEACON] announcing 192.168.1.10 on UDP port 5000 every 10 s (group=1,2)
[STM32-Alpha] NTP offset established: 1234567 ms (state=IDLE)
[STM32-Alpha] IDLE 1.0Hz seq=1 accel=(0.00,-0.00,1.01)g temp=22.3°C hum=45% pwr=89mW e=0.00J
[STM32-Alpha] MOVING 50.0Hz seq=180 accel=(-0.04,0.07,0.97)g temp=22.3°C hum=45% pwr=260mW e=0.27J
```

**Verify a Parquet file was written** (appears after `MISSION_END_IDLE_S`
of state==IDLE following any MOVING run, default 30 s):

```bash
ls -lh ~/PLUDOS/client/ram_buffer/
# mission_1779041235.parquet
```

### 2.7 Daily Operations & Monitoring

#### Container status

```bash
# See all containers and their state (Up / Exited / Created)
podman ps -a

# Check which image a running container is using
podman inspect pludos-data-engine --format 'image={{.ImageName}} created={{.Created}}'
```

#### Watching logs

```bash
# Follow live (Ctrl+C to stop)
podman logs -f pludos-data-engine
podman logs -f pludos-alumet-relay

# Last N lines only
podman logs --tail=50 pludos-data-engine
```

#### Restart after a small code change

```bash
cd ~/PLUDOS/client

# 1. Pull the latest code
git pull

# 2. Rebuild the image (only the changed service)
~/.local/bin/podman-compose up --build -d data-engine

# 3. The new image is built but the old CONTAINER is still running.
#    'restart' does NOT swap the image — you must recreate the container:
podman stop pludos-data-engine
podman rm   pludos-data-engine
~/.local/bin/podman-compose up -d data-engine

# 4. Verify the new version is running (check startup log line says "ADR-015 v2")
podman logs --tail=5 pludos-data-engine
```

#### Restart the full stack

```bash
cd ~/PLUDOS/client
~/.local/bin/podman-compose down     # stop and remove all containers
~/.local/bin/podman-compose up -d    # recreate from current images
```

#### Rebuild everything from scratch (e.g. after requirements.txt change)

```bash
cd ~/PLUDOS/client
~/.local/bin/podman-compose down
~/.local/bin/podman-compose up --build -d
```

#### Check what's in the buffer

```bash
# Parquet files (missions flushed to disk)
ls -lh ~/PLUDOS/client/ram_buffer/

# Count accumulated packets (requires pyarrow on host, or inspect via container)
podman exec pludos-data-engine python3 -c "
import os, pyarrow.parquet as pq
for f in sorted(os.listdir('/app/ram_buffer')):
    t = pq.read_table(f'/app/ram_buffer/{f}')
    print(f'{f}: {len(t)} rows')
"
```

#### Silence the CNI firewall warning

Every `podman` command prints:
```
Error validating CNI config file … plugin firewall does not support config version "1.0.0"
```
This is cosmetic — networking works. Fix it once:

```bash
rm ~/.config/cni/net.d/client_default.conflist
# podman recreates the file correctly on the next compose up
```

### 2.8 Alumet Energy Monitoring (Phase 1 — tegrastats)

The `AlumetProfiler` in `client/client.py` uses `tegrastats` to read the
Jetson's INA3221 power rails (`VDD_GPU`, `VDD_CPU`, `VDD_SOC`) during each FL
training round. `tegrastats` is part of JetPack — no installation needed.

Verify before running an FL round:

```bash
# Confirm tegrastats is available
which tegrastats
tegrastats --interval 100 --count 1
# Expected output includes: VDD_GPU xxxmW VDD_CPU xxxmW VDD_SOC xxxmW

# Check the active power mode (tagged in every InfluxDB point)
sudo nvpmodel -q
# e.g.: NV Power Mode: MAXN_SUPER

# Set max performance for reproducible benchmarks (optional)
sudo nvpmodel -m 0   # MAXN_SUPER — 25 W
sudo jetson_clocks   # lock clocks
```

In `TEST_MODE=1` (laptop), `AlumetProfiler` falls back to randomised mock
values so InfluxDB points still flow without hardware.

**Phase 2 (open):** replacing the `tegrastats` subprocess call with a local
Alumet relay sidecar. See ADR-011 in `docs/decisions.md` and the
`pludos-alumet` skill for the implementation plan.

### 2.8 Start the AI Worker (FL rounds)

The `ai-worker` service requires Tailscale to reach the central server and is
gated behind the `vpn` profile.

```bash
cd ~/PLUDOS/client

# Start both data-engine and ai-worker + Tailscale sidecar
podman-compose --profile vpn up -d

# Watch AI worker logs during an FL round
podman logs -f pludos-ai-worker
```

The AI worker will wait until `flwr run .` is executed on the server (Phase 3)
before training begins.

### 2.9 Run as a Persistent Systemd Service (Optional)

To survive reboots without a login session:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/pludos-data-engine.service << 'EOF'
[Unit]
Description=PLUDOS Data Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/<your-username>/PLUDOS/client
ExecStart=/usr/bin/podman-compose up data-engine
ExecStop=/usr/bin/podman-compose down
Restart=always
RestartSec=10s

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now pludos-data-engine.service
loginctl enable-linger $(whoami)   # start at boot without interactive login
```

---

## Phase 3: Central Server & Telemetry (Laptop)

### 3.1 Python Environment

```bash
cd ~/PLUDOS
python3 -m venv pludos_venv
source pludos_venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` at the repo root includes `flwr`, `xgboost`,
`influxdb-client`, `pandas`, `pyarrow`, and all transitive dependencies
at pinned versions. (`aiocoap` was removed with ADR-015 — the data-engine
no longer needs it.)

### 3.2 Configure the Server Environment File

```bash
cd ~/PLUDOS/server
cp .env.example .env
```

Edit `server/.env`. At minimum, rotate the two secrets before any non-local
deployment:

```bash
INFLUXDB_ADMIN_PASSWORD=changeme   # change this
INFLUXDB_ADMIN_TOKEN=changeme-pludos-token  # change this — Jetson client/.env must match
GRAFANA_ADMIN_PASSWORD=changeme    # change this
```

The token value in `server/.env` → `INFLUXDB_ADMIN_TOKEN` **must match**
`INFLUXDB_TOKEN` in `client/.env` on every Jetson gateway.

### 3.3 Start the Monitoring Stack (InfluxDB + Grafana + Alumet)

```bash
cd ~/PLUDOS/server

# First run: builds the Alumet container (~10 min, Rust compilation)
podman-compose up --build

# Subsequent runs:
podman-compose up -d
```

Services started by `server/compose.yaml`:

| Service | Container | Port | Credentials (from server/.env) |
|---|---|---|---|
| InfluxDB 2.7 | `pludos-influxdb` | `localhost:8086` | `INFLUXDB_ADMIN_USER` / `INFLUXDB_ADMIN_PASSWORD` |
| Grafana | `pludos-grafana` | `localhost:3000` | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` |
| Alumet (server energy) | `pludos-alumet` | (internal) | writes to InfluxDB automatically |

Verify InfluxDB is accepting writes:

```bash
curl -s http://localhost:8086/health | python3 -m json.tool
# "status": "pass"
```

Verify Alumet is running and writing to InfluxDB:

```bash
podman logs pludos-alumet
# Expected: alumet-cli startup lines, then periodic "wrote point" messages
```

> **RAPL note:** the Alumet container reads `/sys/class/powercap` from the
> host for Intel CPU energy measurement. On AMD processors or VMs without
> RAPL access, Alumet falls back to process-level CPU stats (less accurate).
> Check `ls /sys/class/powercap/intel-rapl/` on the host before starting.

### 3.4 Connect Grafana to InfluxDB

1. Open `http://localhost:3000` → log in with credentials from `server/.env`.
2. `Connections → Data Sources → Add data source → InfluxDB`.
3. Configure exactly:
   - **Query Language:** Flux
   - **URL:** `http://influxdb:8086` (internal Podman network name — not `localhost`)
   - **Organization:** `pludos`
   - **Token:** value of `INFLUXDB_ADMIN_TOKEN` from `server/.env`
   - **Default Bucket:** `alumet_energy`
4. `Save & Test` → expect `"datasource is working. 1 buckets found"`.

### 3.5 Grafana Dashboard: Energy Telemetry

Create a new dashboard → `Add visualization` → switch to **Script editor**.

**GPU + CPU power per FL round** (primary training curve):
```flux
from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "fl_energy")
  |> filter(fn: (r) =>
       r["_field"] == "power_gpu_w"   or
       r["_field"] == "power_cpu_w"   or
       r["_field"] == "power_total_w"
    )
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
```

Group by `fl_round` and `device` tags to get per-round, per-device curves.
Jetson data has `device=jetson-<hostname>`; server has `device=server`.

**Cumulative energy per round:**
```flux
from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "fl_energy")
  |> filter(fn: (r) => r["_field"] == "energy_j")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "energy_j")
```

See `docs/ANALYTICS.md` for the full schema reference and additional queries.

**Metrics to monitor for system health:**

| Metric | Field | What to look for |
|---|---|---|
| GPU power during FL round | `power_gpu_w` | Spike during `model.fit()` (production: 5–25 W range) |
| Cumulative training energy | `energy_j` | Monotonically increasing during a round; flat between rounds |
| CPU power | `power_cpu_w` | Elevated during data loading + Flower gRPC |
| Total system power | `power_total_w` | Baseline ~7 W idle; training pushes toward 15–25 W |
| NVPModel tag | tag: `nvpmodel` | Must match what `nvpmodel -q` returns on the Jetson |

Set the Grafana time range to **Last 5 minutes** — FL training rounds complete
in under 5 seconds on the Jetson GPU.

### 3.6 Start a Federated Learning Round

There are two modes. Use **simulation** for development (single machine,
no Jetson needed). Use **real deployment** when physical Jetsons are
connected over Tailscale.

#### Mode A — Simulation (default, single machine)

`pyproject.toml` default federation `pludos-sim` runs server + one fake
client in the same process, loading Parquet files from the local disk.

```bash
cd ~/PLUDOS
source pludos_venv/bin/activate

# Optional env overrides (shell — these are not read from server/.env):
# export FL_NUM_ROUNDS=5
# export FL_MIN_FIT_CLIENTS=1

flwr run .          # uses pludos-sim federation (num-supernodes=1)
```

Expected output per round:
```
--- ROUND 1: aggregating from 1 gateway(s) ---
Single gateway: booster forwarded unchanged (XXXXX B).
```

#### Mode B — Real deployment (physical Jetsons over Tailscale)

**Step 1 — Server: open firewall and start SuperLink**

```bash
# Allow Flower gRPC from Tailscale network (100.64.0.0/10 is the CGNAT range)
sudo ufw allow 9091/tcp
sudo ufw allow 9091/tcp from 100.64.0.0/10

# Start SuperLink (keeps running; run in a tmux pane or as a service)
cd ~/PLUDOS
source pludos_venv/bin/activate
flower-superlink --insecure
```

**Step 2 — Each Jetson: start a SuperNode**

```bash
# Get the server's Tailscale IP on the server:
tailscale ip -4     # e.g. 100.x.x.x

# On each Jetson (inside the ai-worker container or the venv):
flower-supernode --insecure --superlink <server-tailscale-ip>:9091
```

**Step 3 — Server: run the FL app**

```bash
# Uncomment [tool.flwr.federations.pludos-network] in pyproject.toml first.
# Raise FL_MIN_FIT_CLIENTS to the actual number of Jetsons connected.
cd ~/PLUDOS
FL_MIN_FIT_CLIENTS=2 flwr run . pludos-network
```

Expected output per round with 2 Jetsons:
```
--- ROUND 1: aggregating from 2 gateway(s) ---
Tree-set union: 2 gateways → 20 trees total (XXXXX B).
```

### 3.7 Run the Server as a Persistent Systemd Service

Mirrors the Jetson recipe in §2.9 — brings up the entire `server/compose.yaml`
stack (InfluxDB + Grafana + Alumet + the `fl-trigger` watcher described in §3.8)
on boot without requiring an interactive login.

The unit file is committed to the repo so it stays in sync with the compose
stack:

```bash
mkdir -p ~/.config/systemd/user
cp ~/PLUDOS/server/systemd/pludos-server.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now pludos-server.service

# Required so the user service starts at boot with no shell login.
loginctl enable-linger $(whoami)
```

Verify after a reboot:

```bash
systemctl --user status pludos-server.service
podman ps                       # expect 4 containers: influxdb, grafana, alumet, fl-trigger
```

> **Path note:** the committed unit hard-codes `/usr/bin/podman-compose`. If the
> distro installs it elsewhere (`pip install --user podman-compose` puts it at
> `~/.local/bin/podman-compose`), edit `ExecStart` / `ExecStop` in your local
> copy under `~/.config/systemd/user/`. Leaving the in-repo copy untouched keeps
> it portable across machines.

To stop the stack temporarily (e.g. for an InfluxDB schema migration):

```bash
systemctl --user stop pludos-server.service
```

`Restart=always` brings the stack back up if `podman-compose up` exits — for
example after a transient image pull failure during a Podman update.

### 3.8 Automatic FL Round Trigger (`fl-trigger`)

`server/compose.yaml` includes a `fl-trigger` service that watches InfluxDB for
gateway readiness and launches `flwr run .` autonomously. It removes the need
for a human to run `flwr run .` after every mission set.

**Readiness logic:** a gateway is considered ready when *either* condition
holds since the last successful FL run:

- The gateway emitted a `gw_status` InfluxDB point with `parquet_files_available > 0`
  (heartbeat; written by `client/client.py` at startup and after each round's
  `evaluate()`), or
- The gateway wrote one or more `stm_mission` points (mission flush; written by
  `client/data-engine.py` on every Parquet flush).

When `len(ready_gateways) >= FL_MIN_FIT_CLIENTS`, the trigger spawns
`flwr run .` from the bind-mounted PLUDOS repo at `/app/project`, waits for it
to exit, and writes `/app/state/last_run.json` with the round number, exit code,
participant list, and parsed per-client accuracy.

**Restart safety:** a pidfile at `/app/state/trigger.pid` prevents two trigger
instances (e.g. after a `podman restart`) from launching `flwr run .` at the
same time. A stale pidfile (process no longer alive) is reclaimed automatically.

**Tunables** in `server/.env` (defaults shown):

```bash
FL_TRIGGER_INTERVAL_S=30   # poll period in seconds
FL_MIN_FIT_CLIENTS=1       # gateways required before firing a round
FL_NUM_ROUNDS=3            # forwarded to server.py
```

**Inspecting trigger state**:

```bash
podman exec pludos-fl-trigger cat /app/state/last_run.json
podman exec pludos-fl-trigger ls /app/state/logs/
podman logs -f pludos-fl-trigger
```

**Models written by the trigger** (Commit 3 change): after every successful
round, `server/server.py` writes the merged XGBoost booster to
`server/models/global_model_round_<N>.ubj` and atomically updates the
`server/models/latest.ubj` symlink. Recover from a crashed server by loading
`latest.ubj` directly into `xgb.Booster.load_model()` — no need to replay
training.

**Disabling the trigger temporarily** (for manual `flwr run .` runs):

```bash
podman stop pludos-fl-trigger
# … manual flwr run . from the host venv …
podman start pludos-fl-trigger
```

---

## Network Binding

How the three tiers discover each other:

| Link | Protocol | Port | Config location |
|---|---|---|---|
| STM32 → Jetson (telemetry) | raw UDP (28 B) | 5683 | beacon-discovered; `wifi_credentials.h` → `JETSON_IP` is the boot-time fallback only |
| Jetson → STM32 beacon | UDP broadcast | 5000 | `client/.env` → `GATEWAY_IP`, `SHUTTLE_GROUP` |
| Jetson → Server (FL gRPC) | gRPC over Tailscale | 9091 | `client/.env` → `TS_AUTHKEY`; §3.6 Mode B |
| Jetson → Server (InfluxDB) | HTTP over Tailscale | 8086 | `client/.env` → `INFLUXDB_URL` |
| Server Alumet → InfluxDB | HTTP (internal bridge) | 8086 | automatic via compose bridge |
| Grafana → InfluxDB | HTTP (internal bridge) | 8086 | Grafana data-source GUI |

**Getting the Tailscale IPs:**

```bash
# On both the Jetson and the laptop, after joining the tailnet:
tailscale ip -4
# Returns 100.x.x.x — use this as <server-tailscale-ip> in client/.env
```

**Beacon broadcast (zero-touch IP provisioning — end-to-end):**

The `data-engine` broadcasts `PLUDOS-GW:<ip>` (or `PLUDOS-GW:<ip>:<csv-ids>`
when `SHUTTLE_GROUP` is set) to `255.255.255.255:5000` every 10 seconds.
`network_mode: host` is already set on the `data-engine` service in
`client/compose.yaml` so the broadcast escapes the container bridge.
`GATEWAY_IP` in `client/.env` is optional — if unset, data-engine auto-
detects the outbound interface IP.

The STM32 firmware listens for this beacon at boot (30 s patient probe),
on every WiFi reconnect (short 500 ms probe), and periodically every 30 s
while IDLE. In a multi-Jetson WiFi (3-Jetson dev rig), set
`SHUTTLE_GROUP=1,2` / `3,4` / `5,6` per Jetson so STMs bond only to their
assigned gateway. See `docs/DEPLOYMENT_3JETSON.md` for the full recipe.
`JETSON_IP` in `wifi_credentials.h` is the compile-time fallback used
only if the very first boot beacon probe times out.

---

## Pre-Test Connection Checklist

Run through this before first hardware test. Each item maps to a
real network path or config dependency.

### Server-side (laptop)

```bash
# 1. InfluxDB health
curl -s http://localhost:8086/health | python3 -m json.tool
# → "status": "pass"

# 2. Grafana reachable
curl -s -o /dev/null -w "%{http_code}" http://localhost:3000
# → 200 or 302

# 3. Alumet container running and writing
podman logs pludos-alumet 2>&1 | tail -5
# → alumet-cli startup + periodic write lines
# If RAPL unavailable: ls /sys/class/powercap/intel-rapl/ — expect empty on AMD

# 4. InfluxDB accessible from Tailscale network (Jetson writes here)
sudo ufw allow 8086/tcp from 100.64.0.0/10
# Verify Tailscale IP is assigned:
tailscale ip -4       # note this — goes into client/.env INFLUXDB_URL

# 5. Flower SuperLink port (Mode B only)
sudo ufw allow 9091/tcp from 100.64.0.0/10
```

### Jetson-side (run before `podman-compose up`)

```bash
# 6. client/.env values set
grep -E "INFLUXDB_URL|TS_AUTHKEY" ~/PLUDOS/client/.env
# INFLUXDB_URL must be http://<server-tailscale-ip>:8086 (not localhost)
# TS_AUTHKEY must be a valid tskey-auth-... value

# 7. Tailscale joined and can reach server
tailscale ip -4                              # Jetson's Tailscale IP
ping -c 2 <server-tailscale-ip>             # must succeed
curl -s http://<server-tailscale-ip>:8086/health | python3 -m json.tool
# → "status": "pass"  — if this fails, check server ufw rule from step 4

# 8. tegrastats working (AlumetProfiler prerequisite)
which tegrastats && tegrastats --interval 100 --count 1 | grep VDD_GPU
# → line containing VDD_GPU Xm W — if missing, TEST_MODE=1 is the fallback

# 9. Ports open on Jetson
sudo ufw allow 5683/udp   # raw UDP PludosTelemetry (ADR-015 v2)
sudo ufw allow 5000/udp   # beacon broadcast (network_mode: host already set)

# 10. data-engine container listening
podman logs pludos-data-engine 2>&1 | tail -3
# → "Telemetry UDP listener bound on port 5683"
# → "[BEACON] announcing <ip> on UDP port 5000 every 10 s (group=...)"
```

### STM32 → Jetson smoke test

```bash
# From the repo root on the laptop. Default target is 127.0.0.1; override
# with TELEMETRY_HOST=<jetson-wlan0-ip> for a real Jetson.
python3 tools/mock_stm32.py
# Or stress-test 6 shuttles at once (3-Jetson dev rig):
# MOCK_SHUTTLES=6 python3 tools/mock_stm32.py

# Jetson data-engine log should show:
# [STM32-Alpha] NTP offset established: ...
# [STM32-Alpha] IDLE 1.0Hz seq=1 accel=(0.00,-0.00,1.01)g temp=22.3°C ...
```

### Known gaps (will not block first test)

| Gap | Impact | Workaround |
|---|---|---|
| Alumet `cargo install alumet-cli` may fail if package name differs on crates.io | `pludos-alumet` container won't start | Server energy measurement missing; all other services unaffected. Verify with `podman build server/alumet` and check the error. |
| `flwr run .` simulation mode doesn't exercise real Jetson client | FL tree-set union not tested end-to-end multi-gateway | Use Mode B (§3.6) with `flower-superlink` + `flower-supernode` |
| `client/Containerfile` uses CPU-only `python:3.10-slim` | `ai-worker` falls back to CPU XGBoost on the Jetson | Tracked separately — needs a JetPack 6 CUDA base image |
| `POWER_MOVING_MW` default (260 mW) is a datasheet rough estimate | Per-mission `energy_j` numbers are ±40% | Bench-ammeter calibration when hardware is on the rig |

---

## Customization Guide

Four files cover the majority of behavioural tuning:

### 1. `STM_Shuttles/PLUDOS_Edge_Node/Core/Inc/wifi_credentials.h`

Per-shuttle network identity. Must be edited before each flash:

```c
#define WIFI_SSID     "..."           // 2.4 GHz AP SSID
#define WIFI_PASSWORD "..."           // AP password
#define JETSON_IP     "10.x.x.x"     // boot-time fallback only; beacon overrides
#define SHUTTLE_ID    1U              // 1-based integer (range 1..6 on the 3-Jetson dev rig)
```

### 2. `STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c`

Firmware sensor and state-machine constants (in the USER CODE guards).
Rebuild and reflash after changes:

```c
#define MOVEMENT_THRESHOLD_G2   0.05f   // g² — raise if false MOVING triggers
#define MOVEMENT_DWELL_MS       500U    // ms — continuous-above duration to enter MOVING
#define MOVEMENT_DEBOUNCE_MS    300U    // ms — tolerance for sub-threshold dips during dwell
#define NO_MOVEMENT_TIMEOUT_MS  20000U  // ms — no-above-threshold duration to exit MOVING
#define SAMPLE_PERIOD_IDLE_MS   100U    // 10 Hz internal sampling in IDLE
#define SAMPLE_PERIOD_MOVING_MS 100U    // 10 Hz sampling + transmit in MOVING
#define TX_PERIOD_IDLE_MS       10000U  // 0.1 Hz UDP transmit in IDLE
#define ENV_READ_PERIOD_MS      500U    // 2 Hz HTS221 cache refresh
```

### 3. `client/.env`

Gateway runtime tuning — takes effect on container restart, no image rebuild:

```bash
# Per-shuttle buffer pressure (in packets). At 10 Hz MOVING: SOFT=3000 ≈ 5 min,
# HARD=4500 ≈ 7.5 min. Gateway-wide ceiling is a safety valve across all shuttles.
SHUTTLE_SOFT_LIMIT=3000
SHUTTLE_HARD_LIMIT=4500
GATEWAY_HARD_LIMIT=100000

# Mission-end flush: state==IDLE seconds after a MOVING run that triggers Parquet.
MISSION_END_IDLE_S=30

# NTP drift correction — lower value = more frequent re-anchoring per shuttle.
NTP_REFRESH_INTERVAL=100

# Beacon broadcast
GATEWAY_IP=               # auto-detected if unset
BEACON_INTERVAL_S=10
BEACON_PORT=5000
SHUTTLE_GROUP=1,2         # multi-Jetson pairing (see DEPLOYMENT_3JETSON.md)

# TEST_MODE=1             # write to ./ram_buffer (laptop testing, no containers)
```

### 4. FL round parameters — `server/server.py` + shell env

`server.py` reads tuning knobs from the shell environment, so you can
override without editing source. Set them before `flwr run .`:

```bash
export FL_NUM_ROUNDS=5          # default 3
export FL_MIN_FIT_CLIENTS=2     # default 1 — raise for multi-gateway
flwr run . pludos-network
```

Alternatively, edit the defaults at the top of `server/server.py`:

```python
NUM_ROUNDS      = int(os.getenv("FL_NUM_ROUNDS",      "3"))
MIN_FIT_CLIENTS = int(os.getenv("FL_MIN_FIT_CLIENTS", "1"))
```

XGBoost complexity and AlumetProfiler poll rate live in `client/client.py`:

```python
# client/client.py:227 — model complexity (trees per gateway per round)
model = xgb.XGBClassifier(n_estimators=10, tree_method='hist', device=DEVICE)

# client/client.py:155 — AlumetProfiler 10 Hz poll (decrease for higher resolution)
time.sleep(0.1)
```

---

## Simulator: No Hardware Required

Test the full gateway stack on a laptop without any STM32 hardware:

```bash
# Terminal 1 — start data-engine locally (no container, writes to ./ram_buffer)
cd client
TEST_MODE=1 python3 data-engine.py
# Add SHUTTLE_GROUP=1,2 to exercise the multi-Jetson pairing filter.

# Terminal 2 — emit 28-byte PludosTelemetry to 127.0.0.1:5683
cd ..
python3 tools/mock_stm32.py
# Env overrides: TELEMETRY_HOST, TELEMETRY_PORT, MOCK_SHUTTLES, FIRST_SHUTTLE_ID,
#                MISSION_S, IDLE_S, POST_MISSION_IDLE_S
# Stress test (6 shuttles in one process):
#   MOCK_SHUTTLES=6 python3 tools/mock_stm32.py
```

Each shuttle cycles `IDLE → MOVING (30 s) → IDLE (35 s)` and the long IDLE
phase triggers a mission-end flush — a `.parquet` lands in `./ram_buffer/`
after roughly the first 70 s. Pass `TEST_MODE=1` to `client.py` as well to
run XGBoost on CPU and use mock power values in the `AlumetProfiler`.

---

## Quick-Reference Commands

```bash
# --- STM32 ---
cd STM_Shuttles/PLUDOS_Edge_Node/Debug && make clean && make -j4   # CLI build
# Flash via STM32CubeIDE Run → Run

# --- Jetson ---
cd ~/PLUDOS/client
podman-compose up --build data-engine          # first run (builds image)
podman-compose up -d data-engine               # background start
podman-compose --profile vpn up -d            # with AI worker + Tailscale
podman logs -f pludos-data-engine             # live UDP ingestion logs
podman logs -f pludos-ai-worker               # FL round logs
podman exec pludos-data-engine ls /app/ram_buffer/  # list Parquet files

# --- Server ---
cd ~/PLUDOS/server && podman-compose up -d    # start InfluxDB + Grafana
cd ~/PLUDOS && flwr run .                     # start FL round (3 rounds)
# InfluxDB UI:  http://localhost:8086  (admin / adminpassword)
# Grafana UI:   http://localhost:3000  (admin / admin)

# --- Stop everything ---
cd ~/PLUDOS/client  && podman-compose down
cd ~/PLUDOS/server  && podman-compose down
```
