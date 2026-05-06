# PLUDOS: Modular Framework for Industrial Frugal Data Collection & Edge AI

**Industrial-Academic Collaboration:** [Savoye SASU](https://www.savoye.com/) & PhD Research [[theses.fr/s410359](https://theses.fr/s410359)]

PLUDOS (**P**ower-aware **L**ightweight **U**DP **D**ata **O**rchestration **S**ystem) is a modular framework for **frugal data collection** and **Energy-Aware Federated Learning (HE-AFL)** at the extreme edge. Developed for large-scale industrial logistics, it optimizes the energy-accuracy trade-off in warehouse automation environments.

> ### ⚖️ Intellectual Property Notice
> **Copyright © 2026 Gentjan Gjinalaj & Savoye SASU. All Rights Reserved.**
>
> This repository contains proprietary research and industrial code. Unauthorized copying, modification, distribution, or use is strictly prohibited. Access is provided for review and academic validation within the context of the doctoral thesis.

---

## 🎯 Core Research Objectives

- **Computational Frugality:** Minimizing the energy footprint of the monitoring system itself to ensure a "net-zero" monitoring overhead.
- **Modular Scalability:** Rapid deployment across diverse industrial hardware architectures, from ultra-low-power microcontrollers to edge AI accelerators.
- **High-Granularity Telemetry:** Real-time vibration and power analysis using lightweight, event-driven protocols (CoAP/UDP).

---

## 🏗️ System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         PLUDOS Three-Tier Architecture                        │
│                                                                              │
│  ┌─────────────────────┐  CoAP CON (5683)   ┌──────────────────────────────┐│
│  │  STM32U585 Shuttle  │ ─────────────────► │  Jetson Orin Nano Gateway    ││
│  │                     │  raw UDP (5684)     │                              ││
│  │  • INA219 power mon │ ─────────────────► │  data-engine  ──► Parquet    ││
│  │  • 3-axis accel     │                     │  ai-worker    ──► XGBoost   ││
│  │  • FreeRTOS FSM     │ ◄─────────────────  │  alumet-relay ──► InfluxDB  ││
│  │                     │  beacon UDP (5000)   │                              ││
│  └─────────────────────┘                     └──────────┬───────────────────┘│
│                                                         │ Tailscale VPN       │
│                                                         ▼                     │
│                                              ┌──────────────────────────────┐ │
│                                              │  Central Server              │ │
│                                              │                              │ │
│                                              │  Flower ServerApp            │ │
│                                              │  XGBoost tree-set union      │ │
│                                              │  InfluxDB 2.7 + Grafana      │ │
│                                              └──────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Layer 1 — Edge Provisioning (STM32 Shuttle)

- **Hardware:** STM32U585AII6Q on B-U585I-IOT02A (Cortex-M33 @ 160 MHz, TrustZone non-secure)
- **Sensor pipeline:** 3-axis accelerometer (ISM330DHCX), INA219 power monitor
- **State machine:** IDLE (2 Hz) / MOVING (50 Hz) FSM based on vibration threshold
- **Transport:** CoAP CON for critical vibration + power data; raw UDP for environmental telemetry
- **Zero-touch provisioning:** Auto-discovers gateway IP via UDP beacon on port 5000

### Layer 2 — Edge Gateway (Jetson Orin Nano)

- **`data-engine`:** asyncio CoAP server — receives, timestamps (NTP-anchored), buffers per-shuttle, flushes to Parquet on mission-end or buffer threshold
- **`ai-worker`:** Flower `ClientApp` — loads Parquet, trains XGBoost on NVIDIA Ampere GPU, streams phase-level energy telemetry to InfluxDB
- **`alumet-relay`:** Sidecar container — reads INA3221 hardware power rails via Alumet, exposes Prometheus metrics; provides real energy readings as replacement for `tegrastats`
- **Buffer policy:** 400-packet soft limit, 500-packet hard limit; per-shuttle dict (supports multi-shuttle missions)
- **Storage:** tmpfs RAM disk for zero-wear Parquet buffering; PyArrow for fast columnar serialization

### Layer 3 — Central Server

- **`flower-superlink`:** Coordinates FL rounds via Flower framework
- **Aggregation:** Horizontal tree-set union (ADR-010 Option A) — merges XGBoost booster trees from all gateways into a single global model
- **Monitoring:** InfluxDB 2.7 stores three measurements: `fl_energy` (10 Hz power samples per round), `fl_phases` (per-phase energy breakdown), `stm_mission` (per-shuttle mission energy)
- **Dashboards:** Grafana with Flux queries for round-level and mission-level energy analysis

---

## ⚡ Energy Monitoring Stack

PLUDOS instruments energy at three levels simultaneously:

| Measurement | Source | Granularity |
|---|---|---|
| `stm_mission` | STM32 INA219 (estimated via power_mw × elapsed_s) | Per shuttle, per mission |
| `fl_energy` | Jetson INA3221 via Alumet relay (tegrastats fallback) | 10 Hz during FL rounds |
| `fl_phases` | Derived from `fl_energy` accumulator | Per FL phase (load / train / round_total) |

---

## 🚀 Quickstart (Simulation Mode — no hardware required)

Simulation mode runs the full Flower federation (server + one virtual client) in a single process on your laptop. Data is loaded from Parquet files on disk.

**Prerequisites:** Python 3.11+, a virtual environment

```bash
# 1. Clone and set up
git clone <repo-url>
cd PLUDOS
python -m venv pludos_venv
source pludos_venv/bin/activate   # Windows: pludos_venv\Scripts\activate
pip install -e .

# 2. Generate test data (creates a Parquet file in ram_buffer/)
python tools/mock_stm32.py        # emits CoAP packets matching wire_protocol.md

# 3. Run a federated learning simulation (3 rounds, 1 virtual client)
TEST_MODE=1 flwr run .
```

Expected output: 3 FL rounds, each logging `[ALUMET]` energy samples and a round summary. InfluxDB writes will fail gracefully (no server running) — that is expected in simulation mode.

**Spin up the monitoring stack (optional):**

```bash
cd server
podman-compose up -d     # starts InfluxDB on :8086 and Grafana on :3000
cd ..
TEST_MODE=1 INFLUXDB_URL=http://127.0.0.1:8086 flwr run .
# then open http://localhost:3000  (admin/admin, change on first login)
```

---

## 🛠️ Tech Stack

| Component | Technology |
|---|---|
| Firmware | C / STM32CubeIDE / HAL, FreeRTOS |
| IoT Protocol | CoAP RFC 7252 (`aiocoap`), raw UDP |
| Edge runtime | Python async/await, Podman containers |
| AI/ML | Flower (federated learning), XGBoost, NVIDIA CUDA |
| Energy monitoring | Alumet (UGA/LIG), INA3221 / tegrastats, Prometheus |
| Storage | Apache Parquet (PyArrow), InfluxDB 2.7, Grafana |
| Networking | Tailscale VPN overlay (gateway ↔ server) |

---

## 📁 Repository Layout

```
PLUDOS/
├── STM_Shuttles/PLUDOS_Edge_Node/   # STM32U585 firmware (CubeMX project)
├── client/                          # Jetson gateway: data-engine, ai-worker, alumet-relay
│   ├── data-engine.py               # CoAP + UDP receiver, Parquet writer
│   ├── client.py                    # Flower ClientApp + AlumetProfiler
│   ├── alumet-relay/                # Alumet sidecar container
│   └── compose.yaml                 # Podman compose for Jetson services
├── server/                          # Central server: Flower ServerApp + monitoring
│   ├── server.py                    # XGBoost aggregation strategy
│   ├── alumet/                      # Alumet relay receiver container
│   └── compose.yaml                 # Podman compose for InfluxDB + Grafana
├── docs/                            # Architecture, wire protocol, ADRs, backlog
├── tools/                           # mock_stm32.py — test packet emitter
└── pyproject.toml                   # Flower app + server dependencies
```

---

## 📚 Reference Docs

| Doc | Purpose |
|---|---|
| `docs/architecture.md` | Three-tier system, current implementation status |
| `docs/wire_protocol.md` | Exact byte layouts, CoAP framing, retry rules |
| `docs/state_machine.md` | STM32 IDLE/MOVING FSM with all thresholds |
| `docs/decisions.md` | Architecture Decision Records (ADRs) |
| `docs/current_problems.md` | Active backlog (P0 / P1 / P2) |
| `docs/ANALYTICS.md` | InfluxDB Flux queries and Grafana setup |
