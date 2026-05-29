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
- **High-Granularity Telemetry:** Real-time vibration and power analysis using lightweight, event-driven transport (raw UDP, with CoAP reserved for critical messages).

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

### Layer 1 — Edge Provisioning (STM32 Shuttle)

- **Hardware:** STM32U585AII6Q on B-U585I-IOT02A (Cortex-M33 @ 160 MHz, TrustZone non-secure)
- **Runtime:** bare-metal C, HAL drivers, **no RTOS**
- **Sensor pipeline:** ISM330DHCX 6-axis IMU (accel + gyro), HTS221 temperature/humidity, LPS22HH pressure
- **State machine:** IDLE (0.1 Hz TX / 10 Hz internal sampling) / MOVING (50 Hz TX) FSM, entered on a vibration threshold
- **Transport:** 24-byte `PludosTelemetry` raw UDP to port 5683, fire-and-forget (ADR-015 / ADR-016)
- **Zero-touch provisioning:** auto-discovers the gateway IP via a UDP beacon on port 5000

### Layer 2 — Edge Gateway (Jetson Orin Nano)

- **`data-engine`:** asyncio UDP server — receives, NTP-anchors timestamps, buffers per-shuttle, computes derived columns (vibration stats, ZUPT distance, mission/phase segmentation), and flushes to Parquet on mission-end or buffer pressure
- **`ai-worker`:** Flower client — loads Parquet, generates anomaly labels (IsolationForest or 1D-CNN autoencoder, selectable), trains XGBoost (**CPU by default**; auto-detects GPU and falls back to CPU), streams per-phase energy to InfluxDB
- **`alumet-relay`:** sidecar container — reads INA3221 hardware power rails via Alumet (ADR-011 Phase 2); runs in all profiles, with a healthcheck that gates `ai-worker`
- **Buffer policy:** per-shuttle 1000-packet soft limit / 1500-packet hard limit, 50 000-packet gateway ceiling (multi-shuttle aware)
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
| `stm_mission` | Gateway-derived (state × `POWER_IDLE/MOVING_MW` × elapsed_s) | Per shuttle, per mission |
| `fl_energy` | Jetson INA3221 via Alumet relay (tegrastats fallback) | ~10 Hz during FL rounds |
| `fl_phases` | Derived from `fl_energy` (load / train / round_total) | Per FL phase |

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
pip install -e .

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

## 🛠️ Tech Stack

| Component | Technology |
|---|---|
| Firmware | C / STM32CubeIDE / HAL, bare-metal (no RTOS) |
| Transport | raw UDP (telemetry), CoAP RFC 7252 (`aiocoap`, critical messages) |
| Edge runtime | Python async/await, Podman containers |
| AI/ML | Flower (federated learning), XGBoost, IsolationForest / 1D-CNN autoencoder |
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
├── build_pludos_dashboard.py        # Grafana dashboard generator
├── docs/                            # architecture, ADRs, wire protocol, guides
└── pyproject.toml                   # Flower app + dependencies
```
