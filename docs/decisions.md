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

## ADR-009 — Per-shuttle NTP offset, computed once
**Status:** Closed (with known gap)

**Decision:** On the first CoAP packet from a shuttle, the gateway computes
`offset_ms = receipt_time_ms - tick_ms` and uses it to assign absolute
timestamps to all subsequent packets from that shuttle.

**Rationale:** The STM32U585 has no RTC battery by default. Its `tick_ms`
is relative to boot. The NTP offset anchors the relative timeline to the
gateway's NTP-synchronised wall clock.

**Known gap:** The offset is set once and never refreshed. STM32 crystal
drift (~±50 ppm) will cause timestamp error accumulation over long missions.
Refreshing the offset periodically (e.g., every 100 packets) is P1-4 in
`current_problems.md`.

---

## ADR-010 — Federated XGBoost aggregation strategy
**Status:** OPEN

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

**Action required:** Literature review + experimental comparison before
implementing. This is a thesis contribution — document the choice carefully.

---

## ADR-011 — Real Alumet energy integration
**Status:** OPEN

**Question:** How should the Jetson report real energy consumption to the
central server instead of mock values?

**Current placeholder:** `AlumetProfiler` in `client.py` writes
`random.uniform(25, 45)` W in TEST_MODE and `12.0` W in production.

**Target:** Use real Alumet (developed by UGA/LIG) to measure:
- Jetson power via INA3221 (`tegrastats` or `/sys/bus/i2c/.../in_power*_input`)
- GPU power during `model.fit()`
- Energy cost per FL round (start/stop timestamps × power)

**Alumet relay concept:** An Alumet instance on the Jetson (relay) collects
local sensor data and forwards it to the main Alumet instance on the central
server. The PLUDOS `AlumetProfiler` should be replaced by this relay client.

**Action required:**
1. Install Alumet on the Jetson (`cargo install alumet` or use the Jetson
   plugin from the Alumet repository).
2. Verify the INA3221 sensor is accessible.
3. Replace the `AlumetProfiler` thread with an Alumet relay client call.
4. Update InfluxDB write to use Alumet output format.

See the `pludos-alumet` skill for integration guidance.
