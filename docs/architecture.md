# PLUDOS Architecture

PLUDOS is a three-tier energy-aware federated learning system for predictive
maintenance on warehouse shuttles. This document describes responsibilities,
data flow, and current implementation status. Where claims in other docs
diverge from the code, the code wins and the divergence is flagged.

---

## Tier 1 — Extreme Edge (STM32U5 on shuttle)

**Hardware:** STMicroelectronics B-U585I-IOT02A (STM32U585AII6Q, Cortex-M33 @
160 MHz, 786 KB SRAM (768 KB main + 16 KB SRAM4 backup domain), 2 MB Flash). Sensors used: ISM330DLC accelerometer over
I2C2 @ 100 kHz; MXCHIP EMW3080 WiFi module over SPI2. Power sensing path is
declared in the firmware data model (`adc_power_mw` field in
`SensorSample_t`) but the ADC is not configured in the .ioc and the firmware
inserts the placeholder `150.0f` mW. Temperature/humidity sensors are
declared in the wire protocol but not yet read from the on-board sensors.

**Firmware responsibilities:**

- Sample the accelerometer, run the idle/moving state machine, buffer samples
  in static SRAM (`sensor_buffer[256]`), and transmit to the gateway.
- Critical telemetry (vibration, accel, power, status) goes out as CoAP
  Confirmable POST to `udp://<gateway>:5683/vib` with binary payload.
- Non-critical telemetry (temperature, humidity placeholders) goes out as
  raw UDP to the same address/port during idle state.
- Manage SRAM pressure: trigger flush at 70% buffer fill or on transition to
  idle; suspend sampling at 95% fill until the buffer is drained.

**Implementation status:**

- State machine: implemented (see `state_machine.md`).
- CoAP transmit: implemented with manual application-layer retry loop and
  hand-rolled exponential backoff (2s/4s/8s/16s, max 4 attempts). Note this
  contradicts `CLAUDE.MD`'s instruction to rely on native RFC 7252 backoff.
- WiFi: working after EXTI ISR routing fix (see `docs/WIFI_FIX_AND_BUILD.md`).
- Compile blocker: `jetson_ip` is referenced as a writable buffer in several
  places but never declared as one — needs `static char jetson_ip[16] = {0};`.
- Credentials: `WIFI_SSID` / `WIFI_PASSWORD` are committed in `main.c`;
  should be moved to an ignored header.
- Beacon discovery on UDP 5000 is mentioned in CLAUDE.MD as zero-touch
  provisioning but is currently stubbed: firmware uses `JETSON_IP` directly
  ("Skipping beacon discovery, using hardcoded IP") and the gateway's
  `broadcast_beacon` task sleeps 60s indefinitely.

---

## Tier 2 — Edge Gateway (Jetson Orin Nano per warehouse)

**Hardware:** Jetson Orin Nano Super Developer Kit (8 GB module, 67 TOPS, 7-25 W envelope). One gateway per warehouse,
designed for ≥100 shuttles per gateway.

**Software (containerised under Podman, see `client/compose.yaml`):**

- `data-engine` service: aiocoap-based CoAP server bound to 0.0.0.0:5683;
  ingests `CriticalPayload` packets from STM32 shuttles, buffers in process
  memory (Python list `ram_buffer`), flushes to Parquet on the volume
  `shared_ram_buffer` (a tmpfs RAM-disk).
- `ai-worker` service: Flower client (`client.py`) that loads the latest
  Parquet file, trains XGBoost locally, and ships the booster bytes to the
  central server. Profiled by `AlumetProfiler` (see below).
- `tailscale` service: optional sidecar joining the gateway to the Tailnet
  for Gateway↔Server reachability; activated via `--profile vpn`.

**Buffering and flush policy (data-engine):**

- Hard limit: 500 packets in Jetson process memory.
- Soft limit (flush trigger): 80% (400 packets).
- Mission-end flush: when the STM32 sets `mission_active = 0` in any packet,
  the buffer is sorted by `(shuttle_id, sequence_id)` and written as
  `mission_data_<unix_ts>.parquet` via PyArrow with `os.replace` for atomic
  rename. The buffer is then cleared.

**Temporal alignment (data-engine):**

- The first packet from a given `shuttle_id` establishes the NTP offset:
  `offset = receipt_time_ms - tick_ms`. Subsequent packets have an absolute
  timestamp computed as `tick_ms + offset`.
- This is per-shuttle, set once, never refreshed. Real-world clock drift on
  the STM32 (no RTC battery on the IOT02A by default) will eventually cause
  drift; periodic resync is not implemented.

**Energy profiling (AlumetProfiler in `client.py`):**

- Spins a background thread at 10 Hz during `model.fit()` and writes power
  samples to InfluxDB tagged with `fl_round`.
- **Current implementation is not real Alumet.** It writes a Python
  `random.uniform(25.0, 45.0)` in TEST_MODE and a hardcoded `12.0` in
  production mode. Wiring this to real Jetson sensors (INA3221 via
  `tegrastats` or Alumet's Jetson plugin) is open work.

---

## Tier 3 — Central Server (laptop, eventually a server)

**Software (containerised under Podman, see `server/compose.yaml`):**

- `influxdb`: InfluxDB 2.7, bucket `alumet_energy`, org `pludos`, default
  admin token `pludos-secret-token` (rotate before any non-local deployment).
- `grafana`: visualisation, default admin/admin.
- The Flower `ServerApp` (`server.py`) is a separate process started via
  `flwr run .` from the project root.

**Federated learning round (server.py):**

- 3 rounds, `min_fit_clients = 1`, `min_available_clients = 1`.
- `on_fit_config_fn` passes `server_round` to the client so the
  AlumetProfiler can tag energy samples by round.
- Custom `XGBoostStrategy(FedAvg)` overrides `aggregate_fit`. **The current
  override does NOT aggregate.** It decodes each client's parameters,
  collects raw libxgboost byte streams, and selects
  `max(streams, key=len)` — the largest booster wins and is sent back as
  the global model. This is selection, not aggregation. Several public docs
  (`README.md`, `CHANGELOG.md`) say FedAvg, the progress report claims
  "Federated XGBoost over UDP Streams" as a contribution, and `CLAUDE.MD`
  says "horizontal federated learning or strict tree pruning" — none of the
  three matches `server.py`.

---

## Data flow (steady state, single mission)

1. Shuttle wakes, STM32 enters MOVING state on accelerometer threshold.
2. STM32 samples at 50 Hz, buffers locally; at 70% buffer fill or on
   transition back to IDLE, flushes via CoAP CON to gateway.
3. Gateway receives, parses 39-byte payload, computes/applies NTP offset,
   appends to in-process list, ACKs with 2.04 Changed.
4. On `mission_active = 0` flag (or 80% gateway-buffer fill), gateway sorts
   and writes Parquet to tmpfs.
5. Out of band (manual or scheduled), `flwr run .` starts an FL round; the
   server signals the gateway-side client; the client loads the latest
   Parquet, fits XGBoost on GPU, returns booster bytes, AlumetProfiler
   pushes power samples to InfluxDB during fit.
6. Server "aggregates" (currently: picks largest booster) and ends round.

---

## Failure modes and current handling

| Failure | Detection | Handling | Status |
| --- | --- | --- | --- |
| WiFi disconnect on shuttle | `wifi_station_ready` flag, MXCHIP events | Sampling continues, buffer fills, eventually triggers SRAM-suspend at 95% | implemented |
| Gateway unreachable | CoAP ACK timeout | Manual app-layer retransmit up to 4 attempts with exponential backoff; on final failure, sample is dropped (wait for next cycle) | implemented |
| Gateway process crash | none on STM32 side | STM32 sees ACK timeouts and drops samples | partial |
| Gateway tmpfs loss on reboot | inherent to tmpfs | Mission data not yet flushed to durable storage is lost | accepted, by design |
| Server unreachable (FL round) | Flower retry / hang | Round fails; gateway client error | not hardened |
| Clock drift between STM32 and gateway | no detection | NTP offset is set once per shuttle and never refreshed | known gap |
| WiFi credentials in repo | n/a | none | needs fix |

---

## What is genuinely novel vs engineering

For thesis-writing purposes, distinguish carefully:

- **Engineering, not novel:** XGBoost over Flower (Flower has official XGBoost
  examples), CoAP-confirmed transport for critical sensor data (standard
  IoT pattern), tmpfs buffering on edge gateways (standard sysadmin), random
  jitter for transmission scheduling (CSMA-style, decades old).
- **Plausibly novel, with caveats:** SRAM-pressure-driven flush trigger from
  the constrained edge rather than the gateway, *if* compared against
  existing IoT backpressure literature and shown to outperform. Treating
  the energy cost of an FL round as a tagged time-series for
  energy-aware adaptation, *if* the loop is closed (server reads InfluxDB
  to choose `n_estimators`, which is currently listed as future work).
- **Currently unsupported by the code:** "Federated XGBoost" as a
  contribution. The aggregation step is selection, not federation. To
  defend this claim, the strategy needs to actually merge boosters
  (e.g., by horizontal tree-set union with pruning, or distillation onto
  a smaller global model).
