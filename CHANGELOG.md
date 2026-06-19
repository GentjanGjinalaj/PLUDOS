# PLUDOS Changelog

All notable changes to the PLUDOS system. Entries are in reverse chronological
order. Each entry maps to one or more ADRs or resolved backlog items
(see `docs/decisions.md` and `docs/current_problems.md`).

---

## [Unreleased] — Correctness & Consistency Fixes + FL Drain-Disconnect Guard

**Goal:** Small correctness/consistency batch — no FL redesign, no firmware logic
change. Sync two stale comments/docs to the code, and add one guard so the FL
worker fails loudly instead of training on an empty frame. See
`docs/current_problems.md` FL-P0.

### Fixed
- **FL worker trained on 0 samples (FL-P0).** `anomaly.py::load_buffered_data`
  globbed all `*.parquet`. Under ADR-021 the only files written are `cap_*` drain
  captures whose raw schema shares only `temp_c` with the training feature set, so
  `dropna()` emptied every row and `ai-worker` silently retrained on 0 samples and
  saved a garbage model (observed every cycle for 3 days on the live Jetson). Added
  a guard: the loader now skips `cap_*` and daily-consolidated (`YYYY-MM-DD.parquet`)
  files and raises a clear error on an empty frame instead of training. Guard only —
  the real FL↔cap-schema reconnect stays deferred to the FL phase.

### Docs / comments
- `state_machine.md`: MOVING entry threshold corrected `0.05` → `0.06 g²` to match
  `MOVEMENT_THRESHOLD_G2 = 0.06f` (all 7 references, incl. the derived
  `√(1 + 0.06) − 1 ≈ 0.0296 g`).
- `main.c`: PHASE 2c comment "every 5 min" → "every 10 min" (matches
  `CAP_IDLE_SNAP_PERIOD_MS = 600000`); threshold inline comment "0.05 is a guess" →
  "0.06".

### Backlog (recorded in the gitignored `current_problems.md`, not fixed)
- FL-P0 (guard added here, real reconnect deferred), FL-P1 (ADR-010 tree-set union
  duplicates the shared prefix G× per round under T3.6 warm-start with >1 gateway),
  P2-17 (daily-consolidation dedup key collides across STM reboots — dead path,
  recorded only).

---

## [Unreleased] — Drain Delivery-Evidence + Retry Back-off (ADR-021 Phase 1)

**Goal:** Close three silent drain-integrity failure modes that don't bite on a
clean LAN (drains are 0 % loss today) but cause silent data loss or a radio
duty-cycle blowout over a multi-week run. Scope: STM32 firmware + drain receiver,
one coherent change set. Still blast-over-UDP — no ARQ. See
`docs/current_problems.md` P1-7/P1-8/P1-9.

### Added
- **BEGIN liveness echo (`DRAIN_ACK`, wire type 6, 8 B).** The gateway
  (`drain_receiver.py`) replies an 8-byte ack on **every** received `DRAIN_BEGIN`
  (×3, so up to three chances even if one is lost in the post-power-on window).
  This is delivery evidence — "the Jetson is listening" — **not** retransmission;
  types 4/5 stay reserved for the planned Phase-2 NAK/ACK_COMPLETE ARQ.

### Fixed
- **P1-7 · `drained=1` set without delivery evidence.** `Drain_Mission` previously
  marked a mission drained unconditionally after the UDP blast — every `sendto`
  return was `(void)`-cast, so a Jetson reboot / container restart mid-drain
  discarded the mission from accounting while PSRAM still held it. Now the firmware
  sends `DRAIN_BEGIN` ×3, waits a bounded window (`Drain_WaitForAck`, ~750 ms cap)
  for the echo, and **only** marks `drained=1` if it arrives. No echo ⇒ the chunk
  blast is skipped entirely (radio stays dark) and the whole mission retries on the
  next wake. The gateway dedups a just-finalised `(shuttle_id, mission_id)` for the
  `DEDUP_TTL_S` window, so an immediate re-drain is dropped; a retry after the
  window is stored as a fresh capture under a new `gw_mission_id`.
- **P1-8 · Stale `jetson_ip` never refreshed.** `jetson_ip` was resolved once at
  boot; both in-loop re-check paths (PHASE 3 / 3b) are dead under the ADR-021 duty
  cycle (gated on `wifi_driver_initialized`, which is 0 whenever the main loop runs
  — the radio is only up inside a blocking drain). A changed gateway DHCP lease
  meant every drain blasted the old address forever. Now a missing echo triggers a
  one-shot `BEACON_Run` **inside the drain window** (radio already up) to refresh
  `jetson_ip`; later missions in the same wake and the next wake self-heal.
- **P1-9 · Watermark safety-flush retry storm.** PHASE 2d fired `Drain_AllPending`
  every loop iteration while `cap_wtm_hit && IDLE`; `cap_wtm_hit` is only cleared by
  a successful drain, so a gateway-down night spun jitter + 2× `WIFI_PowerOn`
  continuously — radio at max duty, the opposite of ADR-021's intent. Added a
  `CAP_WTM_COOLDOWN_MS` (10 min) back-off after a failed safety-flush drain.

### Known limitation (not fixed)
- PSRAM ring overwrite: `cap_ring_wptr` wraps with no live-mission collision check
  and `m->byte_count` is uncapped (`main.c` ring-write site). Unreachable in normal
  ops (missions ~1.3 MB ≪ 8 MB ring); only bites after repeated drain failures.
  Documented with an in-code `DEFER` comment and tracked as P2-16.

### Docs
- `wire_protocol.md §2` documents the type-6 `DrainAck` and the Phase-1
  blast+BEGIN-ack flow. `sampling_strategy.md` corrected — the "nothing is freed
  until `ACK_COMPLETE`" claim was false for Phase 1 (no ARQ exists); a mission is
  freed on the BEGIN-ack liveness check, with full `ACK_COMPLETE` deferred to
  Phase 2. (A stale copy of the same claim remains in `decisions.md:787` — tracked,
  out of this change set's file scope.)

### Hardware verification pending
- `Drain_WaitForAck` does `recvfrom` on the existing **send** socket (ephemeral
  local port); the gateway replies to the BEGIN's source address. Standard
  BSD/LwIP semantics, but `BEACON_Run` uses a separate **bound** socket, so this
  exact recv-on-sendto-socket path is unverified on the MXCHIP EMW3080. Confirm on
  the first flashed run. Fallback if it fails: `bind` the drain socket to a fixed
  local port at creation.

---

## [Unreleased] — Alumet Relay Log Housekeeping (ADR-011 Phase 2)

**Goal:** Bound unbounded alumet-relay log growth on the Jetson eMMC (CSV had
reached ~330 MB). Housekeeping only — no energy-measurement logic change.

### Added
- `entrypoint.sh` rotates the live CSV once it passes `ALUMET_CSV_MAX_MB`
  (default 200 MB, ≈2 days at 1 Hz): snapshot to `alumet_readings_<ts>.csv`,
  then restart the agent. The alumet csv plugin truncates `output_path` on open
  (verified on hardware — not append-mode), so the restart reopens an empty CSV;
  an in-place truncate is avoided as it would leave a sparse file. Check runs in
  the existing watchdog loop. Newest `ALUMET_CSV_KEEP` (3) archives retained.
- Startup prune of per-restart `alumet-*.log` files, keeping newest
  `ALUMET_LOG_KEEP` (5, incl. current run).
- Three env knobs wired through `compose.yaml` + documented in `.env.example`
  and `client/alumet-relay/OVERVIEW.md`. No new image dependency (no logrotate).
---

## [Unreleased] — Idle-Waveform Trim Fix + Dashboard Drift Cleanup (ADR-021)

**Goal:** Make the Grafana idle waveform show real rest vibration (not the LPF2
rail-clip artifact) and remove the dashboard generator that had diverged from
the committed, provisioned dashboard.

### Fixed
- Idle-snapshot settling trim is now applied **once** in
  `drain_receiver.py:_finalise_mission` (new `_trim_idle_settling` helper),
  *before* the Parquet write, the vibration stats, and the InfluxDB summary.
  Previously the trim lived inside `_write_stream_parquet`, so the
  `stm_idle_wave` InfluxDB waveform got the **raw, untrimmed** samples and the
  pre-trim `t0` — Grafana showed a fake ±2 g rail-clip at the start of every
  idle snapshot. Parquet and InfluxDB now use identical trimmed data and t0.
- Side effect of the same fix: `accel_peak_g` (vibration panel) no longer
  includes the rail-clip spike, since `_mag_stats` now runs on trimmed samples.

### Changed
- `_write_drain_summary` (`data-engine.py`) now also skips the InfluxDB write
  when `INFLUXDB_TOKEN` is empty (mirrors the `gw_status` path) — avoids a
  guaranteed auth failure + noisy warning on token-less gateways.

### Removed
- `build_pludos_dashboard.py` — the Python dashboard generator. It had diverged
  from the hand-tuned, provisioned `server/grafana/dashboards/pludos_system_monitor.json`
  and queried dead pre-ADR-021 measurements (`stm_telemetry`, `tx_rate_hz`,
  old `stm_mission` fields). A single run clobbered both the live Grafana
  dashboard and the committed JSON. The committed JSON is now the single
  hand-maintained source of truth. Docs updated (`server/grafana/OVERVIEW.md`,
  root `OVERVIEW.md`, `README.md`).

---

## [0.5.0] — Wire Protocol v3 + Gyroscope (ADR-016)

**Goal:** Add ISM330DHCX gyroscope data to the telemetry stream while
simultaneously reducing wire cost from 28 to 24 bytes by switching float32
fields to int16 scaled integers.

### Added
- `gyro_x / gyro_y / gyro_z` fields (int16 × 100 = dps) in `PludosTelemetry_t`
- `gyro_mag`, `gyro_jerk` derived columns in `data-engine.py` at flush time
- `horizontal_accel`, `tilt_angle_deg` derived columns (gravity-removed motion signal)
- Mission segmentation columns: `moving_run_id`, `pause_duration_s`,
  `moving_run_dur_s`, `pause_count`, `is_long_pause`
- ZUPT-based `speed_ms` and `displacement_m` columns (using actual inter-packet dt)
- `stm_mission` InfluxDB summary now includes `displacement_m` and `max_speed_ms`
- Live `stm_telemetry` InfluxDB stream for real-time Grafana monitoring

### Changed
- Wire payload shrunk from 28 → **24 bytes** (int16 scaling halves per-field cost)
- `_flush()` returns `(final_displacement_m, max_speed_ms)` for mission summary
- Grafana dashboard updated with derived motion panels; INA3221 panels removed
  (hardware not present in current deployment)

---

## [0.4.0] — Energy-Aware FL Loop + Multi-Gateway Architecture (ADR-010, ADR-014)

**Goal:** Close the energy-awareness feedback loop and support multiple Jetsons
with deterministic shuttle-to-gateway assignment.

### Added
- `XGBoostStrategy._merge_boosters()` — horizontal tree-set union (ADR-010 Option A):
  concatenates all client booster trees, re-sequences IDs, validates before broadcast
- `server.py _query_last_round_energy()` — queries InfluxDB `fl_phases` after each
  round; adapts `n_estimators` (−2 if over budget, +1 if under 60% of budget)
- `fit_config()` passes `n_estimators` to each client via Flower config dict
- `fl-trigger` container — polls InfluxDB, fires `flwr run .` when gateways ready;
  pidfile prevents concurrent rounds; writes `last_run.json` per round
- Multi-Jetson beacon pairing: `SHUTTLE_GROUP=1,2` in `.env` causes data-engine
  to append group IDs to the beacon and reject out-of-group ingress
- `fl_phases` InfluxDB measurement (load / train / round_total per round)
- `fl_energy` InfluxDB stream at 10 Hz during model training

### Changed
- Per-shuttle buffer limits: `SHUTTLE_SOFT_LIMIT=3000`, `SHUTTLE_HARD_LIMIT=4500`,
  `GATEWAY_HARD_LIMIT=100000` (scaled for 10 Hz TX rate)

---

## [0.3.0] — Unified UDP Stream + ADR-015 (replaces CoAP)

**Goal:** Eliminate three production-blocking bugs caused by the CoAP CON
buffer-and-flush model (FSM starvation, invisible environmental data,
buffer-drain fragility). Simplify the protocol to a continuous fire-and-forget stream.

### Added
- `PludosTelemetry_t` — single 28-byte packed struct streamed over raw UDP
  (later 24 bytes in v3). Both states transmit the same struct; only the rate
  differs (10 Hz MOVING, 0.1 Hz IDLE after commit 3e99444)
- `TelemetryProtocol` UDP listener in `data-engine.py` on port 5683
- `BEACON_Run()` in STM32 firmware — listens for `PLUDOS-GW:<ip>` broadcast
  on boot, on WiFi reconnect, and every 30 s while IDLE
- `_broadcast_beacon()` in data-engine — broadcasts gateway IP on UDP 5000
  every `BEACON_INTERVAL_S` seconds
- `sequence_monotonic` wrap detection and sort key in data-engine (P2-10)
- Per-shuttle in-memory buffers: `dict[str, list[dict]]` keyed by shuttle name (P2-9)
- NTP offset refresh every 100 packets per shuttle (ADR-009, P1-4)

### Removed
- CoAP CON stack (`COAP_SendBufferedBatch`, `aiocoap` dependency, port 5684 listener)
- STM32 SRAM ring buffer (`sensor_buffer[256]`)
- `power_mw` wire field (gateway now derives from `state` × `POWER_IDLE/MOVING_MW`)
- `pressure_hpa` wire field (LPS22HH read locally for UART debug only)
- `mission_active` flag (mission boundary now detected gateway-side via 30 s IDLE)

### Changed
- `data-engine.py` migrated from Docker to **Podman** (ADR-003)
- `client/compose.yaml` and `server/compose.yaml` converted to Podman Compose
- All services declared rootless Podman containers

---

## [0.2.0] — Alumet Energy Profiling + Server Stack (ADR-011)

**Goal:** Instrument energy consumption at both the gateway and server tiers.

### Added
- `AlumetProfiler` class in `client.py` — 10 Hz background thread sampling
  `tegrastats` (VDD_GPU / VDD_CPU / VDD_SOC) during `model.fit()`
- `TEST_MODE` fallback to randomised mock values (laptop-safe)
- `alumet` container in `server/compose.yaml` — reads Intel RAPL via
  `/sys/class/powercap`; writes `fl_energy` tagged `device=server`
- `client/alumet-relay/` sidecar scaffolding (Phase 2, ADR-011 open):
  `Containerfile`, `probe.py`, `entrypoint.sh`; gated by `profiles: [energy]`
- InfluxDB 2.7 + Grafana stack in `server/compose.yaml`
- `server/.env.example` and `client/.env.example` with all required keys
- `server/systemd/pludos-server.service` unit file for boot-time auto-start

### Changed
- `client.py evaluate()` now runs real inference on a held-out 80/20 split (P2-6)
- `load_buffered_data()` loads the most recent `MAX_PARQUET_FILES` (default 20)
  Parquet files instead of only the newest one (P2-12)

---

## [0.1.0] — Initial Prototype (Federated Learning Skeleton)

**Goal:** Establish the fundamental data pipeline (STM32 → Jetson → Server)
and prove out the Flower + XGBoost federation before hardware deployment.

### Added
- `server.py` — Flower `ServerApp` with `FedAvg` strategy base
- `client.py` — Flower `ClientApp` with XGBoost tabular training
- `data-engine.py` — initial CoAP ingestion pipeline (`aiocoap`)
- `tools/mock_stm32.py` — mock STM32 packet emitter
- `pyproject.toml` — Flower app configuration
- `server/compose.yaml` — InfluxDB + Grafana (Podman)
- ISM330DHCX accelerometer driver (`Core/Src/sensors.c`)
- HTS221 temperature/humidity driver
- STM32 IDLE / MOVING FSM with 500 ms dwell and 20 s exit timeout
- `EXTI` ISR routing fix for MXCHIP EMW3080 WiFi init hang
  (see `docs/WIFI_FIX_AND_BUILD.md`)
