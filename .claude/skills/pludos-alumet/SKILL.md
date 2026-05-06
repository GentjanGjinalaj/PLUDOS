---
name: pludos-alumet
description: Guides integration of the Alumet energy measurement framework (developed by UGA/LIG) into the PLUDOS system. Use whenever the user asks about real energy measurement on the Jetson, replacing the AlumetProfiler placeholder, reading INA3221 or tegrastats power data, implementing the Alumet relay concept, or any work related to ADR-011 in docs/decisions.md. Also use when the user mentions Alumet, energy sensing, power monitoring, InfluxDB energy writes, phase-level energy breakdown, or per-shuttle energy measurement.
---

# PLUDOS Alumet Integration Skill

Alumet is an open-source energy measurement framework by LIG (Laboratoire
d'Informatique de Grenoble / UGA) — the academic partner in the PLUDOS CIFRE
PhD project. Plugin-based, architecture-agnostic, with native NVIDIA Jetson
INA3221 support.

**Docs:** https://alumet-dev.github.io/user-book/intro.html
**Source:** https://github.com/alumet-dev/alumet

---

## Current implementation status

| Component | State |
|---|---|
| Jetson tegrastats → `fl_energy` InfluxDB | **Active** — Phase 1, running |
| Phase-level breakdown → `fl_phases` InfluxDB | **Active** — load/train/round_total |
| Per-shuttle mission energy → `stm_mission` InfluxDB | **Active** — written at mission-end |
| Server RAPL → `fl_energy` InfluxDB | **Built** — `server/alumet/Containerfile` |
| Jetson INA3221 via alumet-relay → server gRPC relay | **Scaffolded** — flags confirmed, hardware build pending |
| Server Prometheus live scrape (port 9091) | **Added** — needs hardware flag verification |
| `probe.py` INA3221 Python fallback | **Dormant** — code commented out, kept as reference |

---

## Architecture

```
STM32 Shuttle(s)              Jetson Orin Nano                     Central Server
┌─────────────┐              ┌──────────────────────────────┐      ┌─────────────────────────────┐
│ power_mw    │──CoAP/UDP──► │ data-engine.py               │      │ alumet container            │
│ (estimated) │              │  stm_mission → InfluxDB      │      │  --plugin rapl              │
└─────────────┘              │                              │      │  --relay-in 0.0.0.0:50051   │
                             │ client.py (AlumetProfiler)   │      │  --output influxdb          │
                             │  tegrastats (active)         │─────►│  --output prometheus :9091  │
                             │  relay file (dormant hook)   │      │                             │
                             │  fl_energy  → InfluxDB       │      │ InfluxDB + Grafana          │
                             │  fl_phases  → InfluxDB       │      │  fl_energy  (all devices)   │
                             │                              │      │  fl_phases  (per Jetson)     │
                             │ alumet-relay container       │      │  stm_mission (per shuttle)  │
                             │  --plugin jetson             │─────►│                             │
                             │  --relay-out server:50051    │ gRPC └─────────────────────────────┘
                             └──────────────────────────────┘
```

---

## Confirmed relay CLI flags (from Alumet docs)

```bash
# Jetson alumet-relay container (client/alumet-relay/entrypoint.sh)
alumet-cli \
    --plugin jetson \                    # native INA3221 reader
    --relay-out <server-ip>:50051 \      # forward stream to server Alumet
    --tag "device=$(hostname)"

# Local mode (no ALUMET_SERVER_ADDR set) — writes directly to InfluxDB
alumet-cli \
    --plugin jetson \
    --output influxdb \
    --influxdb-url $INFLUXDB_URL \
    --influxdb-token $INFLUXDB_TOKEN \
    --influxdb-org $INFLUXDB_ORG \
    --influxdb-bucket $INFLUXDB_BUCKET \
    --tag "device=$(hostname)"

# Server alumet container (server/alumet/Containerfile CMD)
alumet-cli \
    --plugin rapl \
    --relay-in 0.0.0.0:50051 \
    --output influxdb \
    --influxdb-url $INFLUXDB_URL \
    --influxdb-token $INFLUXDB_TOKEN \
    --influxdb-org $INFLUXDB_ORG \
    --influxdb-bucket $INFLUXDB_BUCKET \
    --output prometheus \                # live scrape on port 9091
    --tag device=$ALUMET_DEVICE_TAG
```

**Still need hardware verification (run `alumet-cli --help` after first Jetson build):**
- `--plugin jetson` vs `--plugin nvidia-jetson`
- `--output influxdb` and `--influxdb-*` flag names
- `--output prometheus` flag name and default port

---

## InfluxDB schema — three measurements

### `fl_energy` — 10 Hz continuous Jetson power samples
Written by `AlumetProfiler._poll_metrics()` in [client/client.py](client/client.py)

```
Measurement : fl_energy
Tags        : device (Jetson hostname), fl_round, nvpmodel
Fields      : power_gpu_w, power_cpu_w, power_total_w, energy_j (cumulative since round start)
Timestamp   : nanosecond precision
```

### `fl_phases` — per-phase energy breakdown per FL round
Written by `AlumetProfiler.end_phase()` in [client/client.py](client/client.py)

```
Measurement : fl_phases
Tags        : device, fl_round, phase (load | train | round_total), nvpmodel
Fields      : duration_ms, energy_j (delta for this phase only), avg_power_w
Timestamp   : nanosecond precision (at phase end)
```

| Phase | What it covers | Typical duration |
|---|---|---|
| `load` | `load_buffered_data()` — Parquet read from tmpfs | < 1 s |
| `train` | `model.fit()` — GPU-intensive XGBoost | seconds–minutes |
| `round_total` | Entire `fit()` call including overhead | covers load + train |

**Key thesis metric:** `train.energy_j / round_total.energy_j` = fraction of round energy spent on actual learning.

### `stm_mission` — per-shuttle, per-mission energy summary
Written by `_write_mission_summary()` in [client/data-engine.py](client/data-engine.py) at mission-end

```
Measurement : stm_mission
Tags        : shuttle_id, gateway (Jetson hostname)
Fields      : energy_j (integrated from STM32 power_mw field), packets, duration_ms
Timestamp   : nanosecond precision (at mission end)
```

Note: `stm_mission.energy_j` is the STM32's own estimated power consumption during the
mission — different from `fl_energy` which is the Jetson's energy. Both are needed.

---

## Grafana query examples (Flux)

### Training energy per FL round across all Jetsons
```flux
from(bucket: "alumet_energy")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "fl_phases" and r.phase == "train")
  |> filter(fn: (r) => r._field == "energy_j")
  |> group(columns: ["fl_round", "device"])
  |> sum()
```

### Phase breakdown for one round on one Jetson
```flux
from(bucket: "alumet_energy")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "fl_phases")
  |> filter(fn: (r) => r.fl_round == "2" and r.device == "jetson-warehouse1")
  |> filter(fn: (r) => r._field == "energy_j")
  |> group(columns: ["phase"])
```

### Per-shuttle mission energy (all shuttles, last 24 h)
```flux
from(bucket: "alumet_energy")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "stm_mission")
  |> filter(fn: (r) => r._field == "energy_j")
  |> group(columns: ["shuttle_id", "gateway"])
  |> sum()
```

### Live 10 Hz Jetson power (current round)
```flux
from(bucket: "alumet_energy")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "fl_energy")
  |> filter(fn: (r) => r._field == "power_total_w")
  |> group(columns: ["device"])
```

### Prometheus (once relay active) — add as Grafana data source
URL: `http://<server-ip>:9091`
Then query: `alumet_power_total_w` (exact metric name needs hardware verification)

---

## Jetson power sensor access

### Via alumet-cli native plugin (primary — Phase 2)
```bash
# Reads INA3221 at native rate; --relay-out or --output influxdb
alumet-cli --plugin jetson --relay-out <server:port> --tag "device=$(hostname)"
```

### Via tegrastats (active fallback in AlumetProfiler — Phase 1)
```bash
tegrastats --interval 100 --count 1
# Parses VDD_GPU, VDD_CPU, VDD_SOC rails
```

### Via sysfs directly (dormant emergency fallback — probe.py)
```bash
# Verify INA3221 paths on physical Jetson (not inside container):
ls /sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power*_label

# Read a channel (milliwatts):
cat /sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power0_input
```
See [client/alumet-relay/probe.py](client/alumet-relay/probe.py) for the full sysfs
reader and channel classification logic (VDD_GPU_SOC → power_gpu_w, VDD_CPU_CV → power_cpu_w).

---

## AlumetProfiler API (client/client.py)

```python
profiler = AlumetProfiler(round_num)
profiler.start()                   # begins 10 Hz sampling thread

profiler.begin_phase("load")       # snapshot: time + energy_j
X, y = load_buffered_data()
profiler.end_phase("load")         # writes fl_phases point with delta

profiler.begin_phase("train")
model.fit(X, y)
profiler.end_phase("train")

profiler.end_phase("round_total")
profiler.stop()                    # joins thread, closes InfluxDB client
```

To add a new phase (e.g. `transmit`): call `begin_phase("transmit")` before the
operation and `end_phase("transmit")` after. No other changes needed — `fl_phases`
will automatically contain the new phase tag.

---

## Hardware verification checklist (one-time, on physical Jetson)

```bash
# 1. Build relay image (~15 min first time, layers cached after)
cd ~/PLUDOS/client
podman build -f alumet-relay/Containerfile alumet-relay/ -t pludos-alumet-relay

# 2. Confirm plugin and output flag names
podman run --rm pludos-alumet-relay alumet-cli --help 2>&1 | grep -E "plugin|output|relay"

# 3. Verify INA3221 sysfs paths on Jetson host
ls /sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power*_label

# 4. Check current NVPModel
sudo nvpmodel -q

# 5. Start relay stack
cd ~/PLUDOS/client
podman-compose up -d alumet-relay

# 6. Activate relay mode
echo "ALUMET_SERVER_ADDR=<server-tailscale-ip>:50051" >> .env
podman-compose up -d alumet-relay
```

---

## Anti-patterns

- **Don't cite TEST_MODE energy numbers.** `random.uniform(25, 45)` is not a measurement.
- **Don't report benchmarks without nvpmodel tag.** A training energy figure without
  the NV power mode is not reproducible. Always tag it.
- **Don't conflate `fl_energy` and `stm_mission.energy_j`.** The first is Jetson-side
  (INA3221 or tegrastats); the second is the STM32's own self-reported power estimate.
  Different devices, different rails, different accuracy.
- **Don't drop `fl_phases` from the thesis analysis.** The `train` fraction of
  `round_total` is the key efficiency claim. Without it you can only report total
  round energy, not where it was spent.
- **Don't assume `--plugin jetson` is the exact flag.** It may be `--plugin nvidia-jetson`.
  Verify with `alumet-cli --help` on first hardware build before updating entrypoint.sh.
