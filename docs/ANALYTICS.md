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

Several measurements across the pipeline, all in the same bucket
(`alumet_energy`) so Grafana can query across them in one data source.
For the shuttle data pipeline (no FL), the live measurements are
`stm_mission` (drain summary), `stm_idle_wave`, and the alumet power rails
(`input_current` / `input_voltage`). The `fl_*` measurements only appear
during FL rounds.

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

### `stm_mission` — per-shuttle summary (one point per drained capture)

This measurement has **two writers**. The drain path (`drain_receiver.py` →
`_write_drain_summary`) is the live one Grafana shows; the legacy live-stream
writer (`_write_mission_summary`) is dormant because the radio is off outside
drains. Distinguish them by the `source` tag.

| Field | InfluxDB concept | Value |
|---|---|---|
| `_measurement` | measurement name | `stm_mission` |
| `shuttle_id` | tag | STM32 shuttle identifier |
| `gateway` | tag | Jetson hostname |
| `source` | tag | `"drain"` on drain-path points; **absent** on legacy live points |
| `kind` | tag | `"mission"` (MOVING) or `"idle_snapshot"` (drain points only) |
| `mission_id` | field | gateway-assigned unix-ms capture id |
| `packets_total` | field | total drain chunks expected |
| `packets_received` | field | chunks received |
| `packets_lost` | field | chunks missing |
| `loss_pct` | field | `100 × lost / total` |
| `accel_samples` | field | accel samples in the capture (post idle-trim) |
| `gyro_samples` | field | gyro samples in the capture |
| `complete` | field | bool — all chunks received |
| `accel_rms_g` | field | accel-magnitude RMS (vibration intensity) |
| `accel_peak_g` | field | accel-magnitude peak |
| `gyro_peak_dps` | field | gyro-magnitude peak |
| `temp_c` | field | env stamp (idle snapshots only) |
| `pressure_hpa` | field | env stamp (idle snapshots only) |

Dashboards filter on `source == "drain"` so the dormant legacy points
(which only carry `packets` / `duration_ms`, no `source` tag) never appear.

Note: shuttle-side `energy_j` was removed in schema v4 (it was a hardcoded
`power_mw × elapsed` estimate). Real energy is Jetson/server-side only — see
`fl_energy` / `fl_phases` above and `input_current`/`input_voltage` below.

### `stm_idle_wave` — per-sample idle-snapshot waveform

Written by the drain path for `is_idle_snapshot` captures only — one point
per accel sample, timestamped off the anchored capture `t0_wall_ms` at the
snapshot ODR. Lets Grafana chart the idle vibration signature.

| Field | InfluxDB concept | Value |
|---|---|---|
| `_measurement` | measurement name | `stm_idle_wave` |
| `shuttle_id` | tag | STM32 shuttle identifier |
| `gateway` | tag | Jetson hostname |
| `ax_g` / `ay_g` / `az_g` | field | accel axes, g |
| `gx_dps` / `gy_dps` / `gz_dps` | field | gyro axes, dps (only when gyro present) |

### `input_current` / `input_voltage` — Jetson board power (alumet-relay)

Written by the `alumet-relay` sidecar (ADR-011, INA3221 via the alumet
`jetson` plugin) — raw rails, not a computed power field. Grafana computes
board watts at query time with a Flux join: `i_ma × u_mv / 1_000_000`.

| Field | InfluxDB concept | Value |
|---|---|---|
| `_measurement` | measurement name | `input_current` / `input_voltage` |
| `value` | field | milliamps (`input_current`) / millivolts (`input_voltage`) |

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

### 4.5 Per-shuttle drain summary (stm_mission, source=drain)

One point per drained capture. Filter on `source == "drain"` to exclude the
dormant legacy live points. Use a table panel or time series grouped by
`shuttle_id`.

```flux
from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "stm_mission")
  |> filter(fn: (r) => r["source"] == "drain")
  |> filter(fn: (r) => r["_field"] == "loss_pct" or r["_field"] == "accel_peak_g" or r["_field"] == "accel_samples")
  |> group(columns: ["shuttle_id", "gateway"])
  |> yield(name: "drain_summary")
```

### 4.6 Jetson board power (input_current × input_voltage)

Power is not stored; join the two raw rails and multiply at query time.

```flux
cur = from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "input_current")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> rename(columns: {_value: "i_ma"})
volt = from(bucket: "alumet_energy")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "input_voltage")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> rename(columns: {_value: "u_mv"})
join(tables: {c: cur, v: volt}, on: ["_time"])
  |> map(fn: (r) => ({_time: r._time, _value: r.i_ma * r.u_mv / 1000000.0}))
  |> yield(name: "board_power_w")
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

**Phase 2 (ADR-011, CLOSED 2026-05-26):** the Jetson `alumet-relay` sidecar
is deployed and verified. When a gateway sets `ALUMET_SERVER_ADDR`, it relays
INA3221 metrics to the server Alumet instance over gRPC for a unified energy
view; otherwise the gateway writes `input_current`/`input_voltage` to InfluxDB
directly (`INFLUXDB_TOKEN` set) or stays local-only. The modes are mutually
exclusive — see ADR-011 in `docs/decisions.md`.
