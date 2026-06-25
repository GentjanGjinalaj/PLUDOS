# PLUDOS — Presentation Deck (bullet / slide style)

> Energy-aware **federated learning** for warehouse shuttles.
> CIFRE PhD — UGA (Grenoble) × Savoye. Edge → Gateway → Server.
> Slide-style notes: keep bullets, no paragraphs. Numbers without a primary
> source are marked `unmeasured`. (See `architecture.md` / `decisions.md` for full detail.)

---

## 1. One-liner

- Warehouse shuttles carry a **STM32 sensor node** → stream vibration to a **Jetson gateway** → train an **anomaly model** federated across gateways, **without raw data leaving the edge**.
- Twist: the system **measures its own energy** and is meant to **adapt** to it (energy-aware FL).

## 2. The problem

- Warehouse shuttles fail (bearings, wheels, payload imbalance) → unplanned downtime.
- Vibration signature predicts failure — but:
  - shuttles are **battery-powered** → can't stream 3.3 kHz raw 24/7.
  - raw data is **operationally sensitive** → don't centralize it.
  - many shuttles, many sites → need **on-edge learning**, only models shared.
- Goal: catch anomalies early, **cheaply (energy)**, **privately (federated)**.

## 3. System architecture — 3 tiers

```
[Shuttle STM32U585]  --UDP/WiFi-->  [Jetson Orin Nano gateway]  --Flower/Tailscale-->  [Central server]
  IMU capture            telemetry +     reassemble → Parquet →            XGBoost tree-set
  FSM idle/moving        high-rate drain  XGBoost train (Flower client)     union aggregation
  PSRAM ring buffer                       InfluxDB + Grafana                InfluxDB + Grafana
```

- **Edge (shuttle):** STM32U585AII6Q @160 MHz, Cortex-M33, bare-metal HAL, no RTOS, no `malloc` in app code.
- **Gateway (Jetson):** Orin Nano Super, containerized Python (Podman), asyncio UDP, PyArrow Parquet, Flower client, Alumet/INA3221 energy.
- **Server:** Flower ServerApp, federated XGBoost aggregation, InfluxDB + Grafana.

## 4. Shuttle firmware — highlights

- **Two-state FSM:** `IDLE` ↔ `MOVING`, motion-detected from accel magnitude vs threshold (`MOVEMENT_THRESHOLD_G2`), with dwell + debounce + timeout to kill flapping.
- **Adaptive sampling:**
  - MOVING: **accel 3332 Hz / gyro 416 Hz** (≈8:1), captured to PSRAM.
  - IDLE: **12.5 Hz** snapshot, every 10 min — a cheap "is it healthy at rest" sample.
- **Capture → PSRAM ring → drain:** high-rate runs buffered in external PSRAM, transmitted *after* the run (radio off during motion → ADR-021).
- **Energy:** idle now enters **Stop2 deep sleep**, woken by **IMU motion (hardware INT)** or an **RTC timer** — MCU sleeps instead of polling at 160 MHz.
- **Reliability:** hardware **IWDG watchdog**, CRC-validated PSRAM index → survives resets, ARQ retransmit on the drain.

## 5. Gateway — highlights

- **Two ingest paths, two ports:**
  - `:5683` live low-rate telemetry (24-byte packed struct).
  - `:5684` high-rate **drain** (reassembled from chunked PSRAM blasts).
- **Never blocks the event loop:** Parquet writes + InfluxDB writes go to worker threads / bounded pools.
- **Self-describing storage:** per-drain `cap_accel_*` / `cap_gyro_*` Parquet, distinct from live telemetry.
- **Energy telemetry:** Alumet reads on-board **INA3221** → real board power → InfluxDB → Grafana.
- **Beacon pairing:** gateway broadcasts `PLUDOS-GW:<ip>` so shuttles bond automatically; `SHUTTLE_GROUP` scopes multi-Jetson rigs.

## 6. Server / FL — highlights

- **Flower** orchestrates rounds; clients train **XGBoost** locally on their own Parquet.
- **Aggregation = horizontal tree-set union** (ADR-010): concatenate each client's boosted trees, re-sequence tree IDs, validate the merged booster loads — then broadcast.
- **Anomaly model:** XGBoost on windowed vibration features; `cnn_autoencoder` labeller path scaffolded (FL/ML rework parked for a dedicated pass).
- Raw data **never leaves the gateway** — only model bytes cross the tailnet.

## 7. The energy-aware angle (the thesis core)

- Every tier is **instrumented for energy**, not just function:
  - Gateway: real INA3221 power per drain (comms window vs storage window measured **separately**).
  - Shuttle: Stop2 + wake-on-motion to kill idle waste (saving = `unmeasured`, bench pending).
- Vision: **feedback loop** — energy budget drives FL cadence / model size (`n_estimators` adapted across rounds).
- Honesty rule baked into the project: **no invented numbers** — unmeasured stays `unmeasured`.

## 8. End-to-end data pipeline (one mission)

1. Shuttle moves → FSM `IDLE→MOVING` → capture accel/gyro to PSRAM at full rate.
2. Shuttle stops → FSM `MOVING→IDLE` → seal mission (CRC index).
3. Radio on → **drain**: chunked UDP blast, ARQ for missing chunks, anti-collision jitter.
4. Gateway reassembles → trims settling head → writes `cap_*` Parquet → mirrors summary + waveform to InfluxDB.
5. Flower client loads recent Parquet → trains XGBoost → ships booster to server.
6. Server unions trees → broadcasts → Grafana shows loss %, vibration, energy.

## 9. The small logic — where the months actually went

> These are the unglamorous details that make it *work on hardware*, not just on slides.

- **`HAL_GetTick()` freezes in Stop2.** Naive sleep corrupted timing + the FSM dwell. Fix: gate Stop2 entry on *no dwell in progress* **and** *settle window expired* — two separate freeze bugs.
- **WiFi heap-spin → watchdog reset.** MXCHIP BSP `malloc`s 2.5 KB net buffers from a 4 KB heap shared with `printf`; NULL-alloc → infinite spin → IWDG reset *with no log*. Root-caused via A/B compile gate; fixed by raising heap `0x1000→0x4000`.
- **Drain piggyback.** Idle snapshots never self-drain — they ride the *next* mission's drain wake, so a tiny snapshot isn't worth a radio power-up on its own.
- **Reset-safe timestamps.** Firmware `mission_id` resets to low numbers every boot. Gateway dedups on a **TTL window** + uses its own monotonic `gw_mission_id` (unix-ms) for filenames — so a post-reset id reuse is *new data*, not a dropped duplicate.
- **No NTP for drains.** Drain wall-clock is reconstructed from *intra-capture* tick delta (`tx_tick − t0_tick`, same-boot, exact) — volatile PSRAM guarantees same-boot, so no reboot ambiguity.
- **LPF2 settling trim.** The IMU low-pass filter resets on every ODR change → first samples clip at the ±2 g rail. Trimmed per-stream by its own ODR (idle ~1 s, moving ~30 ms), and `t0` advanced so timestamps stay honest.
- **Wake threshold below the FSM floor.** IMU hardware wake-on-motion is set *under* `MOVEMENT_THRESHOLD_G2` so the FSM still makes the authoritative IDLE→MOVING call after wake — hardware wakes, software decides.
- **8:1 accel:gyro ODR.** Gyro is the bandwidth hog; bearing faults live in accel → spend the FIFO budget where the signal is. FIFO is demuxed back into two streams on the gateway.
- **Mission waveform decimation.** Full 3332 Hz mission is too dense for InfluxDB → a ~50 Hz **per-axis signed peak-per-bin envelope** is mirrored to Grafana (keeps peaks, no aliasing); full rate stays in Parquet.
- **Off-loop finalisation.** A back-to-back multi-mission drain would stall the next BEGIN-ack past the shuttle's budget → the blocking Parquet write runs on a worker thread so the queue never falls a drain behind.
- **CubeMX discipline.** All pin/peripheral changes go through the `.ioc` in CubeMX (owner), firmware logic lives only inside `USER CODE` guards → regeneration never wipes the EXTI-ISR WiFi fix.

## 10. Tech stack

- **Embedded:** STM32CubeIDE, HAL-only, static buffers, hand-rolled IWDG, CubeMX-gated peripherals.
- **Gateway/server:** Python `asyncio`, Podman (rootless), PyArrow/Parquet, InfluxDB, Grafana.
- **FL:** Flower (`flwr`) + XGBoost (tree-set union).
- **Energy:** Alumet + INA3221 (real, on hardware).
- **Network:** raw UDP (telemetry + drain) over WiFi, Tailscale overlay for the tailnet.

## 11. Status

- **Working:** FSM idle/moving, PSRAM capture + drain (0%-loss missions on bench), Stop2 + wake-on-motion, reassembly→Parquet, InfluxDB+Grafana (incl. idle **and** mission waveform panels), real gateway energy, FL tree-set union.
- **Bench-blocked (code done):** shuttle idle-power number (`unmeasured`), `MOVEMENT_THRESHOLD_G2` calibration (harness ready).
- **Parked:** FL/ML rework (CNN-vs-XGBoost), full energy-feedback adaptation loop.

## 12. Contribution framing (claim carefully)

- Not "new ML." The contribution is the **systems integration**: an end-to-end, **energy-instrumented**, federated edge pipeline on real constrained hardware — measured, not simulated.
- Any novelty claim must be checked against prior IoT/FL literature before it goes in the thesis.
