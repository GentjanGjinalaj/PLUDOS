# PLUDOS Changelog

All notable changes to the PLUDOS system. Entries are in reverse chronological
order. Each entry maps to one or more ADRs or resolved backlog items
(see `docs/decisions.md` and `docs/current_problems.md`).

---

## [Unreleased] — Phase 3: Stop2 Idle Sleep + ISM330 Wake-on-Motion (firmware)

**Goal:** Replace the full-clock busy-poll idle (O3) with Stop2 deep sleep between idle
snapshots, woken by the ISM330 itself on motion (INT1→PE11/EXTI11) or by an RTC wake-up
timer at the snapshot cadence. STM32 firmware only — written entirely inside `USER CODE`
guards on top of the owner's CubeMX change (PE11→EXTI11 rising, RTC on LSE + wake-up timer,
NVIC; see `docs/energy_lpm_design.md` C1–C3, all confirmed in the regenerated source).
Behind a compile gate (`STOP2_IDLE_ENABLE`) so the busy-idle baseline is one `#undef` away.
No wire-format, FL, or gateway change. **No measured energy claim** — the shuttle is not
power-instrumented; this lands the mechanism, the saving is `unknown` until the bench
measurement (`energy_lpm_design.md` §2).

### Added
- **Stop2 idle sleep (`main.c`, PHASE 6).** When IDLE with nothing in flight (no active
  snapshot, no pending safety-flush drain, radio already off per ADR-021), the superloop
  enters `HAL_PWREx_EnterSTOP2Mode` instead of `WIFI_DelayWithYield`. On wake it rebuilds
  the 160 MHz tree (`SystemClock_Config`), resumes SysTick, and logs the wake cause
  (`[LPM] Stop2 wake (motion|rtc-snapshot)`). Accel ODR is untouched across sleep, so no
  `ACCEL_SETTLE_MS` blank is needed on wake.
- **ISM330 wake-on-motion (`main.c`, `ISM330_ArmWakeOnMotion`).** Arms `WAKE_UP_THS`/
  `WAKE_UP_DUR`/`MD1_CFG`(INT1_WU)/`TAP_CFG2`(INTERRUPTS_ENABLE) over I2C once at boot.
  Threshold `WK_THS=1 LSB` (~31 mg at ±2 g) — most sensitive step at this FS, at/just-below
  the FSM's ~30 mg motion floor so the IMU never sleeps through motion the FSM would call
  MOVING. Wake only opens the eyes; the 500 ms-dwell FSM still makes the authoritative
  IDLE→MOVING call. Threshold tunable on bench.
- **RTC snapshot-cadence wake (`main.c`, `RTC_ArmSnapshotWake`).** `MX_RTC_Init` leaves the
  wake-up timer at ~2048 Hz (period 0, RTCCLK/16); re-armed at boot to `CK_SPRE_16BITS`
  (1 Hz) with a **14 s** period (`STOP2_WAKE_PERIOD_S`) — deliberately below the ~16.4 s IWDG
  window so every wake returns to the superloop and kicks the dog (see B2 note). The 10-min
  snapshot cadence is counted in wakes (`STOP2_WAKES_PER_SNAP` ≈ 42), since `HAL_GetTick` is
  frozen across Stop2. The `HAL_RTCEx_WakeUpTimerEventCallback` increments `g_rtc_wake_count`;
  PHASE 2c fires the snapshot when the count reaches the cadence.
- **EXTI11 motion routing (`stm32u5xx_it.c`).** `HAL_GPIO_EXTI_Rising_Callback` adds a
  `GPIO_PIN_11` case that sets `g_motion_wake`. Separate IRQ line from the MXCHIP WiFi SPI
  semaphores (EXTI14/15) — the WiFi path is untouched.

### Fixed
- **Stop2 must not sleep mid-dwell (bench-found).** First flash showed the shuttle waking on
  motion (`[LPM] Stop2 wake (motion)`) and starting the IDLE→MOVING dwell, but never promoting
  to MOVING — it re-entered Stop2 between FSM samples, and the 500 ms dwell is timed in
  `HAL_GetTick`, which freezes in Stop2, so the timer never accumulated. The Stop2 gate now also
  requires `continuous_movement_start_tick == 0` (no dwell in progress); while a dwell is active
  the loop stays awake and polls at the idle rate so `HAL_GetTick` advances and the dwell
  completes (or resets after debounce).
- **Stop2 must not sleep during the LPF2 filter-settle window (bench-found).** After an idle
  snapshot, `Capture_EnterIdle()` restores the 104 Hz ODR and arms the `fsm_settle_until_tick`
  guard (`ACCEL_SETTLE_MS` = 1000 ms), during which the FSM skips ALL motion evaluation. The
  device slept inside that window, and because the guard is `HAL_GetTick`-timed and the tick
  freezes in Stop2, the settle window never expired — leaving the FSM permanently "settling"
  and deaf to motion (the shuttle could no longer be promoted to MOVING after the first idle
  snapshot). The Stop2 gate now also requires the settle window to be already expired
  (`(int32_t)(HAL_GetTick() - fsm_settle_until_tick) >= 0`); once expired before sleep it stays
  expired across Stop2, since the frozen tick keeps the signed diff ≥ 0.
- **Heap raised to 16 KB so the WiFi drain can't IWDG-reset after a Stop2 idle period
  (bench-found, A/B-confirmed).** With Stop2 enabled, the post-mission drain hung in the MXCHIP
  SPI BSP and the IWDG rebooted before any `[NETWORK]` log — the mission survived in PSRAM but
  never shipped. Root cause: the BSP allocates 2.5 KB net buffers with `malloc`
  (`MX_WIFI_BUFFER_SIZE` = 2500, `MX_WIFI_MALLOC` = newlib `malloc`) from the linker heap, which
  was only 4 KB (`_Min_Heap_Size = 0x1000`) and is shared with `printf`. Two 2.5 KB buffers
  cannot coexist in 4 KB, so when a buffer from the pre-Stop2 path is still held at drain time,
  the bring-up's allocation returns `NULL` and `process_txrx_poll` spins forever in its only
  no-timeout path (`while (netb == NULL)`, `mx_wifi_spi.c`), never kicking the watchdog →
  ~16.4 s → reset. A/B test (compiling out `STOP2_IDLE_ENABLE`) confirmed the dependence: the
  drain succeeded every time with the busy-poll idle. Fix: `_Min_Heap_Size` 0x1000 → 0x4000 in
  `STM32U585AIIXQ_FLASH.ld` (786 KB SRAM total, so 12 KB extra is negligible) so the bring-up's
  buffer always fits alongside any held buffer and `printf`. This is a pre-existing heap
  fragility that Stop2 exposed, not a defect in the Stop2 logic itself. A follow-up could route
  `MX_WIFI_MALLOC` to a static pool to drop the heap dependence entirely (project "no malloc"
  rule), but the heap bump is the minimal, low-risk cure.

### Notes
- **B1 dropped — RTC time base NOT needed.** The gateway reconstructs capture wall-clock from
  the intra-capture delta `tx_tick − t0_tick`, and every capture (MOVING mission or 10 s idle
  snapshot) runs fully awake; Stop2 sleeps only in the gap *between* captures, where both
  ticks are re-stamped after wake. So `HAL_GetTick` freezing in Stop2 never corrupts a
  timestamp — the RTC is needed only as the wake source, not as a clock.
- **Step C (B2) is now a power optimization, not a correctness gate.** The hand-rolled IWDG
  (~16.4 s, LSI) keeps counting in Stop2 unless the `IWDG_STOP` option byte is set to *Freeze*
  in STM32CubeProgrammer (Option Bytes → User Configuration → IWDG_STOP → Freeze). The firmware
  no longer depends on it: the 14 s `STOP2_WAKE_PERIOD_S` wakes the M33 before the dog expires
  and kicks it, so the device is safe to flash and sleep **today, without the option byte**.
  Setting it later lets `STOP2_WAKE_PERIOD_S` be raised toward 600 s so the MCU wakes ~42× less
  often (a power win to confirm on the bench, not a claim).
- Build verified syntax-only on the CLI toolchain; full link runs in STM32CubeIDE (same
  makefile caveat as below).

---

## [Unreleased] — FSM Audit: Settling-Trim + Capture/FSM Robustness

**Goal:** Firmware FSM audit follow-up. Improve captured-data quality by cropping
filter-settling samples off the head of every drain, and close three correctness
gaps found in the idle/moving FSM and the PSRAM capture path. STM32 + Jetson only —
no wire-format change, no FL/server change, no CubeMX (`.ioc`) change. Phases map to
the audit plan: Phase 0 = gateway crop, Phase 1 = FSM/capture fixes, Phase 2 = FSM
liveness. Phase 3 (Stop2 low-power + non-blocking drain) is deferred and unblocked
only by an owner CubeMX change (see `docs/energy_lpm_design.md`).

### Added
- **MOVING head settling-trim on the gateway (Phase 0a, `drain_receiver.py`).** The
  ISM330 LPF2 resets on every ODR change, so the first samples of a capture are
  settling garbage. The existing idle-snapshot trim was generalized: `_trim_idle_settling`
  → `_trim_settling(samples, odr_hz, trim_ms)`, and a new `MOVING_TRIM_MS` (default
  30 ms, env-overridable; `0` disables) now crops the MOVING onset transient too.
  Trim is sample-based (`trim_ms * odr / 1000`); the dropped head advances `t0` so
  timestamps stay honest. Idle stays at `IDLE_TRIM_MS` (1000 ms). Doc synced
  (`client/CLAUDE.md`).

### Fixed
- **F3 — wrap-safe filter-settle compare (`main.c`).** `HAL_GetTick() < fsm_settle_until_tick`
  → signed `(int32_t)(HAL_GetTick() - fsm_settle_until_tick) < 0`, correct across the
  49.7-day `HAL_GetTick` rollover (matches the watermark back-off pattern).
- **O1 — PSRAM ring overwrite guard (`main.c`, `Capture_Service`).** New
  `CAP_RING_FULL_BYTES` (~97% of usable PSRAM) guard at the top of the burst loop: when
  the un-drained backlog nears the ring size, stop pulling new FIFO words (newest samples
  lost in hardware) so a ring wrap can no longer clobber still-un-drained missions —
  oldest data preserved. One-shot `[CAPTURE] ring near-full` warning; reset when the
  watermark clears. Only reachable after a long gateway outage.
- **O4 — gyro read skipped in IDLE (`main.c`).** The OUTX_L_G I2C read now runs only in
  `STATE_MOVING`. The FSM uses accel alone, the live stream is off in IDLE, and idle
  snapshots pull gyro from the FIFO — so polling gyro in IDLE was pure I2C traffic. In
  IDLE `gyro_*` keep their last value (used only in cosmetic heartbeat logs).
- **F1 — FSM liveness on accel I2C failure (Phase 2, `main.c`).** The MOVING→IDLE timeout
  lived inside the `HAL_I2C_Mem_Read == HAL_OK` branch, so a persistent accel I2C fault
  while MOVING froze the FSM in MOVING forever — the mission never sealed or drained and
  the PSRAM ring kept filling. The timeout now also evaluates in the read-failure branch
  (same 20 s `NO_MOVEMENT_TIMEOUT_MS`, wrap-safe), sealing and draining the stalled mission.

### Notes
- Build verified syntax-only on the CLI toolchain (`arm-none-eabi-gcc 13.2.1`); the full
  link must run in STM32CubeIDE — the `Debug/` makefile carries machine-specific absolute
  include paths and a CubeIDE-only `-fcyclomatic-complexity` flag, and its default `make`
  goal is `clean` (CLI must call `make all`).
- Deferred to Phase 3 (need owner CubeMX change first): F2 (motion onset lost during the
  blocking drain; needs non-blocking/interleaved drain) and O3 (full-clock I2C busy-poll
  in IDLE; resolved by Stop2 + ISM330 INT1 wake-on-motion). Blockers B1 (free-running RTC
  time base — `HAL_GetTick` freezes in Stop2), B2 (IWDG behaviour in Stop2), B3 (clock/
  sensor restore on wake) tracked in `docs/energy_lpm_design.md`.

---

## [Unreleased] — DrainBegin v2 Wire Bump + Drain Energy/Provenance

**Goal:** Coordinated drain-protocol generation bump so each `DrainBegin` carries
its own provenance, plus the gateway plumbing to measure the energy cost of a drain
and to make InfluxDB writes back-pressure-safe. STM32 + Jetson only — no FL/server
change. Nodes must be reflashed in lockstep; the gateway parser keeps a v1 fallback
that warns on a stale node.

### Changed
- **DrainBegin wire format v1 → v2 (36 B → 42 B).** Four fields appended after the
  v1 layout (offsets unchanged, so a v2 gateway still parses a v1 node's 36-byte
  BEGIN and flags the skew by length): `protocol_version` (`DRAIN_PROTO_VERSION=2`),
  `skipped_since_last` (drains abandoned since the last successful blast, saturates
  at 255 — item 15), `threshold_g2_x1000` (the `MOVEMENT_THRESHOLD_G2` IDLE/MOVING
  boundary stamped per-drain for drift tracking — item 10), `jitter_ms` (the actual
  pre-drain anti-collision wait, so the gateway can undo cross-shuttle t0 skew —
  item 13). Firmware (`main.c`), parser (`drain_receiver.py`), Parquet schema, and
  the `stm_mission` Influx point all updated in one pass; docs synced
  (`wire_protocol.md`, `parquet_schema.md`, `DATA_GUIDE.md`, `ANALYTICS.md`,
  `MODULARITY_AND_PIPELINE.md`, `client/CLAUDE.md`).

### Added
- **Drain reception window (item N).** `stm_mission` now carries `recv_start_ms` /
  `recv_end_ms` / `recv_duration_ms` (gateway clock, distinct from the back-dated
  `t0_wall_ms` capture time). A Grafana panel can integrate INA3221 power over that
  interval for the gateway energy cost of receiving each drain.
- **`gw_mission_id` as an Influx tag (item 14)** so energy↔capture can be joined by
  `(gateway, time-range)` in Flux/InfluxQL.
- **Bounded InfluxDB writer pool (item 8).** Replaces the unbounded
  one-daemon-thread-per-write pattern with a fixed pool + back-pressure (drop-with-
  WARN at `INFLUX_MAX_PENDING`) and bounded exponential-backoff retry; cancelled on
  shutdown (Parquet is the durability path).
- **`SO_RCVBUF` clamp warning** on the drain socket — logs the exact host
  `net.core.rmem_max` sysctl when the kernel clamps below the 4 MB request
  (`network_mode: host`, so it must be set on the Jetson host).
- **Alumet relay poll cadence env-driven** (`ALUMET_POLL_INTERVAL` /
  `ALUMET_FLUSH_INTERVAL`, default 200 ms / 5 Hz) — final rate to be confirmed
  against the measured INA3221 `update_interval` on hardware.

### Notes
- TTL dedup margin (item 7) documented in `drain_receiver.py`: 10 s does not fully
  cover a 15 s jitter straggler (harmless distinct file) and a sub-10 s watchdog
  reset could be wrongly dropped — accepted pending an EMW3080 reconnect-time bench
  measurement.

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
