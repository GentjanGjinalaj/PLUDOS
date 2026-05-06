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
**Status:** OPEN — Phase 1 complete; Phase 2 relay scaffolded and flags confirmed; hardware build pending

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

**probe.py status:** dormant. Alumet's native Jetson plugin reads INA3221
directly. probe.py is kept in the repo (commented out) as a reference for the
sysfs channel classification logic and as an emergency fallback if the Jetson
plugin is unavailable. See `client/alumet-relay/probe.py` for details.

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

**Implementation files:**
- `server/server.py`: `_query_last_round_energy()`, `fit_config()`, `_current_n_estimators`
- `client/client.py`: `fit()` reads `n_estimators` from `config`; `N_ESTIMATORS_DEFAULT` fallback
- `server/.env.example`: `FL_ENERGY_BUDGET_J`, `FL_N_ESTIMATORS_MIN/MAX/DEFAULT`
