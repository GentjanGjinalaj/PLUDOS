# PLUDOS — Modularity & the STM↔Jetson Data Pipeline

**Purpose.** This document is the reference for *how the edge of PLUDOS works*:
the STM32 shuttle node, the Jetson gateway, and the Alumet / InfluxDB / Grafana
observability stack that runs **on the gateway itself**. It is deliberately
detailed about sampling, on-device storage, transmission, and reassembly,
because the next design phase — federated learning (FL), model choice
(CNN / self-supervised), and data-drift handling — has to be reasoned about on
top of *exactly* what data exists, at what rate, with what timestamps, and with
what loss characteristics. The central server is described briefly at the end;
it is intentionally not the focus.

The throughline is **modularity**: each tier does one job and degrades
gracefully when the tier above it is gone. A shuttle keeps capturing if the
gateway is down; a gateway keeps storing and visualising if the server is down.

---

## 0. The three tiers at a glance

```
  ┌──────────────┐   raw UDP, 2.4 GHz Wi-Fi   ┌───────────────────┐   Flower / FL    ┌────────────┐
  │  STM32U585   │ ─────────────────────────► │   Jetson Orin     │ ───────────────► │  Server    │
  │  "shuttle"   │   :5684 drain (bulk)       │   Nano "gateway"  │   (VPN overlay)  │ (central)  │
  │  edge node   │   :5000 beacon (discovery) │                   │                  │            │
  └──────────────┘ ◄───────────────────────── └───────────────────┘                  └────────────┘
        ▲            :5684 DrainAck (8 B echo)        │  on-board, local
        │                                             ▼
   ISM330 IMU, HTS221, LPS22HH               InfluxDB + Grafana + Alumet
   8 MB PSRAM capture ring                   (run as containers on the Jetson)
```

- **STM32 (extreme edge):** senses, decides idle-vs-moving, captures high-rate
  IMU to PSRAM, and bursts it to the gateway over Wi-Fi after each run.
- **Jetson (gateway / near edge):** receives, reassembles, stores Parquet,
  summarises to InfluxDB, visualises in Grafana, measures its own energy with
  Alumet, and (optionally) trains/participates in FL.
- **Server (cloud / lab):** aggregates FL model updates across gateways. Not
  required for data collection or local visualisation.

Each arrow is a clean, documented contract (ports + byte layouts), which is what
makes the tiers swappable and independently testable.

---

## 1. The hardware

### 1.1 STM32 shuttle node — B-U585I-IOT02A

- **MCU:** STM32U585AII6Q. Cortex-M33 @ 160 MHz, TrustZone (used non-secure),
  **2 MB Flash**, **786 KB SRAM** (768 KB main + 16 KB SRAM4 backup). No RTOS,
  bare-metal HAL, no dynamic allocation in application code.
- **Wi-Fi:** MXCHIP **EMW3080** over SPI2, **2.4 GHz only**, ST `mx_wifi` BSP,
  AT-command-style IPC. Sockets live on the module; inbound UDP only reaches a
  socket that has been `bind()`-ed.
- **High-rate buffer:** **8 MB external PSRAM** used as a capture ring (see §3).
- **IMU (used):** **ISM330DHCX** 6-axis (accel + gyro), I²C2 @ 400 kHz,
  addr `0xD6` (SA0→VDD on this board). FS held at **±2 g / ±250 dps** in every
  mode, so scaling is constant: accel **0.061 mg/LSB**, gyro **8.75 mdps/LSB**.
- **Environment (used):** **HTS221** temp/humidity, **LPS22HH** pressure
  (both I²C2, cached and used to stamp captures).
- **On board but unused in firmware:** LIS2MDL magnetometer, VL53L5CX
  ToF/gesture, 2× MEMS mics, ambient-light sensor. Adding any needs a CubeMX-side
  peripheral change first.
- **Power note for FL/energy work:** the two user LEDs are driven off; the board
  is currently un-optimised (MCU never sleeps, onboard power LED hardwired). A
  10 000 mAh pack lasts ~1–2 days — energy modelling should treat the present
  firmware as a *worst-case* baseline, not the floor.

### 1.2 Jetson gateway — Orin Nano Super Developer Kit

- 8 GB LPDDR5, 6-core Cortex-A78AE, Ampere GPU **1024 CUDA + 32 Tensor cores,
  67 TOPS**, **7–25 W** envelope. JetPack r35.x, Ubuntu 22.04.
- On-board power monitor **INA3221** (Alumet reads it — §6).
- Runs **Podman 3.4.4** (rootless). All gateway software is containerised.

---

## 2. STM32: state machine & sampling

The shuttle runs a 2-state FSM, **IDLE** ↔ **MOVING**, evaluated every main-loop
pass. Detection is **magnitude-deviation**: `dev = |a_mag² − 1 g²|`. This is
*tilt-immune* (gravity keeps total magnitude at 1 g at any orientation) and
catches travel in any axis, where for flat-mount horizontal motion `dev ≈ a_horiz²`.

| Parameter | Value | Meaning |
|---|---|---|
| `MOVEMENT_THRESHOLD_G2` | **0.06 g²** | trigger level (≈0.24 g dev). **Uncalibrated guess** — must be set from a rest-floor capture. |
| `MOVEMENT_DWELL_MS` | **500 ms** | continuous-above time to enter MOVING |
| `MOVEMENT_DEBOUNCE_MS` | **300 ms** | sub-threshold tolerance inside a dwell (microbreaks) |
| `NO_MOVEMENT_TIMEOUT_MS` | **20 s** | no above-threshold sample for this long → IDLE |
| `ACCEL_SETTLE_MS` | **1000 ms** | blank the trigger after any ODR change (LPF2 re-settles, OUTX reads ~0 g) |
| `SAMPLE_PERIOD_IDLE_MS` | **100 ms (10 Hz)** | FSM poll rate in IDLE |
| `SAMPLE_PERIOD_MOVING_MS` | **20 ms (50 Hz)** | FSM poll cadence in MOVING — **not** a data rate |

**Important physics for the model discussion:** an accelerometer measures
*proper acceleration*, not velocity. Constant-velocity gliding is invisible;
only **start/stop transients and vibration** produce signal. This is why the
node triggers easily on pickup (rotation moves gravity onto new axes) but
poorly on a slow, smooth straight push. A real rail shuttle jerks and vibrates,
so it triggers in practice — but **MOVING is an onset/vibration detector, not a
motion-presence detector**, and any model trained on "MOVING" segments inherits
that bias.

There are three distinct sensor regimes:

1. **LIVE** (accel/gyro 104 Hz) — the legacy low-rate config. The 0.1 Hz live
   telemetry stream that used to ride it (port 5683) is **dormant** under
   ADR-021 (radio is off except to drain).
2. **MOVING capture** — high-rate, FIFO-batched (§3).
3. **IDLE snapshot** — a deliberate low-rate sample of "sitting still" (§3).

---

## 3. STM32: high-rate capture engine (ADR-020/021)

The core idea of ADR-021: **the radio is off while moving.** High-rate IMU is
captured locally to PSRAM, then drained over Wi-Fi *after* the run. This
decouples sensing rate from radio throughput and saves the Wi-Fi energy during
the noisy part of a mission.

### 3.1 MOVING capture

- On IDLE→MOVING the ISM330 is reconfigured to **accel 3332 Hz + gyro 416 Hz**
  and put in **FIFO stream mode**. Each FIFO word = **7 bytes** (1 tag + 6 data),
  accel and gyro words interleaved, distinguished by the tag byte.
- The main loop drains the on-chip FIFO (depth 1023 words) over I²C in bursts
  (96 words ≈ 672 B ≈ 15 ms per burst, up to 12 bursts/service call) into the
  **8 MB PSRAM ring**, one *mission* per MOVING episode.
- FSM detection keeps reading the OUTX registers at the decimated loop rate in
  parallel, so motion detection is unaffected by capture.
- **Overrun events** are counted per mission (FIFO filled faster than drained =
  data loss markers) — surfaced later as integrity metadata.
- **Safety valve:** if undrained bytes cross **75 % of PSRAM (6 MB)**, a
  watermark drain fires even mid-idle; after a *failed* watermark drain
  (gateway down) there's a **10 min cooldown** so the radio doesn't spin at max
  duty overnight.

### 3.2 IDLE snapshot

- Every **10 min**, a **10 s** snapshot is captured at **12.5 Hz** on both axes
  (1:1 accel/gyro), chosen so idle data is directly comparable to MOVING data in
  the shared sub-6 Hz band.
- The first ~1 s is clipped at the ±2 g rail (LPF2 re-settle after the ODR
  change) and is trimmed **on the gateway** (`IDLE_TRIM_MS`, §5).
- Each snapshot is stamped with cached temp + pressure at seal time.

### 3.3 Per-mission bookkeeping

Sample *bytes* live in the PSRAM ring; metadata lives in a 256-slot ring
(`CaptureMission_t`, ~8 KB SRAM, FIFO-reclaimed once drained). Each record
carries: `mission_id`, ring offset, byte/word counts, overrun count,
`start_tick_ms` (the drain **t0**), `sealed`, `drained`, `is_idle_snapshot`, and
the env stamp. `mission_id` is a 16-bit counter that **resets on every STM32
reboot** — it is unique only within one boot session (this matters for dedup, §5).

---

## 4. STM32→Jetson: discovery, drain protocol, and the ACK

### 4.1 Discovery (beacon)

The gateway broadcasts `PLUDOS-GW:<ip>` (or `PLUDOS-GW:<ip>:<csv-ids>` when
shuttle-group filtering is on) on **UDP :5000 every 10 s**. The shuttle learns
the gateway IP from this; it does not need a hardcoded address.

### 4.2 The drain (bulk transfer) — UDP :5684

Wire contract (authoritative layout in `wire_protocol.md §2`):

| Frame | Type | Size | Role |
|---|---|---|---|
| `DrainBegin` | 1 | **42 B (v2)** | opens a mission: magic, shuttle/mission id, total chunks, ODRs, env stamp, `byte_count`, `word_count`, **`t0_tick_ms`**, **`tx_tick_ms`**, + v2 tail: `protocol_version`, `skipped_since_last`, `threshold_g2_x1000`, `jitter_ms` |
| `DrainChunk` | 2 | **18 B hdr + ≤1400 B** | one slice of raw FIFO words; carries `chunk_seq`, `total_chunks`, `payload_len`, **per-chunk CRC32** |
| `DrainEnd` | 3 | — | closes a mission: `total_chunks` + **CRC32 over all data** |
| `DrainAck` | 6 | **8 B** | **gateway→shuttle** echo of a BEGIN — *delivery evidence*, see §4.3 |

- Magic = `"PLDR"` (`0x52444C50`).
- **Chunk payload 1400 B = exactly 200 FIFO words** — never splits a word.
- **BEGIN and END are sent ×3** (`DRAIN_CTRL_REPEAT`) so a single control-packet
  loss doesn't strand a mission.
- **Warm-up:** 24 sacrificial datagrams at 8 ms spacing precede real data to
  absorb the post-power-on loss window (~16 packets measured) and advance
  ARP/MAC learning.
- **Pacing:** yield 1 ms every 8 chunks so the EMW3080 MAC queue and the gateway
  socket drain between bursts (mitigates bursty consecutive loss).
- **Jitter:** before powering the radio, wait a random **1.0–15.0 s** so two
  shuttles exiting MOVING together don't collide on the shared channel.
  `tx_tick_ms` is sampled *after* this wait, so timestamps stay honest.

### 4.3 The DrainAck — what it is and why it matters

This is **not CoAP and not ARQ.** CoAP was removed from PLUDOS entirely; both
telemetry and drain are raw UDP now.

The problem it solves: `sendto()` returning OK only proves the packet left the
radio, **not** that the Jetson received it. Before committing to a multi-MB chunk
blast (which costs Wi-Fi energy — the whole point of ADR-021 is to spend that
energy only when it pays off), the shuttle wants evidence the gateway is
actually listening. So:

1. Shuttle sends `DrainBegin` (×3).
2. Gateway, on receiving *any* BEGIN copy, immediately echoes an 8-byte
   `DrainAck` (type 6) back to the packet's source address.
3. Shuttle waits for the echo: **5 attempts × 150 ms (~750 ms max)**. On a
   match (`shuttle_id`, `mission_id`) it blasts the chunks; on silence it
   **skips** the mission rather than burn radio time into a void.

> **Operational note (resolved 2026-06-15):** the gateway's ack *sender* lives
> in `drain_receiver.py::_send_ack`. For this to work, **(a)** the shuttle's
> drain socket must be `bind()`-ed to a local port (the EMW3080 only delivers
> inbound UDP to a bound socket), and **(b)** the gateway must actually be
> running code new enough to send the ack. A 100 %-loss incident was traced to
> the gateway running a stale image without `_send_ack` — a pure deployment skew,
> not a protocol bug. The lesson for the modular design: *the ack couples the two
> tiers' versions*, so firmware that requires an ack and a gateway that sends one
> must be deployed together.

---

## 5. Jetson: receive, reassemble, store

`data-engine.py` is an `asyncio` UDP service. It owns three sockets — telemetry
`:5683` (dormant), drain `:5684`, beacon `:5000` — and never blocks the event
loop (the only sync work, PyArrow writes and InfluxDB writes, fires on
mission-end or on a fire-and-forget thread).

### 5.1 Drain reassembly (`drain_receiver.py`)

- Chunks are collected per `(shuttle_id, mission_id)` until END or a
  quiet-timeout. CRC is checked; missing `chunk_seq` ranges are recorded.
- **Self-timed timestamps (no NTP):** capture age = `tx_tick_ms − t0_tick_ms`
  (same-boot `HAL_GetTick`, exact). Capture wall-clock = `BEGIN_arrival − age`.
  Volatile PSRAM means both ticks are same-boot, so there's no reboot ambiguity.
- **IDLE settling trim** (`IDLE_TRIM_MS`, 1000 ms): the rail-clipped head of an
  idle snapshot is dropped and `t0` advanced so timestamps stay honest. MOVING
  onset transients are **not** trimmed — they're real signal.
- **Dedup** (`DEDUP_TTL_S`, 10 s): because firmware `mission_id` resets on
  reboot, a finalised `(shuttle, mission)` is held for 10 s — late duplicate
  packets of a just-finalised drain are dropped, but a post-reset drain reusing
  the same id (tens of seconds later) is accepted as new.
- The gateway assigns its own monotonic **`gw_mission_id` (unix-ms)** for
  filenames and InfluxDB — never the ambiguous firmware id.

### 5.2 On-disk Parquet (the FL training substrate)

Each drain produces **one Parquet file per sensor**:
`cap_accel_s<shuttle>_m<gwid>.parquet` and `cap_gyro_...`, zstd-compressed.
Columns (this is the schema the model layer consumes):

| Column | Type | Notes |
|---|---|---|
| `sample_index` | int32 | 0..n−1 within the file |
| `t_ms` | float | `t0 + index·1000/odr` (per-stream ODR) |
| `x` `y` `z` | int16 | **raw** ISM330 LSB at ±2 g / ±250 dps scale |
| `shuttle_id` | int16 | source shuttle |
| `mission_id` | int64 | gateway `gw_mission_id` |
| `odr_accel_hz` / `odr_gyro_hz` | float64 | 3332/416 (MOVING) or 12.5 (idle) |
| `t0_wall_ms` | int64 | reconstructed capture start (post-trim) |
| `is_idle_snapshot` | bool | mode flag — **the natural idle/moving label** |
| `temp_c` / `pressure_hpa` | float32 | env stamp (idle snapshots; NaN for MOVING) |
| `all_packets_received` | bool | integrity gate |
| `packets_total/received/lost`, `packet_loss_pct` | int/float | per-mission loss |
| `missing_chunk_ranges` | str | which seqs were lost |

> For model work: axes are **raw int16**, not g/dps — multiply by 0.061 mg/LSB
> (accel) / 8.75 mdps/LSB (gyro). Loss is per-*chunk* (200 words), so a dropped
> packet removes a contiguous 200-sample block, recorded in
> `missing_chunk_ranges`. Drift/SSL work must decide whether to drop, mask, or
> interpolate those gaps — the metadata to do either is in every file.

### 5.3 Live-buffer policy (telemetry path — currently dormant)

Retained for completeness: per-shuttle soft 3000 / hard 4500 packets, gateway
ceiling 100 000, mission-end after 30 s IDLE. This path is inactive under
ADR-021 but the code remains, so the live stream is re-enableable without
rework.

---

## 6. Alumet — energy measurement (on the gateway)

`alumet-relay` is a sidecar container running `alumet-cli` against the Jetson's
**INA3221** to measure real power draw (ADR-011 Phase 2). It exposes a
**Prometheus endpoint on localhost:9095** that the FL worker scrapes for
per-phase energy, and writes a rotating CSV. It runs **independently of the
server** — energy data is collected and stored on the gateway regardless of
whether the central server is reachable.

> Status: Phase 2 wiring is in place; the *flag-verification* against measured
> ground truth is the open part. Treat Alumet numbers as plumbed-but-unvalidated
> until that's done — relevant if energy is an FL objective, not just telemetry.

---

## 7. InfluxDB + Grafana — observability (on the gateway)

Both run as **local containers on the Jetson** (`pludos-influxdb-local`,
`pludos-grafana-local`). This is the heart of the modularity claim: **you get
full local visualisation with no server at all.** The central server has its own
InfluxDB/Grafana, but the gateway's does not depend on it.

InfluxDB measurements actually written by `data-engine.py`:

| Measurement | Source | Tags | Key fields |
|---|---|---|---|
| `stm_mission` (drain) | every drain | `shuttle_id`, `gateway`, `source=drain`, `kind=mission\|idle_snapshot` | `mission_id`, `packets_total/received/lost`, `loss_pct`, `accel_samples`, `gyro_samples`, `complete`, `temp_c`, `pressure_hpa` |
| `stm_idle_wave` | each idle snapshot | `shuttle_id`, `gateway` | per-sample `ax_g/ay_g/az_g` (+ `gx_dps/gy_dps/gz_dps`) — the actual idle waveform |
| `stm_telemetry` (legacy live path) | — | — | **not written under ADR-021** — the live stream is removed; listed only so the dead measurement isn't mistaken for live data |

InfluxDB writes are **fire-and-forget on a daemon thread**, so visualisation
never stalls the receive loop. Grafana dashboards read these measurements;
because `stm_idle_wave` carries raw waveform points, you can eyeball idle
signatures directly — useful when sanity-checking what a drift detector or SSL
encoder is actually seeing.

**Accessing the Jetson Grafana:** `http://100.119.83.35:3000` (warehouse Jetson
Tailscale IP, `admin`/`admin`). Works from any device joined to the tailnet — a
laptop already on it, or a phone after installing the Tailscale app and logging
in. On the same warehouse LAN you can also use `http://192.168.0.100:3000`. The
laptop's own Grafana additionally carries an `InfluxDB-Jetson` datasource and a
`PLUDOS Jetson (warehouse1)` dashboard, so the standalone Jetson's live data is
viewable from the laptop's `http://localhost:3000` without opening the Jetson
directly.

---

## 8. The communication map (one place)

| Link | Transport | Port | Direction | Payload |
|---|---|---|---|---|
| Beacon | UDP broadcast | 5000 | gateway → shuttles | `PLUDOS-GW:<ip>[:ids]` |
| Telemetry (dormant) | raw UDP | 5683 | shuttle → gateway | 24 B `PludosTelemetry` |
| Drain bulk | raw UDP | 5684 | shuttle → gateway | BEGIN/CHUNK/END |
| DrainAck | raw UDP | 5684 | gateway → shuttle | 8 B liveness echo |
| Alumet scrape | HTTP/Prometheus | 9095 | gateway-internal | power metrics |
| FL | Flower (gRPC) | — | gateway ↔ server | XGBoost booster bytes (over VPN) |

Every edge link is **raw UDP with an explicit byte contract** — no broker, no
CoAP, no TLS on the LAN segment. That keeps the STM side tiny and the tiers
independently testable (`tools/mock_stm32.py` replays the wire format with no
hardware).

---

## 9. Modularity boundaries (what survives what)

- **Shuttle without gateway:** keeps capturing to PSRAM; drains are skipped
  (no ack) and retried; the 75 % watermark + 10 min cooldown bound the damage.
  Data for the most-recent missions survives in the ring until reboot (volatile).
- **Gateway without server:** receives, stores Parquet, summarises to its own
  InfluxDB, and visualises in its own Grafana. Alumet keeps measuring. FL simply
  doesn't run. Nothing in the collection path needs the server.
- **Server without gateways:** aggregation is a no-op; it waits for clients.

This is why the system is described as modular: the **data plane** (sense →
capture → drain → store → visualise) is entirely edge-local, and the
**learning plane** (FL aggregation) is an optional overlay on top.

---

## 10. The server side (brief)

The central server runs a **Flower** `ServerApp` plus its own InfluxDB+Grafana.
FL aggregation is **horizontal XGBoost tree-set union** (ADR-010 Option A):
each gateway's booster trees are concatenated, tree IDs re-sequenced, and the
merged booster validated before broadcast. With all gateways online,
`FL_MIN_FIT_CLIENTS` gates the round so the server waits for everyone.
ADR-010 (aggregation strategy) and ADR-011 (real Alumet integration) are the two
**open** ADRs — anything built on them should be labelled a placeholder until
they close.

---

## 11. The intended deployment & the next design surface

**Planned rig:** **3 Jetson gateways, 6 STM32 nodes (2 per gateway).** The
intent is to use the nodes to emulate a small warehouse: roughly **one STM32
representing the warehouse/environment context and the others acting as
shuttles**, so a single gateway sees a couple of heterogeneous nodes and the
three gateways together form the federation. Group filtering already supports
this (`SHUTTLE_GROUP=1,2` per gateway; beacon suffix bonds only in-group nodes).

This is the substrate for the decisions to make next. The relevant, *concrete*
properties this pipeline hands the model layer:

- **Two rate regimes, one chip:** MOVING (3332/416 Hz, bursty, transient-biased)
  and IDLE snapshots (12.5 Hz, periodic, quiet-state). `is_idle_snapshot` is a
  free coarse label.
- **Raw int16 axes** with exact ODR and reconstructed wall-clock per sample.
- **Structured, *recorded* loss** (per-200-sample-block, with ranges) — a real
  input to robustness / masking strategies, not silent corruption.
- **Per-mission integrity + env + (soon) energy** metadata co-located with the
  waveform.
- **Non-IID by construction:** each shuttle/gateway sees different motion
  profiles → the classic FL heterogeneity setting, plus genuine **data drift**
  as fixtures, surfaces, and the threshold (currently an uncalibrated 0.06 g²)
  change.

Open questions this doc is meant to seed (not answer):
- **Model family:** tree boosters (current FL substrate) vs. CNN on raw windows
  vs. self-supervised pre-training on the abundant unlabelled drain data.
- **Labels:** is `is_idle_snapshot` / FSM state enough, or do we need a richer
  anomaly target? (Note the FSM's onset/vibration bias from §2.)
- **Drift:** per-shuttle vs. per-gateway adaptation, and what to do with the
  recorded packet-loss gaps before they reach a model.
- **Energy as an objective:** once Alumet (ADR-011) is validated, training and
  inference cost become measurable per phase — an FL objective, not just a metric.

---

*Authoritative sources: `docs/architecture.md`, `docs/wire_protocol.md`,
`docs/state_machine.md`, `docs/sampling_strategy.md`, `docs/parquet_schema.md`,
`docs/ANALYTICS.md`, `docs/decisions.md`. Where this doc cites a number it was
read from firmware (`Core/Src/main.c`) or gateway code (`client/data-engine.py`,
`client/drain_receiver.py`); the uncalibrated/open items are flagged inline.*
