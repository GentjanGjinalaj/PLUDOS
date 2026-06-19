# Known Problems & Backlog

Tracked issues with priority levels. Fix P0 before merging new features;
fix P1 before any non-local deployment.

- **P0** — Blocking (prevents correct operation)
- **P1** — Important (degrades correctness or safety)
- **P2** — Nice-to-have (quality / future work)

---

## P0 — Blocking

### FL-P0 · Training loader incompatible with `cap_*` drain schema — *deferred to FL phase*
`anomaly.py::load_buffered_data` globbed all `*.parquet`. Under ADR-021 the only
files written are `cap_accel_*` / `cap_gyro_*` drain captures whose raw schema
(per-sample `sample_index, t_ms, x, y, z` plus per-mission metadata
`shuttle_id, mission_id, odr_accel_hz, odr_gyro_hz, t0_wall_ms, is_idle_snapshot,
temp_c, pressure_hpa, all_packets_received, packets_total, packets_received,
packets_lost, packet_loss_pct, missing_chunk_ranges`) shares only `temp_c`
with the training feature set. The loader read millions of cap rows, `dropna()`
emptied them, and `ai-worker` silently retrained on **0 samples**, saving a
garbage model every cycle (observed: every cycle for 3 days on the live Jetson).
**Guard added** this commit: `load_buffered_data` skips `cap_*` and
daily-consolidated files and raises a clear error on an empty frame instead of
training. The real FL↔live-data reconnect (train on `cap_*` raw schema or wire a
compatible feature path) is **deferred to the FL phase**.

---

## P1 — Important

### ~~P1-2~~ · Jetson IP hardcoded in firmware — **RESOLVED**
**Short-term:** `JETSON_IP` lives in `wifi_credentials.h` (gitignored) alongside SSID/password.
**Long-term:** Beacon discovery now wires the gateway IP at runtime — see P2-1 below.
`wifi_credentials.h::JETSON_IP` is the compile-time fallback used only when the first-boot
beacon probe times out (and is overridden as soon as the periodic IDLE retry succeeds).

### ~~P1-3~~ · CoAP retry contradicts design intent — **RESOLVED (twice)**
Originally closed by ADR-012 (manual 4-attempt retry). The retry loop later
turned out to be the root cause of a recurring "shuttle stuck after MOVING→IDLE"
hang — `recvfrom` blocked the sensor FSM for up to 30 s per failed batch, so
movement could not be re-detected. **Fully retired** by ADR-015: the firmware
now sends a unified `PludosTelemetry` packet via fire-and-forget raw UDP. No
ACK, no retry, no buffer.

### ~~P1-4~~ · NTP offset never refreshed — **RESOLVED**
Offset is now refreshed every `NTP_REFRESH_INTERVAL` packets (default 100) per
shuttle. Drift delta is logged at each refresh. The Parquet sort key is
`(shuttle_id, sequence_id)` — not `timestamp_ms` — so mid-mission offset
corrections do not break sort order. Reset on mission end.

### ~~P1-5~~ · `client/.env.example` missing — **RESOLVED**
`client/.env.example` created with all required keys. `env_file: .env` added
to `data-engine` and `ai-worker` services in `compose.yaml`.
`server/.env.example` remains missing — tracked separately as P2-8.

### ~~P1-6~~ · Tailscale image pinned — **RESOLVED**
`tailscale/tailscale:v1.66.0` in `client/compose.yaml`. Update the tag deliberately when upgrading.

### ~~P1-7~~ · Drain `drained=1` set without delivery evidence — **RESOLVED**
**Symptom (silent):** `Drain_Mission` marked a mission `drained=1` unconditionally
after the UDP blast — every `MX_WIFI_Socket_sendto` return was `(void)`-cast, so
`sendto` succeeding only meant the packet left the radio, not that the Jetson got
it. A Jetson reboot / container restart mid-drain dropped the mission from
accounting while PSRAM still held the only copy. Drains are 0 % loss on a clean
LAN, so this never fired in dev — but it is a silent data-loss path over a
multi-week run.
**Fix:** gateway echoes an 8-byte `DRAIN_ACK` (wire type 6) on every received
`DRAIN_BEGIN`. Firmware sends `DRAIN_BEGIN` ×3, then `Drain_WaitForAck` (recvfrom
with `SO_RCVTIMEO`, 5 attempts, ~750 ms cap) and sets `drained=1` **only** if the
ack arrives. No ack ⇒ skip the chunk blast (radio stays dark), leave `drained=0`,
retry the whole mission next wake. The gateway dedups a just-finalised
`(shuttle_id, mission_id)` for the `DEDUP_TTL_S` window so an immediate re-drain is
dropped; a later retry is stored as a fresh capture. This is liveness evidence, **not** ARQ
(types 4/5 stay reserved for Phase 2). See CHANGELOG "Drain Delivery-Evidence".
**Hardware check pending:** `recvfrom` runs on the existing send socket (ephemeral
port); the gateway replies to the BEGIN source address. Unverified on the MXCHIP
EMW3080 (`BEACON_Run` uses a separate *bound* socket). Confirm on first flash;
fallback is to `bind` the drain socket to a fixed local port.

### ~~P1-8~~ · Stale `jetson_ip` never refreshed under the duty cycle — **RESOLVED**
**Symptom:** `jetson_ip` is resolved once at boot (`main.c`, `if jetson_ip[0]==0`).
The two in-loop re-check paths (PHASE 3, PHASE 3b) are **dead** under ADR-021 —
both are gated on `wifi_driver_initialized != 0`, which is 0 whenever the main loop
runs (the radio is only powered inside a blocking drain). So a changed gateway DHCP
lease would make every drain blast the old address forever and — combined with the
old P1-7 bug — get marked drained anyway.
**Fix:** a missing BEGIN-ack now triggers a one-shot `BEACON_Run` **inside the
drain window** (radio already up) to refresh `jetson_ip`. Later missions in the
same wake and the next wake self-heal. On a beacon miss the existing IP is kept, so
the refresh is safe. (PHASE 3/3b remain dead under the duty cycle but are harmless;
left as-is to avoid scope creep.)

### ~~P1-9~~ · Watermark safety-flush retry storm — **RESOLVED**
**Symptom:** PHASE 2d fired `Drain_AllPending` **every loop iteration** while
`cap_wtm_hit && state==IDLE`. `cap_wtm_hit` is only cleared by a *successful*
`Drain_Mission`, so a gateway-down overnight park looped jitter + 2× `WIFI_PowerOn`
continuously — radio at max duty, the exact opposite of ADR-021's energy intent.
**Fix:** added a `CAP_WTM_COOLDOWN_MS` (10 min) back-off — after a safety-flush
drain that leaves `cap_wtm_hit` still set, PHASE 2d is gated off for the cooldown
(wrap-safe signed tick comparison) before retrying.

### FL-P1 · ADR-010 tree-set union multiplies trees under T3.6 warm-start — *deferred to FL phase*
With >1 gateway, the shared global prefix is duplicated G× per round. Each client
warm-starts from the merged global model (`client.py` fit, `xgb_model=`) so its
booster already contains all global trees; the server then concatenates **all**
clients' trees (`server.py::_merge_boosters`), so the shared prefix is added once
per client every round and the model grows geometrically. Single-client rounds are
unaffected (no-op passthrough). Fix (warm-start deltas only, or dedup the shared
prefix at merge) **deferred to the FL phase**.

---

## P2 — Nice-to-have / Future Work

### ~~P2-1~~ · Beacon discovery — **RESOLVED (end-to-end)**
**Gateway:** `_broadcast_beacon()` sends `PLUDOS-GW:<ip>` to 255.255.255.255:5000 every
`BEACON_INTERVAL_S` (default 10 s). `network_mode: host` on the `data-engine` service in
`client/compose.yaml` lets the broadcast escape the container.
**STM32 side:** `BEACON_Run()` in `main.c` listens at boot (30 s patient probe),
on every WiFi reconnect (short ≤500 ms probe), and periodically every
`BEACON_RETRY_PERIOD_MS = 30 s` while IDLE. The discovered IP overrides the
compile-time `JETSON_IP` fallback as soon as it arrives, so swapping networks
(office WiFi ↔ phone hotspot) is transparent to the firmware.

### P2-2 · STM32 power figure — removed, no shuttle energy measurement exists
ADR-015 v2 removed `power_mw` from the wire and the firmware. The gateway-side
`POWER_IDLE_MW`/`POWER_MOVING_MW` × elapsed estimate was also removed in the
schema-v4 raw-only cull — it was a hardcoded placeholder, not a measurement, and
storing it risked being read as ground truth. `stm_mission` now carries only
`packets`/`duration_ms`.
**Remaining gap:** there is no per-shuttle power/energy figure at all, and no
current-sense shunt on the board.
**Fix (long-term):** add an INA219 on the 3.3 V rail to I2C1 (I2C2 is full) and
have the STM32 send a real `power_mw` field. Only then re-introduce a shuttle
energy column — derived from a measurement, not a constant.

### P2-3 · AlumetProfiler — Phase 1 done, Phase 2 scaffolded (Energy research)
**Phase 1 (done):** `_poll_metrics()` calls `tegrastats --interval 100 --count 1`
and parses `VDD_GPU`, `VDD_CPU`, `VDD_SOC` rails. `nvpmodel` is read once at
profiler init and attached as an InfluxDB tag. `energy_j` is integrated from
power × elapsed time. InfluxDB schema matches `wire_protocol.md §3` fully.
TEST_MODE falls back to random mock — laptop runs still produce InfluxDB points.
**Phase 2 (scaffolded):** `client/alumet-relay/` sidecar created. `probe.py`
reads INA3221 sysfs at 10 Hz into a shared tmpfs file. `_read_relay_metrics()`
in `client.py` reads that file with `_read_tegrastats()` as fallback. Relay
gRPC forwarding to the server is wired but alumet-cli flag names must be
verified on hardware before activating. See ADR-011 in `decisions.md`.

### ~~P2-4~~ · XGBoost aggregation — **RESOLVED (Option A)**
`XGBoostStrategy.aggregate_fit` now implements horizontal tree-set union
(ADR-010 Option A). Each client's booster trees are concatenated into one
merged booster; tree IDs are re-sequenced to prevent collisions; the merged
booster is validated with `xgb.Booster.load_model()` before broadcast.
Single-client rounds return the booster unchanged (no merge overhead).
Multi-gateway test required to verify end-to-end — see the theory-check
table in `docs/DEPLOYMENT_GUIDE.md`. Full option comparison in
`docs/future_options.md §1`.

### ~~P2-5~~ · Temperature/humidity sensors not read — **RESOLVED**
HTS221 driver in `Core/Src/sensors.c`; LPS22HH pressure driver added in the same
session, both on I2C2 (no CubeMX changes needed). After ADR-015 v2 the unified
`PludosTelemetry` packet carries `temp_c` and `humidity_pct` only — `pressure_hpa`
was dropped from the wire (LPS22HH is read locally for debug logging but no longer
transmitted). Current payload is 24 bytes total (see `wire_protocol.md §1`).

### ~~P2-6~~ · `evaluate()` returns dummy accuracy — **RESOLVED**
`PLUDOSClient.evaluate()` now deserialises the server's global booster from the
NumPy parameter array, runs inference on a time-ordered 80/20 held-out split of
the buffered Parquet data, and returns real accuracy. Returns `{"accuracy": 0.0}`
if fewer than 20 samples are available or no global model has been sent yet.

### P2-10 · sequence_id wrap — **RESOLVED**
`sequence_id` is `uint16` (max 65535). At 50 Hz a mission longer than ~22 min
would wrap the counter, causing the Parquet sort to place early packets (IDs near
0) after late packets (IDs near 65535) — corrupting the time series.
**Fix:** The gateway now detects wraps in real-time in `render_post`: when
`last_seq > 60000` and the new `sequence_id < 5000`, `_seq_wrap_counts[shuttle_id]`
is incremented. Every packet gets `sequence_monotonic = sequence_id + wrap_count × 65536`
stored in the packet dict. `_flush()` sorts by `sequence_monotonic` instead of
`sequence_id`. Wrap count resets on mission end.

### ~~P2-11~~ · Gateway buffer limits were global — **RESOLVED**
Old `BUFFER_SOFT_LIMIT` / `BUFFER_HARD_LIMIT` applied to the total count across
all shuttles. With 10+ active shuttles, hitting the soft limit flushed all
shuttles mid-mission, fragmenting Parquet files and breaking the "latest file"
assumption in `ai-worker`.
**Fix:** Replaced with per-shuttle limits:
`SHUTTLE_SOFT_LIMIT=3000` (≈1 min of MOVING at 50 Hz), `SHUTTLE_HARD_LIMIT=4500` (≈1.5 min),
`GATEWAY_HARD_LIMIT=100000`. Soft/hard flushes now affect only the shuttle that hit its
limit; other shuttles continue buffering normally. NOTE: now that MOVING is 50 Hz, these
counts cover ~5× less wall-clock time — revisit the defaults if missions need longer
unflushed windows.

### P2-12 · ai-worker trained on latest Parquet only — **RESOLVED**
`load_buffered_data()` previously loaded only the single newest `.parquet` file.
Under buffer-pressure flushes (P2-11), this could be a partial mission tail.
**Fix:** Loads the most recent `MAX_PARQUET_FILES` (default 20) files and
concatenates them. All mission data in the buffer window is included.

### ~~P2-7~~ · `mock_stm32.py` target IP hardcoded — **RESOLVED**
Default `TELEMETRY_HOST` is `127.0.0.1`. Override via `TELEMETRY_HOST` env var for remote Jetson targets.
TX periods updated to match firmware: `TX_PERIOD_MOVING_S=0.02` (50 Hz), `TX_PERIOD_IDLE_S=10.0` (0.1 Hz).

### ~~P2-8~~ · No `.env.example` for `server/` — **RESOLVED**
`server/.env.example` created with all required keys (`INFLUXDB_*`, `GRAFANA_*`,
`ALUMET_RELAY_PORT`). `env_file: .env` added to all three services in
`server/compose.yaml`. Hardcoded secrets removed from compose. `client/` side
was resolved as P1-5 in the previous session.

### ~~P2-4~~ · Energy-aware FL loop was open — **RESOLVED (ADR-014)**
The server now queries InfluxDB after each round for the maximum `energy_j`
from `fl_phases` (phase=`round_total`) and adapts `n_estimators` for the next
round. Control law: reduce by 2 when over `FL_ENERGY_BUDGET_J`, grow by 1 when
under 60% of budget. `n_estimators` is passed to clients via `fit_config()`.
`FL_ENERGY_BUDGET_J` defaults to `200.0 J` — **must be calibrated on hardware**.

### ~~P2-13~~ · CoAP/NC-UDP jitter analysis — **OBSOLETE under ADR-015**
The original entry analysed collision probability of the `IDLE_TRANSMIT_JITTER_MAX_MS` /
`NC_UDP_INTERVAL_MS` jitter windows. Both constants belong to the retired CoAP + NC-UDP
architecture and no longer exist in the firmware.

**Substantive concern survives** in a new form: under ADR-015, each shuttle streams raw
UDP at up to 50 Hz during MOVING. 100 shuttles × 50 Hz = 5,000 pkts/s arriving at one
Jetson. Profiling still required before claiming 100-shuttle capacity (see P2-14 below).

### P2-14 · Gateway capacity claim (100 shuttles) lacks analysis
`architecture.md` states "≥100 shuttles per gateway" but no profiling or
capacity analysis backs this. At 50 Hz × 100 shuttles = 5,000 pkts/s through
asyncio. Run `tools/mock_stm32.py` stress test (`MOCK_SHUTTLES=100`) and measure
CPU/memory before claiming this in the thesis. See Phase 4 in `next_steps.md`.

### P2-15 · Real federated FL not wired for single-Jetson compose
The unified `ai-worker` service (`client/compose.yaml`) selects mode from
`PLUDOS_MODE`. With no value it defaults to `federated` and blocks waiting for a
Flower SuperNode that the current client compose does **not** define — so the
single dev Jetson runs `PLUDOS_MODE=standalone` (local retrain) instead. Fine for
single-node work, but means we are **not** exercising the real multi-gateway FL
path (tree-set union, ADR-010) against the laptop server.

To wire real federated FL: add a `flower-supernode` service (or run an `flwr`
SuperNode on the Jetson host) that connects to the laptop SuperLink over
Tailscale, set `PLUDOS_MODE=federated`, bring up `--profile vpn`. Server side
(`server.py` ServerApp + `fl-trigger`) is already in place. Separate task — do
when validating the multi-gateway aggregation claim for the thesis.

### ~~Doc mismatch~~ · TX rates in docs vs firmware — **RESOLVED**
Firmware MOVING TX rate raised to **50 Hz** (`SAMPLE_PERIOD_MOVING_MS=20`), IDLE stays
0.1 Hz. ISM330 ODR raised 26→104 Hz with on-chip LPF2 (cutoff ODR/10 ≈ 10.4 Hz) so the
50 Hz stream is alias-free below the 25 Hz Nyquist — resolves the accel half of P1-A.
`wire_protocol.md`, `state_machine.md`, `architecture.md`, and `mock_stm32.py` updated to
50 Hz. Open follow-ups: (1) WiFi throughput ceiling is unmeasured — the synchronous UDP
send self-throttles, so confirm the achieved rate on hardware; (2) gyro LPF1 (CTRL6_C
FTYPE) left at default — tighten for full gyro anti-aliasing if needed.

### ~~Struct comment~~ · main.c `temp_c` sentinel comment wrong — **RESOLVED**
Struct header comment said `0x8000` for HTS221 unavailable (temp). Actual
`TELEMETRY_Send` code sends `0x7FFF` for all unavailable fields (accel, gyro, temp,
humidity). Comment fixed to match code and `wire_protocol.md §1` sentinel contract.

### ~~alumet-relay no profile~~ · alumet-relay always started — **SUPERSEDED**
Originally gated behind `profiles: [energy]` to avoid an unconditional 20–30 min
Rust build. That gate was later removed (ADR-011 Phase 2): `alumet-relay` now
runs in all profiles by design — its healthcheck gates `ai-worker`. Start with
`podman-compose up -d alumet-relay`.

### ~~P2-9~~ · Multi-shuttle flush — **RESOLVED**
`_critical_buf` and `_nc_buf` are now `dict[str, list[dict]]` keyed by
`shuttle_id`. Mission-end (`mission_active=0`) flushes only the keyed
sub-list via `_critical_buf.pop(shuttle_id, [])`. Size-limit flushes iterate
all shuttle sub-lists independently. Each `CriticalResource.render_post`
appends via `.setdefault(shuttle_id, []).append(pkt)`.
`NonCriticalProtocol.datagram_received` uses the same pattern for `_nc_buf`.

### P2-16 · PSRAM ring overwrite has no live-mission collision check — **DEFERRED**
`Capture_Service` writes raw FIFO words into the PSRAM ring at `cap_ring_wptr`,
which wraps at the 8 MB boundary with **no check** that the write position is
about to clobber a sealed-but-undrained mission, and `m->byte_count` is uncapped.
**Why it's safe today:** a single mission is ~1.3 MB ≪ 8 MB ring, and the 75 %
watermark safety flush (PHASE 2d) drains long before the ring fills. The overwrite
only becomes reachable after **repeated drain failures** pile up >6 MB of
undrained captures — and P1-7/P1-9 above now make that pile-up self-limiting
(skipped blast on no ack + cooldown back-off). **Not fixed** — flagged with an
in-code `DEFER` comment at the ring-write site.
**Fix (when needed):** track the oldest undrained mission's `start_offset` and
either stall capture or drop-oldest-with-a-counter when `cap_ring_wptr` would cross
it, so loss is explicit and counted rather than silent.

### P2-17 · Daily consolidation dedup key collides across STM reboots — *deferred to FL phase*
`data-engine.py::_consolidate_day` dedups merged `mission_s*` rows on
`(shuttle_id, seq)`, which collides across STM32 reboots (seq restarts at 0), so a
post-reboot mission can silently drop rows that share a seq with a pre-reboot one.
**Why it's safe today:** this path is **dead** — no `mission_s*` files are written
under ADR-021. The live drain path (`_consolidate_cap_day`) keys on
`(shuttle_id, mission_id, sample_index)` and is already collision-safe. Record-only;
revisit if the live `mission_s*` path is ever reactivated.
