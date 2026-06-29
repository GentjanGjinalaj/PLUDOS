# PLUDOS: Modular Framework for Industrial Frugal Data Collection & Edge AI

**Industrial-Academic Collaboration:** [Savoye SASU](https://www.savoye.com/) & PhD Research [[theses.fr/s410359](https://theses.fr/s410359)]

PLUDOS (**P**ower-aware **L**ightweight **U**DP **D**ata **O**rchestration **S**ystem) is a modular framework for **frugal data collection** and **Energy-Aware Federated Learning** at the extreme edge. Developed for large-scale industrial logistics, it studies the energy-accuracy trade-off in warehouse automation — specifically predictive-maintenance monitoring of Savoye XTPS warehouse shuttles.

> ### ⚖️ Intellectual Property Notice
> **Copyright © 2026 Gentjan Gjinalaj & Savoye SASU. All Rights Reserved.**
>
> This repository contains proprietary research and industrial code. Unauthorized copying, modification, distribution, or use is strictly prohibited. Access is provided for review and academic validation within the context of the doctoral thesis.

---

## 🎯 Core Research Objectives

- **Computational Frugality:** Minimizing the energy footprint of the monitoring system itself, so the act of monitoring is as close to "free" as possible.
- **Modular Scalability:** Rapid deployment across diverse industrial hardware, from ultra-low-power microcontrollers to edge AI accelerators.
- **High-Granularity Telemetry:** High-rate vibration capture (accel 3332 Hz / gyro 416 Hz) buffered on-shuttle, then drained over raw UDP when a run ends — the radio stays off during motion to save energy (ADR-021).

---

## 🏗️ System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         PLUDOS Three-Tier Architecture                         │
│                                                                                │
│  ┌─────────────────────┐  raw UDP 5683        ┌──────────────────────────────┐ │
│  │  STM32U585 Shuttle  │ ────────────────────► │  Jetson Orin Nano Gateway    │ │
│  │                     │  (24-byte             │                              │ │
│  │  • ISM330DHCX IMU   │   PludosTelemetry)    │  data-engine  ──► Parquet    │ │
│  │  • 6-axis accel+gyro│                       │  ai-worker    ──► XGBoost    │ │
│  │  • HTS221 + LPS22HH │ ◄──────────────────── │  alumet-relay ──► InfluxDB   │ │
│  │  • bare-metal FSM   │  beacon UDP (5000)    │                              │ │
│  └─────────────────────┘                       └──────────┬───────────────────┘ │
│                                                           │ Tailscale VPN        │
│                                                           ▼                      │
│                                                ┌──────────────────────────────┐ │
│                                                │  Central Server              │ │
│                                                │                              │ │
│                                                │  Flower ServerApp            │ │
│                                                │  XGBoost tree-set union      │ │
│                                                │  fl-trigger (auto-launch)    │ │
│                                                │  InfluxDB 2.7 + Grafana      │ │
│                                                └──────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

> **Edge data path (ADR-021).** The primary shuttle→gateway link is the
> **high-rate capture drain on UDP :5684**, not the live `:5683` stream (now
> dormant — the radio is off during motion). Before blasting a multi-MB drain,
> the shuttle waits for an **8-byte `DrainAck`** the gateway echoes on :5684 as
> delivery evidence. See [`docs/MODULARITY_AND_PIPELINE.md`](docs/MODULARITY_AND_PIPELINE.md)
> for the full STM↔Jetson contract (sampling, timestamps, drain protocol, schema).

### Layer 1 — Edge Provisioning (STM32 Shuttle)

- **Hardware:** STM32U585AII6Q on B-U585I-IOT02A (Cortex-M33 @ 160 MHz, TrustZone non-secure)
- **Runtime:** bare-metal C, HAL drivers, **no RTOS**
- **Sensor pipeline:** ISM330DHCX 6-axis IMU (accel + gyro), HTS221 temperature/humidity, LPS22HH pressure
- **State machine:** IDLE / MOVING FSM, entered on a vibration threshold. A 50 Hz internal poll only *detects* motion; it is not a data rate.
- **Capture-and-drain (ADR-021):** during a run the IMU streams into a PSRAM ring buffer (accel 3332 Hz / gyro 416 Hz ≈ 8:1); IDLE takes a short 12.5 Hz snapshot every 10 min. The radio is **off** during motion and powers on only to drain the finished capture over UDP to port 5684. A 1–15 s pre-TX jitter decorrelates shuttles that stop together. The shuttle blasts a drain only after the gateway echoes an 8-byte `DrainAck` (delivery evidence — not ARQ); on silence it skips rather than waste radio energy.
- **Zero-touch provisioning:** auto-discovers the gateway IP via a UDP beacon on port 5000
- **Over-the-air firmware update (ADR-019):** the shuttle self-updates over WiFi — it pulls a new image from the gateway (UDP 5685, NAK selective-repeat ARQ), stages it in PSRAM, runs a whole-image CRC32 gate, then **flashes its own inactive flash bank**, swaps banks (`SWAP_BANK`) and reboots. A trial image must self-confirm or it auto-reverts (anti-brick). No ST-Link, no host push. See [`docs/firmware_update.md`](docs/firmware_update.md).

### Layer 2 — Edge Gateway (Jetson Orin Nano)

- **`data-engine`:** asyncio UDP server — receives the live stream and the high-rate drain (port 5684), anchors timestamps, buffers per-shuttle, and flushes to Parquet on mission-end or buffer pressure. It is a **raw-only collector**: Parquet holds only non-recomputable signal (raw accel/gyro/temp/humidity, the state flag, a UTC timestamp, a packet-loss counter). All feature engineering (magnitudes, jerk, tilt, rolling windows, segmentation) is deferred to train-time in the anomaly module
- **`ai-worker`:** Flower client — loads Parquet, generates anomaly labels (**1D-CNN autoencoder by default**, falls back to IsolationForest when torch is missing or there are too few MOVING samples), trains XGBoost (**CPU by default**; auto-detects GPU and falls back to CPU), streams per-phase energy to InfluxDB
- **`alumet-relay`:** sidecar container — reads INA3221 hardware power rails via Alumet (ADR-011 Phase 2); runs in all profiles, with a healthcheck that gates `ai-worker`
- **Buffer policy:** per-shuttle 3000-packet soft limit / 4500-packet hard limit, 100 000-packet gateway ceiling (multi-shuttle aware)
- **Deployment profiles** (`PLUDOS_MODE`): `federated` (joins the central server over Tailscale), `standalone` (local InfluxDB + Grafana, no server), `headless` (ingest only)
- **Storage:** host bind-mount `ram_buffer/` for low-wear Parquet buffering; PyArrow columnar serialization

### Layer 3 — Central Server

- **`server.py`:** Flower `ServerApp`, launched by `flwr run .`
- **`fl-trigger`:** watches InfluxDB for ready gateways and launches FL rounds autonomously — no manual operator step
- **Aggregation:** horizontal tree-set union (ADR-010 Option A) — concatenates XGBoost booster trees from all gateways into a single validated global model; single-client rounds pass through
- **Defaults:** 10 rounds (`FL_NUM_ROUNDS`), `min_fit_clients=1` for dev
- **Monitoring:** InfluxDB 2.7 stores `fl_energy`, `fl_phases`, `stm_mission`, `gw_status`
- **Dashboards:** Grafana, provisioned as code from `server/grafana/`
- **Energy:** server-side Alumet (RAPL) + gRPC relay receiver for the Jetson power streams (ADR-011)

---

## ⚡ Energy Monitoring Stack

PLUDOS instruments energy at several levels:

| Measurement | Source | Granularity |
|---|---|---|
| `fl_energy` | Jetson INA3221 via Alumet relay (tegrastats fallback) | ~10 Hz during FL rounds |
| `fl_phases` | Derived from `fl_energy` (load / train / round_total) | Per FL phase |
| `stm_mission` | Gateway activity summary (packet count + duration) | Per shuttle, per mission |

> **Note:** instrument-grade energy is measured only on the Jetson/server (Alumet).
> The shuttle-side `POWER_*_MW` estimate was removed in the schema-v4 raw-only cull;
> `stm_mission` now records activity metadata (packets, duration), not energy. Add an
> STM32 INA219 before claiming shuttle-side energy figures.

---

## 🚀 Quickstart (Simulation Mode — no hardware required)

Simulation mode runs the full Flower federation (server + one virtual client) in a single process on your laptop, reading Parquet files from disk.

**Prerequisites:** Python 3.10+, a virtual environment

```bash
# 1. Clone and set up
git clone <repo-url>
cd PLUDOS
python -m venv pludos_venv
source pludos_venv/bin/activate   # Windows: pludos_venv\Scripts\activate
pip install -e .          # add ".[cnn]" to run the CNN-autoencoder default in sim
                          # (needs torch; otherwise sim falls back to IsolationForest)

# 2. Generate test data — emits 24-byte UDP PludosTelemetry to a local data-engine
python tools/mock_stm32.py        # MOCK_SHUTTLES=6 for a multi-shuttle run

# 3. Run a federated learning simulation
TEST_MODE=1 flwr run .
```

Each round logs `[ALUMET]` energy samples and a round summary. InfluxDB writes fail gracefully when no server is running — expected in simulation mode.

**Spin up the monitoring stack (optional):**

```bash
cd server
podman-compose up -d     # InfluxDB :8086, Grafana :3000, Alumet, fl-trigger
cd ..
TEST_MODE=1 INFLUXDB_URL=http://127.0.0.1:8086 flwr run .
# then open http://localhost:3000  (default admin/admin — change on first login)
```

For real multi-Jetson deployment over Tailscale, see `docs/DEPLOYMENT_GUIDE.md`
and the federation notes in `pyproject.toml`.

---

## 🧩 Modularity & Deployment Modes

PLUDOS is built so each tier can run **independently**. You do **not** need a
central server to collect data or detect anomalies — a single Jetson is a
complete monitoring node on its own. One `compose.yaml` serves all three modes
via the `PLUDOS_MODE` env var:

| Mode | Server needed? | What runs | Use it when |
|---|---|---|---|
| `headless` | No | `data-engine` + `alumet-relay` only | You just want to **collect** raw Parquet — no AI, no dashboards |
| `standalone` | **No** | adds `ai-worker` + **local** InfluxDB + Grafana | One self-contained Jetson: ingests, retrains XGBoost locally every `STANDALONE_RETRAIN_INTERVAL_S`, and shows its own Grafana — fully offline |
| `federated` | Yes | adds Tailscale + joins the central Flower server | Multiple gateways pool their models via federated learning |

**Run a single Jetson with no server (standalone):**

```bash
cd client
cp .env.example .env          # set JETSON_HOSTNAME + SHUTTLE_GROUP
PLUDOS_MODE=standalone podman-compose --profile standalone up -d
# Grafana → http://<jetson-ip>:3000   (local InfluxDB, no central server)
# warehouse Jetson: http://100.119.83.35:3000 (Tailscale, admin/admin) or http://192.168.0.100:3000 (LAN)
```

In standalone mode `ai-worker` writes the latest model to
`client/ram_buffer/model/latest.ubj`. Switching a node to `federated` later is a
profile change only — the data already on disk is reused. Each tier talks to the
next **only through files / UDP**, never direct calls, so any tier can be
swapped, restarted, or run alone without breaking the others.

---

## 🩹 Troubleshooting (if X → do Y)

| Symptom | Likely cause | Fix |
|---|---|---|
| STM UART shows no `[BEACON] Gateway found` | `data-engine` not running, or STM not on the gateway's WiFi subnet | Start `data-engine` (`podman-compose up -d data-engine`); confirm both on the same 2.4 GHz network |
| STM logs `Ignored beacon (different group)` | The STM's `SHUTTLE_ID` is not in that Jetson's `SHUTTLE_GROUP` | Expected in multi-Jetson rigs — only the paired Jetson should bond. Fix `SHUTTLE_GROUP` in `.env` if pairing is wrong |
| No Parquet files in `ram_buffer/` | No mission completed yet (needs ≥ 30 s IDLE after a MOVING run), or wrong `TEST_MODE` | Wait for a full mission cycle; set `TEST_MODE=1` for local `./ram_buffer`, `0` inside the container |
| Grafana shows "No data" | Stale container missing provisioning bind-mounts, or wrong `INFLUXDB_URL` | Recreate the stack (`podman compose down && up -d`); verify `INFLUXDB_URL`/`INFLUXDB_TOKEN` match between `client/.env` and `server/.env` |
| FL round never starts | Server waiting for `FL_MIN_FIT_CLIENTS` gateways to connect | Lower `FL_MIN_FIT_CLIENTS`, or bring the missing Jetsons online (`--profile vpn`) |
| `ai-worker` falls back to IsolationForest | torch missing, or fewer than `CNN_MIN_MOVING_SAMPLES` (200) MOVING samples | Expected fallback — collect more MOVING data, or confirm torch is in the image |
| CNN feature stats reset on restart | `/app/state` not persisted (Welford running stats lost) | Bind-mount or named-volume `STATE_DIR=/app/state` so stats survive container restarts |
| InfluxDB write errors in simulation | No server running locally | Harmless — writes fail gracefully in sim mode; ignore, or start `server/` compose |

**Common deploy loop (Jetson):**

```bash
ssh <jetson> "cd ~/PLUDOS && git pull && cd client && podman-compose up --build -d data-engine"
ssh <jetson> "podman logs -f pludos-data-engine | head -20"
```

---

## 🛠️ Tech Stack

| Component | Technology |
|---|---|
| Firmware | C / STM32CubeIDE / HAL, bare-metal (no RTOS) |
| Transport | raw UDP — high-rate capture drain (:5684, primary) with 8-byte `DrainAck` delivery echo + dormant live stream (:5683); no CoAP (ADR-015 / ADR-021) |
| Edge runtime | Python async/await, Podman containers |
| AI/ML | Flower (federated learning), XGBoost (federated model), 1D-CNN autoencoder labeller (default) / IsolationForest (fallback) |
| Energy monitoring | Alumet (UGA/LIG), INA3221 / RAPL / tegrastats, Prometheus |
| Storage | Apache Parquet (PyArrow), InfluxDB 2.7, Grafana |
| Networking | Tailscale VPN overlay (gateway ↔ server) |

---

## 📁 Repository Layout

Each major folder has an `OVERVIEW.md` explaining its files in plain language —
start there if you're new. The root `OVERVIEW.md` maps the whole tree.

```
PLUDOS/
├── STM_Shuttles/PLUDOS_Edge_Node/   # Tier 1 — STM32U585 firmware (CubeMX project)
│   ├── Core/Src/main.c              #   FSM, IMU read, telemetry packer, beacon, UDP TX
│   ├── Core/Src/sensors.c           #   HTS221 + LPS22HH I²C drivers
│   └── tools/coap_udp_monitor.py    #   laptop packet listener
├── client/                          # Tier 2 — Jetson gateway
│   ├── data-engine.py               #   UDP receiver, Parquet writer, beacon
│   ├── client.py                    #   Flower client + AlumetProfiler
│   ├── anomaly.py / anomaly_cnn.py  #   anomaly label generators (IF / CNN)
│   ├── alumet-relay/                #   INA3221 power sidecar
│   └── compose.yaml                 #   Podman compose (federated/standalone/headless)
├── server/                          # Tier 3 — central server
│   ├── server.py                    #   Flower ServerApp + XGBoost aggregation
│   ├── trigger/                     #   fl-trigger autonomous FL launcher
│   ├── alumet/                      #   server-side Alumet (RAPL + relay receiver)
│   ├── grafana/                     #   dashboards-as-code
│   ├── systemd/                     #   boot autostart unit
│   └── compose.yaml                 #   InfluxDB + Grafana + Alumet + fl-trigger
├── scripts/                         # calibration tools (constants from real data)
│   └── experiments/                 #   thesis-validation analysers (T8 series)
├── tools/mock_stm32.py              # fake shuttle fleet (no-hardware testing)
├── docs/                            # architecture, ADRs, wire protocol, guides
└── pyproject.toml                   # Flower app + dependencies
```
