# PLUDOS Central Analytics

This document covers the InfluxDB + Grafana monitoring stack used to profile
energy consumption across the federated learning pipeline.

The stack is defined in `server/compose.yaml` and started with `podman-compose`.

---

## 1. Starting the Analytics Stack

```bash
cd server
cp .env.example .env      # first time only — fill in real credentials
podman-compose up -d
```

| Service | Access URL | Credentials (from server/.env) |
|---|---|---|
| **Grafana** | `http://localhost:3000` | `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` |
| **InfluxDB** | `http://localhost:8086` | `INFLUXDB_ADMIN_USER` / `INFLUXDB_ADMIN_PASSWORD` |
| **Alumet** | (internal only) | writes to InfluxDB on startup |

---

## 2. Connecting Grafana to InfluxDB

1. `Connections → Data Sources → Add data source → InfluxDB`
2. Configure exactly:
   - **Query Language**: Flux
   - **URL**: `http://influxdb:8086` — internal Podman bridge name; do NOT use localhost
   - **Organization**: `pludos`
   - **Token**: value of `INFLUXDB_ADMIN_TOKEN` from `server/.env`
   - **Default Bucket**: `alumet_energy`
3. `Save & Test` → expect `"datasource is working. 1 buckets found"`

---

## 3. InfluxDB Schema

Three measurements across the pipeline. All share the same bucket (`alumet_energy`) so Grafana
can query across all of them in one data source.

### `fl_energy` — 10 Hz power samples during FL rounds

| Field | InfluxDB concept | Value |
|---|---|---|
| `_measurement` | measurement name | `fl_energy` (fixed) |
| `device` | tag | Jetson hostname or `server` |
| `fl_round` | tag | Flower round number (`"1"`, `"2"`, …) as string |
| `nvpmodel` | tag | NVPModel mode at profiler init (Jetson only; `"N/A"` on server) |
| `power_gpu_w` | field | GPU rail watts (Jetson: VDD_GPU; server: 0.0 if no GPU) |
| `power_cpu_w` | field | CPU rail watts (Jetson: VDD_CPU; server: RAPL package) |
| `power_total_w` | field | total system watts |
| `energy_j` | field | cumulative joules integrated as power × Δt since round start |

### `fl_phases` — per-phase energy summary (one point per phase per round)

| Field | InfluxDB concept | Value |
|---|---|---|
| `_measurement` | measurement name | `fl_phases` |
| `device` | tag | Jetson hostname |
| `fl_round` | tag | Flower round number as string |
| `phase` | tag | `load` / `train` / `round_total` |
| `nvpmodel` | tag | NVPModel mode |
| `duration_ms` | field | phase wall-clock duration |
| `energy_j` | field | joules consumed during this phase |
| `avg_power_w` | field | mean power = energy_j / duration_s |

### `stm_mission` — per-shuttle mission summary (one point per mission end)

| Field | InfluxDB concept | Value |
|---|---|---|
| `_measurement` | measurement name | `stm_mission` |
| `shuttle_id` | tag | STM32 shuttle identifier |
| `gateway` | tag | Jetson hostname |
| `packets` | field | total packets received in this mission |
| `duration_ms` | field | mission wall-clock duration on gateway |

Note: shuttle-side `energy_j` was removed in schema v4 (it was a hardcoded
`power_mw × elapsed` estimate). Real energy is Jetson/server-side only — see
`fl_energy` / `fl_phases` above.

---

## 4. Grafana Dashboard Queries

### 4.1 GPU + CPU power per FL round (primary training graph)

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

Group by `fl_round` and `device` tags in Grafana to get per-round, per-device curves.

### 4.2 Cumulative energy per round

```flux
from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "fl_energy")
  |> filter(fn: (r) => r["_field"] == "energy_j")
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "energy_j")
```

### 4.3 Server vs Jetson power (multi-device comparison)

```flux
from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "fl_energy")
  |> filter(fn: (r) => r["_field"] == "power_total_w")
  |> group(columns: ["device"])
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
```

### 4.4 Per-phase energy breakdown (fl_phases)

One point per phase per round. Use a bar chart panel, group by `phase` tag.

```flux
from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "fl_phases")
  |> filter(fn: (r) => r["_field"] == "energy_j")
  |> group(columns: ["phase", "fl_round", "device"])
  |> yield(name: "phase_energy")
```

### 4.5 Per-shuttle mission summary (stm_mission)

One point per mission end. Use a table panel or time series grouped by `shuttle_id`.

```flux
from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "stm_mission")
  |> filter(fn: (r) => r["_field"] == "packets" or r["_field"] == "duration_ms")
  |> group(columns: ["shuttle_id", "gateway"])
  |> yield(name: "mission_summary")
```

---

## 5. Visualization Tips

- **Time range**: FL training rounds complete in under 5 seconds. Set Grafana
  to **Last 5 minutes** or you will miss the spikes entirely.
- **TEST_MODE=1**: on a laptop without a Jetson, `AlumetProfiler` writes
  randomised mock values. InfluxDB points still flow; use them to verify
  the dashboard layout before deploying to hardware.
- **NVPModel tag**: every Jetson InfluxDB point carries `nvpmodel`. Filter
  by it to compare results across power modes. Benchmark data without this
  tag is not reproducible.

---

## 6. Server Alumet (ADR-011)

The `alumet` service in `server/compose.yaml` runs alongside InfluxDB and
Grafana. It measures server CPU energy via Intel RAPL and writes to the
same `fl_energy` measurement, tagged `device=server`.

**Phase 2 (ADR-011 open):** when the Jetson relay sidecar is deployed,
the server Alumet instance will also receive and aggregate Jetson energy
streams, giving a unified energy view across all devices in one InfluxDB
bucket without needing the Jetson containers to write to InfluxDB directly.
