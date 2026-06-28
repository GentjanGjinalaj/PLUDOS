# PLUDOS Architecture

PLUDOS is a three-tier energy-aware federated learning system for predictive
maintenance on warehouse shuttles. This document describes responsibilities,
data flow, and current implementation status. Where claims in other docs
diverge from the code, the code wins and the divergence is flagged.

---

## Tier 1 — Extreme Edge (STM32U5 on shuttle)

**Hardware:** STMicroelectronics B-U585I-IOT02A (STM32U585AII6Q, Cortex-M33 @
160 MHz, 786 KB SRAM (768 KB main + 16 KB SRAM4 backup domain), 2 MB Flash).
Sensors in use: ISM330DHCX accelerometer (I2C2), HTS221 temperature/humidity
(I2C2), LPS22HH pressure (I2C2, read for local UART debug only — not on
the wire), MXCHIP EMW3080 WiFi (SPI2). The STM32 no longer computes or
transmits `power_mw` — the gateway derives it from the `state` field using
`POWER_IDLE_MW` / `POWER_MOVING_MW` env vars. Real INA3221/Alumet
measurement on the Jetson is tracked in ADR-011 (P2-2).

**Firmware responsibilities (ADR-015 v2):**

> **Superseded by ADR-020/021 Phase 1 (firmware 2026-06-03).** The continuous
> 50 Hz / 0.1 Hz live UDP stream described below is **removed**. The node now
> captures high-rate IMU vibration into PSRAM during MOVING and drains the sealed
> mission in one burst on :5684 at MOVING→IDLE; the radio is held off otherwise to
> save battery. The FSM still polls the accelerometer over I²C every loop (radio-
> independent). See `sampling_strategy.md §4` and `decisions.md` ADR-020/021. The
> ADR-015 description below is retained for the beacon/bonding and packet-format
> details that still apply to the drain path's gateway discovery.

- Sample the accelerometer (104 Hz ODR, on-chip LPF2 cutoff ≈10.4 Hz; polled at
  50 Hz in MOVING, 10 Hz in IDLE for FSM responsiveness), run the idle/moving
  state machine, and stream telemetry directly. No SRAM buffer — every sample
  becomes a UDP packet at the state-appropriate TX rate (50 Hz MOVING,
  0.1 Hz IDLE — every 100th sample). The synchronous UDP send self-throttles to
  the WiFi ceiling if the radio can't sustain 50 Hz.
- Unified telemetry (accel xyz, gyro xyz, temp, humidity, state) goes out as a
  single 24-byte `PludosTelemetry` v3 raw UDP datagram to
  `udp://<gateway>:5683`. No CoAP, no second port, no ACK, no retry.
- Refresh the env-sensor (HTS221/LPS22HH) cache every 500 ms off the hot
  path; the cached value is stamped into every outgoing packet.

**Implementation status:**

- State machine: implemented — IDLE/MOVING with 20 s no-movement timeout,
  500 ms dwell, 300 ms debounce (see `state_machine.md`).
- Transmit path: implemented as fire-and-forget UDP. No application-layer
  retry. ADR-015 documents the trade-off vs the retired CoAP CON path.
- WiFi: working after EXTI ISR routing fix (`docs/WIFI_FIX_AND_BUILD.md`).
  Non-blocking reconnect on `STA_DOWN` via status callback; reconnect
  triggers a short beacon re-probe to handle network changes (≤500 ms).
- Credentials: in `Core/Inc/wifi_credentials.h` (gitignored). Committed
  template at `wifi_credentials.h.example`.
- Beacon discovery: end-to-end. Gateway broadcasts
  `PLUDOS-GW:<ip>[:csv-ids]` on UDP 5000. STM32 listens at boot (30 s
  patient probe), on every WiFi reconnect (short probe), and periodically
  every 30 s while IDLE; bonds only when its `SHUTTLE_ID` is in the
  beacon's shuttle list (multi-Jetson pairing — see `DEPLOYMENT_3JETSON.md`).

---

## Tier 2 — Edge Gateway (Jetson Orin Nano per warehouse)

**Hardware:** Jetson Orin Nano Super Developer Kit (8 GB module, 67 TOPS, 7-25 W envelope). One gateway per warehouse,
designed for ≥100 shuttles per gateway.

**Software (containerised under Podman, see `client/compose.yaml`):**

- `data-engine` service: asyncio UDP listener bound to 0.0.0.0:5683;
  ingests 24-byte `PludosTelemetry` packets from STM32 shuttles, buffers
  in process memory (per-shuttle `dict[str, list[dict]]` keyed by shuttle
  name — P2-9 fix), flushes to Parquet on a bind-mounted host directory
  (`./ram_buffer`). Also broadcasts the beacon on UDP 5000.
- `ai-worker` service: Flower client (`client.py`) that loads the most
  recent `MAX_PARQUET_FILES` files (default 20), trains XGBoost locally,
  and ships the booster bytes to the central server. Profiled by
  `AlumetProfiler` (see below).
- `alumet-relay` service: sidecar that runs `alumet-cli` to read the
  Jetson INA3221 (ADR-011 Phase 2, CLOSED 2026-05-26). Active on hardware —
  reads real sysfs rails and exports `input_current`/`input_voltage` via a
  Prometheus endpoint on :9095, a rotated CSV, and direct InfluxDB writes.
- `tailscale` service: optional sidecar joining the gateway to the Tailnet
  for Gateway↔Server reachability; activated via `--profile vpn`.

**Buffering and flush policy (data-engine):**

- Per-shuttle soft limit: `SHUTTLE_SOFT_LIMIT` (default 3000 packets,
  ≈1 min of 50 Hz MOVING) — proactive flush, mission keeps buffering.
- Per-shuttle hard limit: `SHUTTLE_HARD_LIMIT` (default 4500 packets,
  ≈1.5 min) — emergency mid-mission flush.
- Gateway-wide ceiling: `GATEWAY_HARD_LIMIT` (default 100 000 packets
  across all shuttles combined) — last-resort safety valve.
- Mission-end flush: detected on the gateway. After a run of state
  ==MOVING packets, when the shuttle stays in state==IDLE for
  `MISSION_END_IDLE_S` (default 30 s), that shuttle's buffer is sorted by
  `(shuttle_id, sequence_monotonic)` and written to one Parquet file via
  atomic `os.replace`. There is no `mission_active` wire flag — the
  firmware (post-ADR-015) doesn't transmit one. Other shuttles' buffers
  are unaffected.
- Multi-Jetson pairing: when more than one Jetson is on the same WiFi,
  set `SHUTTLE_GROUP=1,2` per Jetson — the value is appended to the
  beacon (`PLUDOS-GW:<ip>:1,2`) so STMs bond only to their assigned
  gateway, and also serves as an ingress filter that drops out-of-group
  packets. Empty = accept all.

**Temporal alignment (data-engine):**

- The first packet from a given `shuttle_id` establishes the NTP offset:
  `offset = receipt_time_ms - tick_ms`. Subsequent packets have an absolute
  timestamp computed as `tick_ms + offset`.
- The offset is refreshed every `NTP_REFRESH_INTERVAL` packets (default 100)
  to bound STM32 crystal-drift accumulation. Drift delta is logged at each
  refresh. The sort key is `(shuttle_id, sequence_id)`, not `timestamp_ms`,
  so mid-mission offset corrections do not reorder Parquet rows. See ADR-009.

**Energy profiling (AlumetProfiler in `client.py`):**

- Spins a background thread at 10 Hz during `model.fit()` and writes two InfluxDB measurements:
  `fl_energy` (continuous power samples tagged `fl_round`) and `fl_phases` (one summary point per
  named phase: load / train / round_total with duration_ms, energy_j, avg_power_w).
- Power source priority: `_read_alumet_prometheus()` → `_read_tegrastats()` (fallback).
  Alumet is preferred; tegrastats only activates if the Prometheus endpoint is unreachable.
- INA3221 channels confirmed on Jetson Orin Nano: `VDD_IN` (total), `VDD_CPU_GPU_CV`, `VDD_SOC`.

**`alumet-relay` sidecar (`client/alumet-relay/`) — operational:**

Runs `alumet-agent` (Rust, v0.9.4) on the Jetson. Always-active plugins:
`jetson` (INA3221 source) + `prometheus-exporter` (localhost:9095) + `csv` (local file).
Output mode is controlled by `client/.env` — no image rebuild required to switch:

| Mode | Set in `client/.env` | InfluxDB writer |
|------|----------------------|-----------------|
| Local only | *(neither)* | none |
| Standalone | `INFLUXDB_TOKEN=...` | Jetson writes directly |
| With server | `ALUMET_SERVER_ADDR=<server-ip>:50051` | server alumet relay-server writes |

Relay and direct modes are mutually exclusive — if `ALUMET_SERVER_ADDR` is set, the
`influxdb` plugin is skipped on the Jetson to avoid duplicate data in InfluxDB.
Switch: edit `.env`, then `podman-compose restart alumet-relay`. No rebuild.

Log files on the Jetson (gitignored, bind-mounted):
- `client/logs/alumet/alumet-<ts>.log` — startup/plugin status
- `client/logs/alumet/alumet_readings.csv` — raw INA3221 readings (semicolon-delimited)

Server-side: `pludos-alumet` container runs `rapl + relay-server + influxdb + prometheus-exporter`.
`relay-server` listens on port 50051; silent when no Jetson connects.
See ADR-011 in `decisions.md` for full decision history.

---

## Tier 3 — Central Server (laptop, eventually a server)

**Software (containerised under Podman, see `server/compose.yaml`):**

- `influxdb`: InfluxDB 2.7, bucket `alumet_energy`, org `pludos`, default
  admin token `pludos-dev-token` (rotate before any non-local deployment).
- `grafana`: visualisation, default admin/admin.
- The Flower `ServerApp` (`server.py`) is a separate process started via
  `flwr run .` from the project root.

**Federated learning round (server.py):**

- 10 rounds (env-overridable via `FL_NUM_ROUNDS`), `min_fit_clients = 1`, `min_available_clients = 1`.
- `on_fit_config_fn` passes `server_round` to the client so the
  AlumetProfiler can tag energy samples by round.
- Custom `XGBoostStrategy(FedAvg)` overrides `aggregate_fit` with horizontal
  tree-set union (ADR-010 Option A). Each client's booster JSON is parsed, all
  tree objects concatenated, IDs re-sequenced to prevent collisions, and the
  merged booster validated with `xgb.Booster.load_model()` before broadcast.
  Single-client rounds return the booster unchanged. Multi-gateway test pending.

---

## Data flow (steady state, single mission)

1. Shuttle is duty-cycled with the radio off (ADR-020/021). The STM32 runs
   its FSM internally — 10 Hz IDLE poll, 50 Hz MOVING poll — and transitions
   to MOVING when accelerometer deviation > 0.06 g² for 500 ms (with 300 ms
   debounce). These poll rates gate state only; they are not TX rates.
2. In MOVING the STM32 streams the ISM330 FIFO → an 8 MB PSRAM ring at
   accel 3332 Hz / gyro 416 Hz (≈8:1). Nothing is transmitted live. IDLE
   periodically captures a low-rate 12.5 Hz snapshot (10 s every ~10 min) to
   the same ring.
3. On mission end (MOVING→IDLE), on the IDLE-snapshot cadence, or on a PSRAM
   watermark, the shuttle powers the radio on and drains the captured words
   to `udp://<gateway>:5684` as `DrainBegin`/`DrainChunk`/`DrainEnd` frames.
   The gateway acks each `DrainBegin`, reassembles by `chunk_seq`, validates
   per-chunk CRC32, and recovers capture wall-clock from `tx_tick - t0_tick`
   (no NTP offset on this path). The legacy live `:5683` listener still runs
   but is effectively dormant — firmware gates that TX on a flag only set
   during a drain window.
4. The gateway writes one Parquet per `(shuttle_id, mission_id)` on
   `DrainEnd` (or a quiet timeout): `cap_accel_*` / `cap_gyro_*` files holding
   raw int16 samples plus per-mission metadata (ODRs, `t0_wall_ms`,
   `is_idle_snapshot`, completeness). Idle snapshots are head-trimmed
   (`IDLE_TRIM_MS`) to drop the LPF2 settling transient; MOVING is untrimmed.
   Feature engineering is deferred to train time (`anomaly.py`). A
   `stm_mission` summary (`source="drain"`: loss %, sample counts, vibration
   stats) and, for idle snapshots, per-sample `stm_idle_wave` points are
   pushed to InfluxDB on a daemon thread.
4b. (OTA, ADR-019, test/bench tier.) When a firmware image is staged on the
   gateway (`./firmware/firmware.bin` + `manifest.json`), the beacon gains a
   `:fw=<version>` token. In IDLE, after draining, a shuttle whose compiled
   `FW_VERSION` is older requests the image on `udp://<gateway>:5685`; the gateway
   (`ota_server.py`) blasts `OTA_BEGIN`/chunks/`OTA_END`, the shuttle stages to PSRAM
   and NAK-recovers any losses, gates on a whole-image CRC32, then flashes the
   inactive bank and BFB2-swaps with confirm-or-revert anti-brick. Jetson side
   implemented; STM32 side pending the dual-bank enable. Never runs during MOVING.
5. Out of band (manual or scheduled), `flwr run .` starts an FL round; the
   server signals each gateway-side `ai-worker`, which loads the most
   recent `MAX_PARQUET_FILES` files, fits XGBoost, returns booster bytes.
   `AlumetProfiler` pushes 10 Hz power samples to InfluxDB during the fit.
6. Server aggregates via tree-set union (ADR-010 Option A): concatenates
   booster trees from all clients, re-sequences IDs, validates merged
   model, broadcasts to gateways. Energy-aware loop (ADR-014) adapts
   `n_estimators` next round based on InfluxDB `fl_phases` data.

---

## Failure modes and current handling

| Failure | Detection | Handling | Status |
| --- | --- | --- | --- |
| WiFi disconnect on shuttle | `wifi_station_ready` flag, MXCHIP events | FSM keeps sampling into PSRAM; drain is deferred until the radio reconnects. A mission left undrained is retried on the next wake (the gateway dedups the same `(shuttle_id, mission_id)` within the `DEDUP_TTL_S` window, dropping an immediate re-drain; a later retry is stored as a fresh capture) | implemented |
| Gateway unreachable at drain time | No `DrainAck` echo within the bounded wait window | Shuttle skips the chunk blast, keeps the capture in PSRAM, and retries the whole mission on the next wake. Drain chunks themselves are fire-and-forget (CRC32 + completeness flag, no per-chunk ARQ yet — Phase 2) | implemented (ADR-021 Phase 1) |
| MCU reset (IWDG/brownout) before drain | Boot reads CRC-validated PSRAM persist index | PSRAM survives a core reset; the capture bookkeeping is mirrored to a reserved 16 KB PSRAM region and restored on warm boot, so sealed-but-undrained captures (idle snapshots + pending mission) are re-drained instead of lost. The destructive PSRAM self-test is skipped on a valid recovery | implemented (ADR-021, 2026-06-16) |
| Gateway process crash | none on STM32 side | STM32 keeps sending into the void; resumes when data-engine restarts | accepted |
| Gateway directory loss on reboot | `./ram_buffer` is a host bind-mount, not tmpfs | Buffered Parquet survives a container restart; only un-flushed in-memory packets are lost | mitigated |
| Server unreachable (FL round) | Flower retry / hang | Round fails; gateway client error | not hardened |
| Clock drift between STM32 and gateway | NTP offset refreshed every 100 pkts | Drift delta logged; sort key is sequence_monotonic not timestamp | implemented (ADR-009) |
| WiFi credentials in repo | gitignored header | `wifi_credentials.h` not committed | resolved |
| STM bonds to wrong Jetson on multi-Jetson WiFi | Beacon shuttle-list mismatch | STM ignores beacons whose csv-id list omits its `SHUTTLE_ID`; gateway also drops out-of-group ingress | implemented |

---

## Shuttle physical model (Savoye XTPS)

Understanding the physical operation is required to interpret telemetry and
design correct ML features.

**Rail geometry:** Each shuttle runs on a single 1D horizontal rail on one
minifloor. The shuttle body translates strictly forward/backward along this
axis — no lateral or vertical translation. Rail length is typically 10–20 m
(exact from Savoye; set `RAIL_LENGTH_M_MAX` in the distance estimator env).

**Elevator hand-off:** When a box moves between floors, the shuttle delivers
it to a fixed elevator position at one end of the rail. The elevator (conveyor
belt mechanism, not a cab) carries the box vertically; the shuttle itself stays
on its floor. Each floor has its own shuttle.

**Shelf slots and arm extension:** Shelf positions run perpendicular to the
rail. Each storage position is 2-deep: a front slot (directly accessible) and
a back slot (behind the front slot). The shuttle's telescopic arms
extend/retract perpendicular to the rail to pick and place boxes. A
repositioning mission (accessing a back slot) involves 3–4 MOVING legs with
intermediate IDLE pauses: reach front → push box aside → reach back → retract.
Each arm cycle is one pick/place event. (The gateway no longer counts these —
`pick_events` was removed with the schema-v4 raw-only cull; derive from `state`
transitions downstream if needed.)

**Key implications for telemetry and ML** (the gateway now stores raw signal
only — these are physical facts that inform *train-time* feature engineering
in `anomaly.py`, not gateway code):

- **IDLE = physically stopped.** Velocity is known to be exactly zero at every
  IDLE packet — an exact physical constraint that any downstream ZUPT-style
  integrator can exploit to bound drift to a single MOVING segment.
- **Arm motion is perpendicular to the rail axis.** Arm vibration appears on
  the low-variance accel axis; a track-axis variance test can separate it from
  rail travel at analysis time.
- **Mission boundary risk.** The 30 s IDLE timeout (`MISSION_END_IDLE_S`) may
  split a repositioning cycle if an intermediate shelf pause exceeds 30 s.
  Elevator-cycle granularity is future work.

---

## Deployment Modes (T5.1 — ADR-018)

Set `PLUDOS_MODE` in `client/.env` to select the active profile:

| Mode | Compose profile | What runs | What's lost |
|------|-----------------|-----------|-------------|
| `federated` | `--profile vpn` | data-engine, ai-worker (Flower), alumet-relay, tailscale | — |
| `standalone` | `--profile standalone` | data-engine, ai-worker (local loop), alumet-relay, influxdb-local, grafana-local | cross-shuttle federation, central dashboard |
| `headless` | *(no profile)* | data-engine, alumet-relay | AI inference, InfluxDB, Flower |

**federated** (default) — the current design: gateway registers with the central
SuperLink, participates in XGBoost FL rounds, energy data flows to server InfluxDB.
Requires Tailscale (`TS_AUTHKEY`).

**standalone** — gateway runs without any server reachable. `client.py`
`_run_standalone_loop()` retrains XGBoost every `STANDALONE_RETRAIN_INTERVAL_S`
(default 30 min) on buffered Parquet files and persists the model to
`ram_buffer/model/latest.ubj`. A local InfluxDB (7-day retention) and Grafana
are started on the Jetson. All `INFLUXDB_URL` writes go to `localhost:8086`.
Cross-shuttle federation and the central dashboard are unavailable until the
Jetson rejoins the tailnet and switches back to `federated` mode.

**headless** — data-engine only. Parquet files accumulate on the host bind-mount
(`./ram_buffer`); InfluxDB writes are skipped. No AI, no Flower, no server
dependency. Use for pure datalogging before the ML pipeline is ready, or when
power or network budgets prohibit inference.

Switching modes requires only editing `PLUDOS_MODE` in `.env` and restarting with
the corresponding `--profile` flag. No image rebuild.

---

## What is genuinely novel vs engineering

For thesis-writing purposes, distinguish carefully:

- **Engineering, not novel:** XGBoost over Flower (Flower has official XGBoost
  examples), fire-and-forget UDP telemetry on the edge (standard IoT
  pattern), file-backed buffering on edge gateways (standard sysadmin),
  beacon-based service discovery (mDNS / SSDP variants for decades).
- **Plausibly novel, with caveats:** SRAM-pressure-driven flush trigger from
  the constrained edge rather than the gateway, *if* compared against
  existing IoT backpressure literature and shown to outperform. Treating
  the energy cost of an FL round as a tagged time-series for
  energy-aware adaptation, *if* the loop is closed (server reads InfluxDB
  to choose `n_estimators`, which is currently listed as future work).
- **Now implemented:** "Federated XGBoost" via horizontal tree-set union (ADR-010 Option A).
  Single-gateway test is working. Multi-gateway end-to-end test is pending.
  The claim is defensible once multi-gateway data is collected. See `future_options.md §7`
  for the full contribution checklist.
