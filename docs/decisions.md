# Architecture Decision Records (ADRs)

Tracks design decisions, their rationale, and open questions. When code
disagrees with an ADR that is marked **Closed**, the ADR wins and the code
needs a fix. When the ADR is **Open**, the code is a placeholder.

---

## ADR-001 — CoAP CON for critical, UDP for non-critical
**Status:** Closed

**Decision:** Use CoAP Confirmable (CON) for vibration, accelerometer, power,
and status packets. Use raw UDP for non-critical environmental data
(temperature, humidity).

**Rationale:** Vibration and power data must reach the gateway — losing a
packet means a gap in the time series that cannot be reconstructed. A CoAP
ACK gives the STM32 confirmation the gateway received it. Temperature and
humidity are supplementary; a dropped packet is acceptable.

**Consequence:** CoAP adds round-trip latency and SRAM pressure. The gateway
must ACK each CON packet. With many shuttles transmitting simultaneously,
the gateway CoAP stack becomes the bottleneck.

---

## ADR-002 — Static SRAM buffers only on STM32
**Status:** Closed

**Decision:** No `malloc`, `calloc`, or `free` in STM32 application code.
All buffers are statically declared at file scope.

**Rationale:** Dynamic allocation on bare-metal systems causes heap
fragmentation, non-deterministic latency, and hard-to-reproduce crashes.
The SRAM budget (786 KB) is sufficient for a statically allocated
`sensor_buffer[256]` plus firmware stack.

---

## ADR-003 — Podman, not Docker
**Status:** Closed

**Decision:** All containerised workloads on Jetson and server use Podman.

**Rationale:** Podman is daemonless and rootless by default, which fits
the Jetson deployment model (user systemd services). It is OCI-compatible
with Docker images. No vendor lock-in.

---

## ADR-004 — tmpfs for gateway buffer
**Status:** Closed

**Decision:** Parquet files are written to a `tmpfs` RAM-disk (`shared_ram_buffer`
volume) on the Jetson.

**Rationale:** Warehouse shuttles operate in bursts. The expected write rate
is manageable in RAM. tmpfs eliminates SD/NVMe wear for frequent small writes.

**Trade-off accepted:** Any unflushed mission data is lost if the Jetson
container crashes or the host reboots. This is acceptable at the current
prototype stage. Durable persistence is a future enhancement.

---

## ADR-005 — XGBoost, not neural networks
**Status:** Closed

**Decision:** Use XGBoost as the federated learning model.

**Rationale:** The vibration data is tabular (3-axis accelerometer time
series, aggregated features). XGBoost is interpretable, fast to train on
small datasets, and works well on GPUs with `tree_method='hist'`. Neural
networks would require more data and longer training cycles, which conflicts
with the energy-aware design goal.

---

## ADR-006 — Flower as FL framework
**Status:** Closed

**Decision:** Use Flower (`flwr`) for federated learning orchestration.

**Rationale:** Flower is framework-agnostic, has official XGBoost examples,
handles gRPC transport, and abstracts round management. The `ServerApp` /
`ClientApp` architecture (Flower 1.x) maps cleanly to the PLUDOS topology.

---

## ADR-007 — Tailscale for gateway-to-server VPN
**Status:** Closed

**Decision:** Use Tailscale as the VPN overlay between Jetson gateways and
the central server.

**Rationale:** Tailscale is zero-config, NAT-traversing, and uses WireGuard
underneath. For a mobile deployment (laptops, different networks), it is
far simpler than managing a static VPN server. The `tailscale` sidecar
in `client/compose.yaml` handles join at startup.

---

## ADR-008 — InfluxDB + Grafana for energy monitoring
**Status:** Closed

**Decision:** Energy metrics are stored in InfluxDB 2.7 and visualised via Grafana.

**Rationale:** InfluxDB is purpose-built for time-series data. Energy power
samples (10 Hz during training) are an exact fit. Grafana provides
out-of-the-box dashboards with Flux query support.

---

## ADR-009 — Per-shuttle NTP offset, refreshed periodically
**Status:** Closed

**Decision:** On the first CoAP packet from a shuttle, the gateway computes
`offset_ms = receipt_time_ms - tick_ms` and uses it to assign absolute
timestamps to all subsequent packets from that shuttle. The offset is
refreshed every `NTP_REFRESH_INTERVAL` packets (default 100) to bound
STM32 crystal-drift accumulation.

**Rationale:** The STM32U585 has no RTC battery by default. Its `tick_ms`
is relative to boot. The NTP offset anchors the relative timeline to the
gateway's NTP-synchronised wall clock. At ±50 ppm drift and 50 Hz sampling,
100-packet windows keep worst-case timestamp error under 0.1 ms.

**Sort key is `(shuttle_id, sequence_id)`**, not `timestamp_ms`, so
mid-mission offset corrections do not reorder Parquet rows. The drift delta
is logged at each refresh for debugging. The offset resets on mission end
so the next mission re-anchors cleanly.

---

## ADR-010 — Federated XGBoost aggregation strategy
**Status:** Closed — Option A implemented; multi-gateway validation pending

**Question:** How should the server aggregate XGBoost models from multiple
Jetson gateways?

**Current placeholder:** `XGBoostStrategy.aggregate_fit` in `server.py`
selects the largest booster payload (`max(payloads, key=len)`). This is
selection, not aggregation. It does NOT federate — one gateway's model wins.

**Candidates to evaluate:**
1. **Horizontal tree-set union with pruning** — merge all trees from all
   clients into one booster; prune to a max tree count. Simple but may
   produce an overfit global model.
2. **Distillation onto a smaller global model** — train a smaller model
   on server using client predictions as soft labels. Requires server-side
   labelled data or a proxy dataset.
3. **Flower's built-in XGBoost aggregation** — Flower has official XGBoost
   federation examples; check if their approach fits the PLUDOS use case.
4. **Bagging-style ensemble** — server maintains an ensemble of client
   boosters and routes inference by shuttle ID (no true model merging).

**Implemented:** Option A — horizontal tree-set union in `server/server.py`
`_merge_boosters()`. Parses each client's booster JSON, concatenates all
tree objects, re-sequences IDs, updates `num_trees`, validates the merged
booster loads before broadcasting. Single-client rounds are a no-op passthrough.

All four candidates are documented in `docs/future_options.md §1` for
future reference. Switch to Option B, C, or D by replacing `_merge_boosters()`
without touching any other part of `server.py`.

---

## ADR-011 — Real Alumet energy integration
**Status:** OPEN — Phase 2 operational (INA3221 live, Prometheus + InfluxDB + CSV outputs confirmed); relay-client architecture decision pending (see below)

**Architecture:**
```
Jetson Orin Nano                         Central Server (laptop)
┌────────────────────────────┐           ┌──────────────────────────────┐
│ AlumetProfiler (client.py) │           │ alumet container             │
│  tegrastats → VDD_GPU/CPU  │──HTTP──►  │  Intel RAPL → power_cpu_w    │
│  writes fl_energy to       │  InfluxDB │  receives Jetson relay       │
│  server InfluxDB (Phase 1) │  (direct) │  streams (Phase 2 / gRPC)    │
│                            │           │  writes fl_energy, device=   │
│ Alumet relay sidecar       │──gRPC──►  │  server to InfluxDB          │
│  (Phase 2 — open)          │ Tailscale │                              │
└────────────────────────────┘           └──────────────────────────────┘
```

**Phase 1 done — Jetson (`client/client.py`):**
`AlumetProfiler` calls `tegrastats --interval 100 --count 1` and parses
`VDD_GPU`, `VDD_CPU`, `VDD_SOC` each polling cycle. `nvpmodel -q` is read once
at profiler init. Writes to InfluxDB measurement `fl_energy` with tags
`device`/`fl_round`/`nvpmodel` and fields `power_gpu_w`/`power_cpu_w`/
`power_total_w`/`energy_j`. TEST_MODE keeps random mock (laptop-safe).

**Phase 1 done — Server (`server/alumet/Containerfile`, `server/compose.yaml`):**
`alumet` container added to server stack. Reads Intel RAPL via
`/sys/class/powercap` (mounted read-only from host). Writes to same
`fl_energy` measurement with `device=server`. Grafana queries need no changes —
all devices share one measurement.

**New InfluxDB measurements added this session:**
- `fl_phases` — per-phase energy summary from `client/client.py` AlumetProfiler.
  Tags: `device`, `fl_round`, `phase` (load/train/round_total), `nvpmodel`.
  Fields: `duration_ms`, `energy_j`, `avg_power_w`.
- `stm_mission` — per-shuttle mission summary from `client/data-engine.py`.
  Tags: `shuttle_id`, `gateway`. Fields: `packets`, `duration_ms`.
  (The `energy_j` field was removed in the schema-v4 raw-only cull — it was a
  hardcoded `power_mw × elapsed` estimate, not a measurement. See ADR-017 note.)

See the `pludos-alumet` skill for Grafana query examples and phase breakdown guidance.

**Phase 2 confirmed working (2026-05-26):**
- `alumet-relay` container runs `jetson + prometheus-exporter + influxdb + csv` plugins.
- `network_mode: host` — required on JetPack 5.x rootless Podman to bypass CNI firewall
  plugin version mismatch that silently blocks port-mapped containers.
- INA3221 channels confirmed: `VDD_IN` (total), `VDD_CPU_GPU_CV` (CPU+GPU), `VDD_SOC` (SoC).
- Prometheus scrape at `localhost:9095/metrics` — read by `client.py _read_alumet_prometheus()`.
- CSV output at `client/logs/alumet/alumet_readings.csv` (bind-mounted, gitignored).
- Energy-aware adaptation confirmed: server reads InfluxDB `alumet_energy` bucket after each
  round and adjusts `n_estimators` — tested, values flow correctly.

**Relay-client architecture — CLOSED (2026-05-26):**

Modes are mutually exclusive to avoid duplicate InfluxDB writes. Controlled via `client/.env`,
no image rebuild required:

| Mode | `.env` setting | Active plugins |
|------|----------------|----------------|
| Local only | neither | `jetson + prometheus-exporter + csv` |
| Standalone | `INFLUXDB_TOKEN=...` | + `influxdb` (Jetson → InfluxDB direct) |
| With server | `ALUMET_SERVER_ADDR=<ip>:50051` | + `relay-client` (→ server alumet → InfluxDB) |

`entrypoint.sh` if/else: when `ALUMET_SERVER_ADDR` is set, `relay-client` activates and
`influxdb` is skipped (server alumet handles the write). When only `INFLUXDB_TOKEN` is set,
`influxdb` activates directly. Server `alumet` container already runs `relay-server` on port 50051.

To switch to relay mode: set `ALUMET_SERVER_ADDR=<server-tailscale-ip>:50051` in `client/.env`,
then `podman-compose restart alumet-relay`.

**T7.2 — Zero-reading watchdog (2026-05-28):**

`entrypoint.sh` now runs a background watchdog alongside `alumet-agent`. Every 10 s the
watchdog reads the last 20 rows of `alumet_readings.csv`, filters for power metric rows,
and increments a counter if the last power value is 0. When `ALUMET_ZERO_THRESHOLD`
(default 5) consecutive zero readings are seen the watchdog kills the `tee` process;
alumet-agent dies via SIGPIPE; `wait` returns non-zero; Podman's `restart: unless-stopped`
brings the container back up. Set `ALUMET_ZERO_THRESHOLD=0` to disable the watchdog.

---

## ADR-012 — Manual application-layer CoAP retry
**Status:** Closed

**Decision:** The firmware implements a manual retry loop (4 attempts, 2/4/8/16 s
exponential backoff) rather than relying on RFC 7252 native retransmission.

**Rationale:** The `mx_wifi` BSP does not expose RFC 7252 retransmission natively.
Implementing it at the application layer gives explicit control over the timeout
budget and allows per-attempt UART logging, which is essential for debugging over
ST-Link. The behaviour (4 retries, binary exponential backoff, starting at 2 s)
is consistent with RFC 7252 §4.8 default parameters (MAX_RETRANSMIT=4,
ACK_TIMEOUT=2 s).

**Consequence:** If a native CoAP library is adopted later (e.g. libcoap), the
manual retry loop in `COAP_SendBufferedBatch` must be removed to avoid
double-retransmission.

---

## ADR-013 — Security posture: accepted risks for research prototype
**Status:** Closed — risks explicitly accepted; review before production deployment

**Context:** PLUDOS operates on a controlled warehouse network (isolated WiFi
subnet + Tailscale overlay). The following security gaps exist and are accepted
for the research prototype phase.

| Risk | Component | Mitigation in production |
|---|---|---|
| CoAP receiver accepts packets from any sender | `data-engine.py` | Whitelist STM32 MAC/IP addresses at WiFi AP level |
| Beacon (`PLUDOS-GW:<ip>`) is unauthenticated plain-text UDP | `data-engine.py` | Add HMAC signature to beacon payload; STM32 verifies before trusting IP |
| Flower runs `--insecure` (no TLS) | `server.py`, `flower-supernode` | Add `--ssl-certfile/keyfile` if Tailscale is removed; redundant while WireGuard is active |
| InfluxDB token is a well-known default | `server/.env.example` | Rotate `INFLUXDB_ADMIN_TOKEN` and all `INFLUXDB_TOKEN` values before any non-local deployment; update Grafana data source manually |
| WiFi credentials compiled into firmware | `wifi_credentials.h` | Move to a flash-resident config sector with OTA-writable credentials (see `future_options.md §6.1`) |

**Rationale:** All components operate inside a WireGuard (Tailscale) tunnel or a
physically controlled WiFi network. Adding full authentication to every hop
would double the firmware complexity and is out of scope for the PhD prototype.
The risks are mitigated by the network boundary, not by per-component auth.

**Review trigger:** Any deployment outside a controlled lab network must revisit
this ADR and implement at minimum: InfluxDB token rotation and Flower TLS.

---

## ADR-014 — Energy-aware FL adaptation: closing the feedback loop
**Status:** Closed — implemented in `server.py` and `client.py`

**Context:** The thesis claims "energy-aware federated learning" but prior to
this ADR the energy measurement loop was open: the Jetson measured and recorded
energy to InfluxDB but the server never read it back or changed anything.

**Decision:** After each FL round, the server queries InfluxDB for the maximum
`energy_j` (field) from the `fl_phases` measurement (phase=`round_total`) across
all gateways. It then adapts `n_estimators` for the next round according to:

| Condition | Action |
|---|---|
| `energy_j > ENERGY_BUDGET_J` | `n_estimators = max(MIN, current - 2)` — reduce fast |
| `energy_j < ENERGY_BUDGET_J × 0.6` | `n_estimators = min(MAX, current + 1)` — grow slow |
| otherwise | no change |

`n_estimators` is passed to each client via `fit_config()`. The client reads it
from the `config` dict in `fit()` instead of using a hardcoded value.

**Control law rationale:** Asymmetric response (reduce by 2, grow by 1) prevents
thrashing around the budget boundary. Querying the **max** across gateways means
the most energy-constrained device drives the adaptation — conservative but fair.

**Calibration required:** `FL_ENERGY_BUDGET_J` defaults to `200.0 J`, calibrated from initial hardware
runs. Adjust at 80–90% of the measured comfortable baseline for your hardware.
See `server/.env.example` for the step-by-step calibration query.

**Pipeline verified (TEST_MODE, 2026-05-06):** End-to-end simulation confirmed
all three stages work correctly: `fl_phases round_total` written to InfluxDB per
round (R1=63.9J, R2=56.9J, R3=55.6J mock), server InfluxDB query successful,
n_estimators adaptation fired (R1→R2: 10→8 on budget breach). TEST_MODE values
(random 25–50W × 1.5s sleep) are not representative of real Jetson hardware.

**Bug fixed during verification:** `client/client.py` `fit()` had no `try/finally`
around the load+train block. A `FileNotFoundError` from `load_buffered_data()` (e.g.
empty buffer) bypassed all `end_phase()` calls, so `fl_phases` was never written and
the adaptation loop saw no data. Fixed: `end_phase("round_total")` and
`profiler.stop()` now run unconditionally in `finally`.

**Implementation files:**
- `server/server.py`: `_query_last_round_energy()`, `fit_config()`, `_current_n_estimators`
- `client/client.py`: `fit()` reads `n_estimators` from `config`; `N_ESTIMATORS_DEFAULT` fallback
- `server/.env.example`: `FL_ENERGY_BUDGET_J`, `FL_N_ESTIMATORS_MIN/MAX/DEFAULT`, calibration query

---

## ADR-015 — Unified telemetry stream over raw UDP (supersedes ADR-001, ADR-012)
**Status:** Closed — replaces the CoAP CON + NC-UDP split with one continuous stream.

**Context:** The original two-channel design (CoAP CON for vibration/power on
port 5683 + raw UDP for environmental on port 5684) accumulated three
production-blocking bugs:

1. **Sensor-loop starvation.** `COAP_SendBufferedBatch` blocked the main loop
   on `recvfrom` waiting for the gateway ACK. Up to four retries with binary
   exponential backoff (2/4/8/16 s) could freeze the FSM for 30 s, during
   which no movement sample was read and no state transition could occur.
   After a single network blip the shuttle became unrecoverable.
2. **Invisible environmental data.** NC-UDP was throttled to one packet every
   30–40 s and only fired in `STATE_IDLE`. During MOVING — the phase where
   temperature/humidity could matter most — no environmental data was sent.
3. **Buffer-and-flush fragility.** The 256-entry SRAM ring buffer drained
   one packet per CoAP round-trip. After 178 leftover samples on MOVING→IDLE
   the device would drip-flush for tens of seconds and could not detect a
   new movement during the drain.

**Decision:** A single 24-byte `PludosTelemetry_t` packet is streamed over
**raw UDP** to `udp://<JETSON_IP>:5683`. Both states transmit the same
struct — only the rate differs:

- `STATE_IDLE` — **0.1 Hz** transmit (10 Hz internal sampling for FSM responsiveness)
- `STATE_MOVING` — **50 Hz** transmit (every sample is sent; WiFi-capped)

The packet carries: shuttle_id (uint8), sequence_id (uint16), tick_ms
(uint32), state (uint8, 0/1), accel xyz, temp_c, humidity_pct. The v2
refinement dropped `pressure_hpa` (LPS22HH read locally for debug only)
and `power_mw` (the gateway derives power from `state` via
`POWER_IDLE_MW` / `POWER_MOVING_MW` env vars). No SRAM ring buffer, no
CoAP framing, no ACK, no retry, no mission_active flag.

**Mission boundary detection** moves to the gateway: when `state` flips from
1 (MOVING) → 0 (IDLE) and stays 0 for >30 s, the gateway flushes that
shuttle's buffered packets to one Parquet file. Packet loss during a
mission is acceptable because the data stream is continuous and the ML
features (vibration energy, environmental envelope) are resilient to
sparse drops.

**Rationale:**

- **Liveness over reliability.** A research prototype that emits noisy data
  is more useful than one that goes silent under packet loss. Raw UDP
  `sendto` returns in ~1 ms regardless of network state; the FSM keeps
  running at 50 Hz even when the gateway is unreachable.
- **Single packet format = one code path.** Removes ~600 lines of CoAP
  build/parse/retry plus the entire ring-buffer state machine. The diff
  net-removes more than it adds.
- **Bandwidth is fine.** 24 bytes × 10 Hz = 240 B/s per shuttle. 100
  shuttles = 24 KB/s, well under any 2.4 GHz link budget.
- **Environmental data visible always.** Temp and humidity arrive at the
  gateway every 20 ms during MOVING (cached from a 2 Hz sensor read so
  the I²C bus is not saturated). Diagnostic value during incidents is
  recovered.

**Trade-offs accepted:**

- No per-packet acknowledgement. A dropped 50 Hz packet is invisible; the
  next one arrives 20 ms later. ML training tolerates this — the Parquet
  files are sparse but consistent.
- Mission-end is heuristic, not authoritative. A 30 s IDLE window before
  flush adds latency to the "mission ended" signal; acceptable because
  Parquet writes are not on the critical path of any user action.
- Port 5684 retired. Existing `data-engine.py` NonCriticalProtocol on 5684
  is removed in the same commit.

**Supersedes:**
- **ADR-001** (CoAP CON for critical, UDP for non-critical) — the two-channel
  split is replaced by a single channel; reliability is achieved by
  redundancy (continuous resend at next sample) rather than per-packet ACK.
- **ADR-012** (Manual application-layer CoAP retry) — no retry; UDP is fire-
  and-forget. The retry budget collapses to 0 attempts × 0 ms = 0 ms block.

**Implementation files:**
- `STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c` — new `TELEMETRY_Send`,
  removed `COAP_*`/`SENSOR_Buffer*`/`UDP_SendNonCritical`, added env-cache,
  movement-debounce.
- `client/data-engine.py` — `TelemetryProtocol` UDP listener on 5683
  replaces `CriticalResource` (aiocoap) and `NonCriticalProtocol` (5684).
- `docs/wire_protocol.md` §1 rewritten; §2 marked deprecated.
- `docs/state_machine.md` — sample rates, debounce, no buffer table.

---

## ADR-016 — Wire protocol v3: ISM330 gyroscope + int16 encoding
**Status:** Closed

**Context:** Wire protocol v2 (ADR-015) used float32 fields — straightforward
but 4 bytes per sensor field. Gyroscope data is needed for two reasons: (1)
`gyro_z` yaw rate distinguishes rail translation from arm-extension events;
(2) `gyro_x/y` torsional vibration reveals motor and bearing faults invisible
to the accelerometer alone. Adding three float32 gyro fields would push the
packet to 40 bytes — a 43% size increase.

**Decision:** Upgrade to v3. Three changes in one struct revision:

1. **int16 scaled integers** replace float32 for all sensor fields:
   accel × 100 (g), gyro × 100 (dps), temp × 100 (°C), humidity × 10 (%RH).
   Resolution: 0.01 g accel, 0.01 dps gyro — both exceed the ISM330DHCX
   noise floor (noise density ~120 μg/√Hz; ~0.0004 g RMS over the LPF2's
   ~10 Hz bandwidth at 104 Hz ODR).
2. **Gyroscope fields added:** `gyro_x`, `gyro_y`, `gyro_z` (ISM330DHCX,
   ±250 dps FS, 8.75 mdps/LSB per DS13281 Table 3). Three fields × 2 bytes = +6 B.
3. **Packet shrinks 28 → 24 bytes:** float32 → int16 saves 2 bytes per field
   across 6 accel/env fields (−12 B); the 3 new gyro int16s add +6 B. Net: −4 B
   despite more data.

**Sentinel:** `0x7FFF` (32767) in any `int16_t` field = sensor unavailable;
gateway converts to NaN.

**Rationale:** More information at smaller packet size. At 50 Hz MOVING:
24 B × 50 Hz = 1.2 KB/s per shuttle (vs 28 B × 50 Hz = 1.4 KB/s in v1).

**Implementation files:**
- `STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c` — `PludosTelemetry_t`
  struct updated to v3; int16 encoding and sentinel substitution added.
- `client/data-engine.py` — unpack format updated to `'<BHIBhhhhhhhh'`;
  sentinel → NaN conversion; gyro columns added to Parquet schema.
- `docs/wire_protocol.md` — §1 rewritten for v3 layout.

---

## ADR-017 — Distance estimation via impulse counter
**Status:** Superseded (2026-05-29, schema v4 raw-only)

> **Superseded.** Distance estimation was removed from the gateway entirely.
> Even with the 1D signed-ZUPT design below, the integrator drifted badly at
> the 10 Hz observable rate (a sub-metre move once read as 32 m). Under the
> schema-v4 "store raw, derive at train time" decision the data-engine became
> a pure raw collector: `distance_m_cum` / `displacement_m` / `speed_ms` are no
> longer computed or stored, and `stm_mission.distance_m` was dropped. The
> design record below is kept for any downstream reimplementation that consumes
> raw `accel_x/y/z`. See `docs/distance_estimation.md` (also marked obsolete).

**Context:** Cumulative distance traveled is a wear proxy for shuttle bearings.
The original ZUPT integration (double-integrating `|a_h|`) was unbounded —
unsigned magnitude means velocity never decreases, no gravity removal, no
reset across IDLE windows — producing values that diverge without bound.

**Decision:** Replace ZUPT-on-magnitude with a proper 1D signed ZUPT, exploiting
the Savoye XTPS rail constraint: the shuttle moves strictly forward/backward on
one axis; `state == IDLE` is an exact physical stop.

1. **Auto-detect the track axis** per flush buffer: compare `var(accel_x)` vs
   `var(accel_y)` on MOVING packets. The rail-aligned axis has far higher variance
   than the perpendicular arm-deployment axis. No calibration required.
2. **HPF the track axis** via running mean subtraction over `DISTANCE_HPF_WINDOW`
   packets (default 20 ≈ 0.4 s at 50 Hz), removing mounting-tilt DC offset.
3. **Integrate signed HPF acceleration.** At `state == IDLE`, reset `vel = 0`.
   Since the shuttle is physically stopped on the rail at IDLE, this ZUPT reset
   is exact — drift is bounded to the duration of each MOVING segment.
4. `distance_m_cum += |vel| × dt` — unsigned path length, correct for
   forward/backward travel.

**Rationale:**
- **No calibration constant.** The original impulse counter needed `STEP_DISTANCE_M`
  tuned per floor. Physics (g = 9.81 m/s²) replaces that.
- **Exact ZUPT.** The 1D rail constraint means IDLE truly means zero velocity.
  This is stronger than any heuristic and bounds integration error per segment.
- **Rejects arm noise.** By selecting only the high-variance axis, perpendicular
  arm-deployment vibration does not contaminate the estimate.
- **Error envelope ±15–20%** on normal runs is sufficient for the thesis goal:
  a wear-correlated feature, not a metrology instrument.

**Trade-offs accepted:**
- HPF introduces ≈2 s lag at motion onset (burn-in of the running mean window).
  Short runs (<2 s MOVING) underestimate distance by up to ±40%.
- `distance_m_cum` is path length (not net displacement) — a return trip doubles it.
- No absolute ground truth; validate with a tape measure on first production run.

**Schema change:** `speed_ms` and `displacement_m` Parquet columns removed;
`distance_m_cum` (float32) added. Old Parquet files missing `distance_m_cum`
are backfilled with 0.0 in `client.py load_buffered_data()`.

**InfluxDB:** `stm_mission.displacement_m` and `.max_speed_ms` fields replaced
by `stm_mission.distance_m` (total per-mission distance).

**Implementation files:**
- `client/data-engine.py` — `_flush()` impulse counter; `_write_mission_summary()`
  writes `distance_m`.
- `client/client.py` — `load_buffered_data()` backward-compat backfill;
  `feature_cols` updated.
- `docs/distance_estimation.md` — algorithm, calibration, error envelope.

---

## ADR-018 — Three-mode deployment: federated / standalone / headless
**Status:** Closed

**Context:** The original design assumed a permanent server connection (Tailscale
always up, central InfluxDB + Grafana always reachable). In practice the Jetson
may need to operate without any server: during initial deployment before a Tailscale
auth key is issued, during network partitions, or in a warehouse that never gets
server infrastructure. The thesis also requires demonstrating the system with and
without the federation layer to isolate energy costs.

**Decision:** Introduce a `PLUDOS_MODE` environment variable controlling a three-
way deployment profile switch with no image rebuild required:

| Mode | `PLUDOS_MODE` | Compose profile | Federation | InfluxDB target |
|------|--------------|-----------------|------------|-----------------|
| Federated | `federated` (default) | `vpn` | Flower SuperLink | central server |
| Standalone | `standalone` | `standalone` | none (local loop) | localhost:8086 |
| Headless | `headless` | *(none)* | none | none |

**Federated** — unchanged current design. Requires Tailscale.

**Standalone** — `client.py __main__` detects `PLUDOS_MODE=standalone` and calls
`_run_standalone_loop()` instead of registering with Flower. Retrains XGBoost
every `STANDALONE_RETRAIN_INTERVAL_S` seconds (default 30 min) on buffered Parquet.
Model persisted to `ram_buffer/model/latest.ubj` (same path Flower `evaluate()`
writes in federated mode — seamless switchover). Local InfluxDB and Grafana
launched as compose services `influxdb-local` / `grafana-local` (7-day retention,
named volumes on Jetson eMMC). `data-engine.py` uses the same `INFLUXDB_URL`; ops
just points it at `localhost:8086`.

**Headless** — data-engine continues writing Parquet; `_write_mission_summary()`
is gated on `PLUDOS_MODE != "headless"` to skip InfluxDB writes. ai-worker is not
started. Parquet files accumulate on the bind-mount for later batch ingestion.

**Inference layer refactored (T5.3):** anomaly labelling and data loading extracted
to `client/anomaly.py` (no `flwr` import). `client.py` is a thin Flower wrapper
around it. Standalone mode imports `anomaly.py` directly. Verify with:
`python -c "import sys; sys.path.insert(0, 'client'); from anomaly import label_packets"`.

**Trade-offs accepted:**
- Standalone loses cross-shuttle federation — each Jetson learns only from its own
  shuttles. Model divergence accumulates until the tailnet reconnects and federated
  mode resumes (at which point the global model overwrites the local one).
- Local InfluxDB uses named volumes on Jetson eMMC; 7-day retention caps growth
  but eMMC writes accumulate. Monitor with `du -sh ~/.local/share/containers/...`.
- Headless has no anomaly detection: if ops needs anomaly alerts during the
  logging phase, standalone is the right choice even without a server.

**Implementation files:**
- `client/anomaly.py` — new; pure inference module (no Flower)
- `client/client.py` — PLUDOS_MODE constant; `_run_standalone_loop()`; `__main__` branch
- `client/data-engine.py` — PLUDOS_MODE read; `_write_mission_summary()` gated
- `client/compose.yaml` — `standalone` profile; `influxdb-local`; `grafana-local`
- `client/.env.example` — PLUDOS_MODE documentation
- `docs/architecture.md` — Deployment Modes section

---

## ADR-019 — Remote firmware update (OTA) for the STM32 shuttle
**Status:** Open (not implemented; physical ST-Link flash is the only path today)

**Context:** Once shuttles are deployed in a warehouse, physical access for an
ST-Link reflash becomes impractical — the whole point of the fleet is that it
runs unattended. We need a way to push new `main.c` builds over the network.
The beacon (UDP :5000) is *not* this: it only discovers the gateway IP at
runtime and writes RAM, never flash. There is no remote-update path in the
firmware yet.

**Two-tier decision (deliberately split by phase):**

**Test/bench phase (now) — lightweight, no security:**
The STM32U585 has **dual-bank flash** (2 × 1 MB) with hardware bank swap via the
`BFB2` option bit. This gives the lightest possible OTA with *no bootloader
project, no TrustZone, no signing*:
1. App receives a new `.bin` over the network (CoAP block-wise — already reserved
   for control messages, ADR-001 — or TFTP; both give chunk-level reliability).
2. App unlocks flash, erases the *inactive* bank, writes the image into it.
3. App verifies a **CRC32 over the received image** — this gate is mandatory, not
   optional: a single dropped chunk over a lossy link otherwise bricks the board.
4. Only if CRC matches: toggle `BFB2`, call `HAL_FLASH_OB_Launch()` → reset boots
   the other bank. Next update ping-pongs back.
   Because the active bank is always remapped to `0x08000000`, the *same* `.bin`
   flashes to either bank — no per-bank relink.

This is ~a few hundred lines (flash driver + option-byte handling + a UDP/CoAP
"firmware push" handler). It skips signing and encryption, which is acceptable on
a trusted test LAN.

**Production phase (deferred) — ST SBSFU/MCUboot:**
Reuse ST's **SBSFU example for the B-U585I-IOT02A** (STM32CubeU5, AN5447 / UM2851):
immutable secure boot, ECDSA signature + SHA256 integrity, AES-CTR encrypted
images, MCUboot dual-slot swap. The example's loader is YModem-over-UART; the
WiFi-download glue (image → inactive slot via the MXCHIP EMW3080) is the part we
write. Trigger via a CoAP CON control message (ADR-001's reserved use).

**Existing code to reuse (do not reinvent):**
- ST SBSFU — `STM32CubeU5/Projects/B-U585I-IOT02A/Applications/SBSFU` (production engine)
- ST OpenBootloader — same tree (UART/USB/SPI/I2C local loaders)
- FreeRTOS `iot-reference-stm32u5` — full network OTA over MQTT on this exact board,
  but FreeRTOS- and AWS-coupled (conflicts with our bare-metal, no-cloud stack)

**Trade-offs accepted:**
- The test-phase path has **no rollback**: a broken image that passes CRC but
  hangs at boot requires physical ST-Link recovery. Keep an ST-Link on the bench.
- No authenticity check on the test path — anyone on the LAN could push an image.
  Acceptable for a trusted bench, *not* for deployment. The production path closes
  this with ECDSA signing.
- SBSFU integration (TrustZone flash layout, signing toolchain) is a multi-week
  effort — hence the phase split rather than jumping straight to secure OTA.

## ADR-020 — High-rate vibration capture: PSRAM mission buffer + burst drain
**Status:** Open (proposed). EMW3080 benchmark **resolved 2026-06-01**; now gated
on one `.ioc` change (I²C2 → Fast mode) plus implementation. Adds a new capture
mode *alongside* ADR-015's continuous stream — it does not replace it. The full
executable strategy (rates, filters, drain protocol, CubeMX steps, Jetson
changes) lives in `sampling_strategy.md`; this ADR is the decision record.

**Context:** ADR-015 deliberately removed the SRAM buffer in favour of a
continuous 50 Hz UDP motion-context stream. That is the right design for the
idle/moving FSM. It is the wrong design for **machine-health / vibration ML**,
which wants kHz accelerometer data (ISO 20816 content reaches ~1 kHz). The
ISM330DHCX supports ODRs up to 6.66 kHz, but the EMW3080 Wi-Fi link cannot ship
kHz raw continuously. Two facts make a buffered approach viable:
- **Missions are short:** ~15 s average, 30 s worst case (real Savoye XTPS
  pick-to-elevator cycle). A whole mission of raw fits in memory.
- **The board has external memory:** B-U585I-IOT02A carries **8 MB Octo-SPI
  PSRAM** (64-Mbit) and 64 MB QSPI flash on top of the 768 KB internal SRAM
  (UM2839 §1 / DB4410). 8 MB ≫ any single mission at kHz.

The EMW3080 radio ceiling was measured with the `BENCH_THROUGHPUT` one-shot sweep
in `main.c` (sender-side pkt/s is the real limit because `MX_WIFI_Socket_sendto`
backpressures at the module's rate). **Result (paired UART sender + Jetson pcap,
desk, single shuttle, 3 runs within ~1 %):** throughput is packet-rate-bound and
climbs monotonically with datagram size — 24 B = 0.22 Mbps, 256 B = 1.78,
512 B = 2.81, 1024 B = 3.92, **1472 B = 4.49 Mbps**. Air loss was ~0 % at ≥256 B
(24 B lost ~5 % from its high packet rate). **Caveat:** that ~0 % loss is a desk
best case; real deployment (moving shuttles, range, 6-way 2.4 GHz contention)
will be lossy and bursty — hence the reliable-drain protocol below. A 675 KB
worst-case mission drains in ~1.2 s single-shuttle (~7 s under 6-way contention),
both inside a normal IDLE gap if drains are staggered.

**Decision (proposed):**
1. **Capture mode, separate from the context stream.** During MOVING, sample
   accel at a high ODR (start **3333 Hz**, see open items) into a raw buffer.
   Gyro at **416 Hz** or duty-cycled (context, not vibration). Temp/humidity stay
   at the existing **2 Hz** cache. The 50 Hz ADR-015 context stream is unaffected.
2. **Buffer in Octo-SPI PSRAM, not internal SRAM.** Memory-mapped PSRAM is
   directly load/store addressable (so it *can* be computed on) with bandwidth far
   above the 20–80 KB/s sample rate. Hold one mission with headroom to
   **double-buffer** (capture mission N+1 while draining N). Internal SRAM is
   reserved for DMA staging and DSP scratch, **not** bulk storage. QSPI flash is
   **not** used as the ring (erase-before-write latency + endurance).
3. **ISM330 FIFO + batched I²C reads.** Per-sample polling at kHz does not fit the
   I²C2 bus (6667 Hz × 12 B ≈ 720 kbit/s payload > 400 kHz bus). Use the sensor's
   on-chip FIFO with a watermark interrupt and burst reads, likely at I²C
   fast-mode-plus (1 MHz). FIFO depth + I²C speed set the real usable ODR.
4. **Burst drain on mission-end (IDLE), in ~1400 B datagrams.** Pack many samples
   per datagram (≈116 accel samples at 1400 B), sequence them, frame the mission
   (`mission_id, shuttle_id, odr, sample_count, t0_ms, layout`). Gateway
   reassembles to one Parquet per mission (int16 + zstd). Store `t0 + ODR` once and
   derive per-sample time by index — never per-sample timestamps.
5. **Idle telemetry stays live and unbuffered** (0.1 Hz, 24 B), independent of the
   drain. The drain and the sparse idle packets share the radio without conflict.
6. **Loss handling = NAK selective-repeat ARQ over the buffered mission.** Real
   deployment loss is significant, so the drain must recover it. Frame the
   mission into ~1400 B chunks (`mission_id, chunk_seq, total_chunks, crc32`),
   blast all chunks, then the gateway returns one NAK listing missing
   `chunk_seq` ranges (or `ACK_COMPLETE`); STM re-sends only those from PSRAM;
   repeat to completion or a bounded round cap (then write `complete=false`).
   Not per-packet ACK (kills throughput), not FEC (constant overhead vs free
   IDLE latency). Idempotent via `mission_id`; control packets re-prompt on
   timeout. Full spec in `sampling_strategy.md §9`.

**Sizing (6 B/sample = 3× int16; arithmetic, not measured):**

| Config | rate | 30 s mission | internal 768 KB? | 8 MB PSRAM? |
|---|---|---|---|---|
| accel 1667 Hz | 10 KB/s | 300 KB | ✓ | ✓ |
| accel 3333 Hz | 20 KB/s | 600 KB | ✓ tight | ✓ |
| accel 3333 + gyro 416 | 22.4 KB/s | 672 KB | ✗ | ✓ |
| accel 6667 + gyro 6667 | 80 KB/s | 2.4 MB | ✗ | ✓ |

**Open items / prerequisites (must resolve before coding):**
- ~~**EMW3080 throughput**~~ — **DONE** (see Context: 4.49 Mbps @ 1472 B,
  ~0 % loss nominal). Datagram size = 1472 B; drain fits IDLE; loss handled by ARQ.
- **ODR ≠ usable bandwidth.** The ISM330DHCX is a general 6-axis IMU, not a
  dedicated wideband vibration sensor (cf. ST IIS3DWB, ~6.3 kHz flat). The on-chip
  LPF2 widest cutoff is ODR/4, so 3333 Hz gives a ~833 Hz clean band. Confirm the
  flat-bandwidth/noise figures in the ISM330DHCX datasheet before trusting content
  above a few hundred Hz. `hardware_refs.md` line 32 also mis-cites the part as
  "ISM330DLC" — fix to DHCX.
- **CubeMX `.ioc` changes — now narrowed** (full click-path in
  `sampling_strategy.md §10`):
  - ~~**REQUIRED:** I²C2 speed Standard → Fast mode~~ — **DONE** (now 421 kHz
    Fast mode, regenerated and booting; sufficient, FM+ 1 MHz only if later going
    to 6667 Hz).
  - **OPTIONAL:** ISM330 INT1→EXTI (else poll FIFO_STATUS); I²C2 RX DMA (else
    blocking burst reads — RX DMA deferred, U5 GPDMA1 flow blocked it).
  - ~~PSRAM memory-mapping~~ — **DONE in user code** (`Core/Src/psram.c`):
    APS6408 mode-register config + `HAL_OSPI_MemoryMapped()` on the CubeMX
    `hospi1` handle, called from `USER CODE BEGIN 2`. Hardware-verified
    (`[PSRAM] self-test PASS`, 8 MB mapped at 0x90000000). No `.ioc` edit.

**Trade-offs accepted:**
- Reintroduces a buffer, against ADR-015's no-buffer stance — but only for the new
  capture mode; the context stream stays buffer-free.
- Draining during IDLE keeps the radio on longer → energy cost, in tension with
  the battery goal. The eventual answer is **on-device feature extraction**
  (FFT band energy, RMS, kurtosis, crest factor from SRAM windows) transmitted at
  ~1 Hz — small, FL-friendly, radio-mostly-off. Raw drain is kept **now** for the
  research phase (we don't yet know which features carry the fault signal);
  feature extraction is the deployment path, tracked as a follow-up to this ADR.
- PSRAM adds active power vs internal SRAM; measure it (ADR-011 path).

> **Revised by ADR-021** (2026-06-01): decision points 1 and 5 (capture only during
> MOVING; live 50 Hz/0.1 Hz stream stays on alongside the drain) are superseded by
> the power-aware model — capture runs in *both* states at state-rated ODR, and the
> radio is powered **off** except to drain. The buffer/drain/ARQ core (points 2–4, 6)
> is unchanged.

---

## ADR-021 — Power-aware capture + WiFi duty-cycling (revises ADR-020)
**Status:** Open (proposed). Capture/drain core from ADR-020 stands; this ADR fixes
*when the radio is on* and *what is captured in each state*. WiFi power primitives
**implemented and hardware-verified 2026-06-01**. **Phase 1 implemented and
hardware-verified 2026-06-03:** radio off during MOVING, powered on only to drain at
MOVING→IDLE then off; live 5683 stream dropped. **Phase 2 implemented 2026-06-03:**
unified IDLE capture (decision 1) — a 10 s snapshot every 5 min at 12.5 Hz on the same
accel+gyro, stamped with HTS221 temp + LPS22HH pressure, queued in PSRAM and drained
piggyback on the next MOVING→IDLE wake; and the total-ring 75 % watermark is now a
cross-mission accumulator (`cap_undrained_bytes`) that forces a standalone safety
drain on a long idle park. Snapshot period/duration and the 12.5 Hz idle ODR are
provisional pending IMU idle-current measurement.

**Context:** profiling the ADR-015 firmware showed the dominant edge-node power
draw is the EMW3080: it is associated to the AP 100 % of the time and transmits a
50 Hz UDP stream throughout every MOVING window (TX bursts ~200 mA). For a
battery shuttle that is the wrong trade — the live stream's only consumers are the
FSM state and a liveness heartbeat, neither of which needs the radio lit during
motion. Meanwhile ADR-020 already buffers the valuable (vibration) data in PSRAM
and drains it in ~1 s. So the radio only needs to wake to drain.

ADR-020 as written kept capture MOVING-only and the live stream always-on. Two
corrections from the field design review:
- The ISM330DHCX has **one ODR at a time** — a 104 Hz live-stream config and a
  3333 Hz capture config cannot coexist on the same chip. The live stream must
  become a *decimated view* of the capture stream, not an independent reader.
- Data is wanted in IDLE too (low-rate baseline / health), not just MOVING.

**Decision (proposed):**
1. **Unified capture, state-rated ODR.** The ISM330 runs through its FIFO at all
   times; the FSM state sets the rate. **MOVING:** accel 3333 Hz / gyro 416 Hz
   (vibration). **IDLE:** both at a few Hz (lowest clean ODR ≈ 12.5 Hz) for
   baseline. The FSM and any live heartbeat read the latest sample out of the
   FIFO drain — one source of truth, no second ODR config.
2. **Radio off by default; on only to drain.** WiFi is powered **off** (module
   held in hardware reset) during MOVING and between drains. It is powered **on**
   to drain at: (a) MOVING→IDLE (the just-captured mission), and (b) a PSRAM
   **flush watermark** during long IDLE — a shuttle parked for hours still wakes
   periodically to flush so the ring never overruns. After `ACK_COMPLETE` the
   radio powers off again.
3. **On-demand power primitives.** `WIFI_PowerOn()` (probe-once → hard-reset →
   init → connect → DHCP → socket) and `WIFI_PowerOff()` (close socket →
   disconnect → hold EMW3080 RESET low). Re-power costs ~4 s (DHCP-dominated) —
   negligible amortized over a mission or hours of idle. The boot path now calls
   `WIFI_PowerOn()`; the off→on cycle was verified reversible on hardware
   (`[SELFTEST] WiFi power-cycle PASS`). `jetson_ip` is cached across cycles so
   re-draining skips the beacon wait.

**Trade-offs accepted:**
- No real-time stream during motion. The gateway no longer sees 50 Hz live state
  while a shuttle moves; it learns the mission (incl. state timeline) at drain
  time. Acceptable: real-time motion state is not a current consumer requirement,
  and the energy saving is large. Revisit if a live-during-motion need appears
  (could keep a 1 Hz heartbeat with the radio in 802.11 power-save instead of off).
- Re-init latency (~4 s) on every drain wake — paid during IDLE where latency is
  free.
- Flush-watermark policy adds a wake even with no mission (long idle). Bounded by
  the low IDLE ODR (~12.5 Hz × 6 B × 2 = ~150 B/s → the ring fills slowly).

**Still open (unchanged from ADR-020 §open):** anti-alias filter corners per ODR,
ISM330 achieved bandwidth/noise, IMU current per mode, fault frequencies. Plus new:
the flush-watermark threshold and IDLE ODR are provisional — set them once IMU
idle current and ring-fill rate are measured. Also: the MCU itself still busy-waits
in `WIFI_DelayWithYield` (no `WFI` sleep) — a separate power follow-up.
