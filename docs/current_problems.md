# Known Problems & Backlog

Tracked issues with priority levels. Fix P0 before merging new features;
fix P1 before any non-local deployment.

- **P0** — Blocking (prevents correct operation)
- **P1** — Important (degrades correctness or safety)
- **P2** — Nice-to-have (quality / future work)

---

## P0 — Blocking

*No open P0 items.*

---

## P1 — Important

### P1-2 · Jetson IP hardcoded in firmware (Flexibility)
**File:** `Core/Src/main.c`
`JETSON_IP` is hardcoded as a string literal. If the Jetson IP changes
(DHCP lease renewal, different network), the firmware must be recompiled
and reflashed.
**Fix (short-term):** Move to `wifi_credentials.h` alongside SSID/password.
**Fix (long-term):** Implement beacon discovery (STM32 listens for UDP
broadcast from gateway on port 5000). Currently stubbed — see P2-1.

### ~~P1-3~~ · CoAP retry contradicts design intent — **RESOLVED**
Decision made: keep the manual application-layer retry loop (4 attempts,
2/4/8/16 s backoff). The `mx_wifi` BSP does not expose RFC 7252 native
retransmission; the manual loop provides equivalent behaviour with explicit
per-attempt UART logging. Documented in ADR-012 in `docs/decisions.md`.

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

---

## P2 — Nice-to-have / Future Work

### P2-1 · Beacon discovery — gateway side done, STM32 side pending
**Gateway:** `_broadcast_beacon()` now sends `PLUDOS-GW:<ip>` to
255.255.255.255:5000 every `BEACON_INTERVAL_S` seconds (default 10).
The gateway IP is either set via `GATEWAY_IP` env var or auto-detected.
**Limitation:** Broadcast only escapes the container bridge when
`network_mode: host` is set for the `data-engine` service in `compose.yaml`.
With the current bridge networking the beacon is confined to the container
network. Set `GATEWAY_IP` and `network_mode: host` on the Jetson to activate.
**STM32 side:** Firmware still uses `JETSON_IP` directly (separate session).
When the STM32 is updated to listen on UDP 5000, no gateway changes are needed.

### P2-2 · ADC power sensing — estimate implemented, real measurement pending (Energy accuracy)
**File:** `Core/Src/main.c`
`power_mw` is now computed by `POWER_EstimateMilliwatts()` using datasheet
current figures (STM32U585 DS13259 §6.3.7 + MXCHIP EMW3080 §5.2) and the
current WiFi/FSM state. This replaces the fixed `150.0f` placeholder and gives
a meaningful ±40% estimate without any hardware changes.
**Remaining gap:** No ADC is wired to a current-sense shunt. The board has no
on-board power monitor IC reachable by the MCU (unlike the Jetson's INA3221).
**Fix (long-term):** Add an INA219 on the 3.3V rail to I2C1 (I2C2 is
full), configure in CubeMX, implement `INA219_ReadPowerMilliwatts()` to replace
the estimate. Alternatively, calibrate the estimate constants against a bench
ammeter measurement during development.

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

### P2-5 · ~~Temperature/humidity sensors not read~~ — **RESOLVED**
HTS221 driver in `Core/Src/sensors.c`; LPS22HH pressure driver added in same
session. Both on I2C2, no CubeMX changes needed. UDP non-critical packet now
carries `temp_c`, `humidity_pct`, and `pressure_hpa` (30 bytes).

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

### P2-11 · Gateway buffer limits were global — **RESOLVED**
Old `BUFFER_SOFT_LIMIT` / `BUFFER_HARD_LIMIT` applied to the total count across
all shuttles. With 10+ active shuttles, hitting the soft limit flushed all
shuttles mid-mission, fragmenting Parquet files and breaking the "latest file"
assumption in `ai-worker`.
**Fix:** Replaced with per-shuttle limits (`SHUTTLE_SOFT_LIMIT=400`,
`SHUTTLE_HARD_LIMIT=600`) plus a gateway-wide emergency ceiling
(`GATEWAY_HARD_LIMIT=50000`). Soft/hard flushes now affect only the
shuttle that hit its limit; other shuttles continue buffering normally.

### P2-12 · ai-worker trained on latest Parquet only — **RESOLVED**
`load_buffered_data()` previously loaded only the single newest `.parquet` file.
Under buffer-pressure flushes (P2-11), this could be a partial mission tail.
**Fix:** Loads the most recent `MAX_PARQUET_FILES` (default 20) files and
concatenates them. All mission data in the buffer window is included.

### P2-7 · `mock_stm32.py` target IP hardcoded (Developer experience)
**File:** `tools/mock_stm32.py`
Default `COAP_SERVER` falls back to a hardcoded IP (`10.187.8.48`).
**Fix:** Change default to `127.0.0.1` so running locally works without setting
any environment variables. Keep the env var override.

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
`FL_ENERGY_BUDGET_J` defaults to `50.0 J` — **must be calibrated on hardware**.

### P2-13 · Random jitter window unspecified (Traffic analysis gap)
`state_machine.md` documents that STM32 adds a random pre-transmit delay on
IDLE entry, but the jitter window size is not specified or analysed. With 100
shuttles simultaneously entering IDLE, the collision probability depends directly
on this window. The `#define` name and default value are in `main.c` but absent
from the docs. Before the multi-shuttle stress test (Phase 4), document and
justify the window size with a back-of-envelope collision analysis.

### P2-14 · Gateway capacity claim (100 shuttles) lacks analysis
`architecture.md` states "≥100 shuttles per gateway" but no profiling or
capacity analysis backs this. At 50 Hz × 100 shuttles = 5,000 CoAP packets/s
through asyncio. Run `tools/mock_stm32.py` stress test and measure CPU/memory
before claiming this in the thesis. See Phase 4 in `next_steps.md`.

### ~~P2-9~~ · Multi-shuttle flush — **RESOLVED**
`_critical_buf` and `_nc_buf` are now `dict[str, list[dict]]` keyed by
`shuttle_id`. Mission-end (`mission_active=0`) flushes only the keyed
sub-list via `_critical_buf.pop(shuttle_id, [])`. Size-limit flushes iterate
all shuttle sub-lists independently. Each `CriticalResource.render_post`
appends via `.setdefault(shuttle_id, []).append(pkt)`.
`NonCriticalProtocol.datagram_received` uses the same pattern for `_nc_buf`.
