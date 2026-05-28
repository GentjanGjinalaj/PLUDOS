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

**Phase 2 — Jetson Alumet relay sidecar (scaffolding done, relay flags pending):**
`client/alumet-relay/` directory added with three files:
- `Containerfile` — two-stage Rust+Python build for Jetson (ARM64 auto-selected)
- `probe.py` — reads INA3221 sysfs at 10 Hz, writes `/app/power_metrics/latest.json`
  to a shared tmpfs volume so `AlumetProfiler` gets hardware sensor data without
  subprocess calls. Works immediately without confirmed Alumet relay flags.
- `entrypoint.sh` — starts probe.py (always) + alumet-cli relay (when
  `ALUMET_SERVER_ADDR` is set; flags unconfirmed — see TODO in file).

`client/compose.yaml` updated:
- `alumet-relay` service added (no vpn profile — probe mode works without Tailscale).
- `power_metrics` tmpfs volume shared between alumet-relay and ai-worker.
- `ai-worker` now has configurable hostname (`JETSON_HOSTNAME`) so the InfluxDB
  `device` tag is human-readable rather than a container ID hash.

`client/client.py` updated:
- `_read_relay_metrics()` reads `latest.json` from the shared volume; returns `None`
  if file unavailable so caller falls back gracefully.
- `_poll_metrics()` uses `_read_relay_metrics() or _read_tegrastats()` — INA3221
  when relay is running, tegrastats otherwise. InfluxDB writes with `fl_round` tag
  remain in client.py (relay probe only writes the raw file, not InfluxDB).
- `ALUMET_RELAY_METRICS_FILE` env var controls the relay file path.

`server/compose.yaml` updated: relay port 50051 now exposed (was commented).

**Relay flags confirmed from Alumet docs (no longer guessed):**
- Client: `alumet-cli --plugin jetson --relay-out <server:port>`
- Server: `alumet-cli --plugin rapl --relay-in 0.0.0.0:<port>`

**probe.py:** removed (T4.4). Alumet's native Jetson plugin reads INA3221 directly;
the file-based sidecar (`probe.py`) and its shared volume are no longer needed.
`_read_relay_metrics()` and `ALUMET_RELAY_METRICS_FILE` removed from `client.py`.

**New InfluxDB measurements added this session:**
- `fl_phases` — per-phase energy summary from `client/client.py` AlumetProfiler.
  Tags: `device`, `fl_round`, `phase` (load/train/round_total), `nvpmodel`.
  Fields: `duration_ms`, `energy_j`, `avg_power_w`.
- `stm_mission` — per-shuttle mission summary from `client/data-engine.py`.
  Tags: `shuttle_id`, `gateway`. Fields: `energy_j`, `packets`, `duration_ms`.

**Remaining for full Phase 2 activation (requires physical Jetson):**
1. Build relay image: `cd client && podman build -f alumet-relay/Containerfile alumet-relay/`
2. Verify Jetson plugin flag name: `podman exec pludos-alumet-relay alumet-cli --help`
   (may be `--plugin nvidia-jetson` instead of `--plugin jetson`).
3. Verify InfluxDB output flags: `--output influxdb --influxdb-url ...` (used in both
   entrypoint.sh local mode and server/alumet/Containerfile — needs hardware confirmation).
4. Verify INA3221 sysfs path: `ls /sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power*_label`
5. Set `ALUMET_SERVER_ADDR=<server-tailscale-ip>:50051` in `client/.env` to activate relay.
6. Confirm Prometheus exporter port 9091 with `alumet-cli --help` (server Containerfile adds
   `--output prometheus`; verify this flag name on the installed version).

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

**Calibration required:** `FL_ENERGY_BUDGET_J` defaults to `50.0 J`, which is a
placeholder. Measure actual `energy_j` values from `fl_phases` after the first
few hardware runs, then set the budget at 80–90% of the comfortable baseline.
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

**Decision:** A single 28-byte `PludosTelemetry_t` packet is streamed over
**raw UDP** to `udp://<JETSON_IP>:5683`. Both states transmit the same
struct — only the rate differs:

- `STATE_IDLE` — **1 Hz** transmit (10 Hz internal sampling for FSM responsiveness)
- `STATE_MOVING` — **50 Hz** transmit (every sample is sent)

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
- **Bandwidth is fine.** 28 bytes × 50 Hz = 1.4 KB/s per shuttle. 100
  shuttles = 140 KB/s, well under any 2.4 GHz link budget.
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

## ADR-017 — Distance estimation via impulse counter
**Status:** Closed

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
   packets (default 20 ≈ 2 s at 10 Hz), removing mounting-tilt DC offset (~0.22 Hz).
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
