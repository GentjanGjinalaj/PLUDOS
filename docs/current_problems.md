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

### P1-3 · CoAP retry contradicts design intent (Architecture)
**File:** `Core/Src/main.c`
The firmware implements a manual application-layer retry loop (2/4/8/16 s
exponential backoff, 4 attempts). The original CLAUDE.MD said "rely on
native RFC 7252 binary exponential backoff." These duplicate each other.
**Fix:** Decision needed — keep manual loop (predictable) or replace with
RFC 7252 CoAP library retransmission. Document the choice in `decisions.md`.

### P1-4 · NTP offset never refreshed (Data quality)
**File:** `client/data-engine.py`
The per-shuttle NTP offset (`receipt_ms - tick_ms`) is computed once on
the first packet and never updated. STM32 crystal drift (~±50 ppm) will
cause timestamp error accumulation over long missions.
**Fix:** Refresh the offset every N packets (e.g., every 100). Requires
careful handling of mid-mission offset jumps in the Parquet sort logic.

### P1-5 · `server/.env.example` and `client/.env.example` missing (Ops)
New deployers have no template showing which env vars are required.
**Fix:** Create `.env.example` files for both `client/` and `server/`
listing all required keys with safe placeholder values. These are committed;
the real `.env` files are gitignored.

### ~~P1-6~~ · Tailscale image pinned — **RESOLVED**
`tailscale/tailscale:v1.66.0` in `client/compose.yaml`. Update the tag deliberately when upgrading.

---

## P2 — Nice-to-have / Future Work

### P2-1 · Beacon discovery stubbed (Zero-touch provisioning)
**File:** `client/data-engine.py` — `broadcast_beacon` task sleeps indefinitely.
The gateway never broadcasts its IP. The STM32 uses `JETSON_IP` directly.
**Fix:** Implement UDP broadcast from gateway on port 5000 at startup. Have
the STM32 listen before attempting CoAP connection.

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

### P2-3 · AlumetProfiler writes mock values (Energy research)
**File:** `client/client.py`
`AlumetProfiler` writes `random.uniform(25, 45)` W in TEST_MODE and `12.0` W
in production. No real sensor data reaches InfluxDB.
**Fix:** Replace with real Alumet relay integration. See ADR-011 in
`decisions.md` and the `pludos-alumet` skill.

### P2-4 · XGBoost "aggregation" is selection, not federation (Research validity)
**File:** `server/server.py`
`XGBoostStrategy.aggregate_fit` picks the largest booster. This is not
federated averaging. The thesis cannot claim federated XGBoost until this
is replaced with a real aggregation strategy.
**Fix:** Implement one of the candidates in ADR-010 (`decisions.md`). Requires
a literature review and experimental design before coding.

### P2-5 · ~~Temperature/humidity sensors not read~~ — **RESOLVED**
HTS221 driver in `Core/Src/sensors.c`; LPS22HH pressure driver added in same
session. Both on I2C2, no CubeMX changes needed. UDP non-critical packet now
carries `temp_c`, `humidity_pct`, and `pressure_hpa` (30 bytes).

### P2-6 · `evaluate()` returns dummy accuracy (Metrics)
**File:** `client/client.py`
`PLUDOSClient.evaluate()` returns hardcoded `(0.95, num_examples, {})`.
No actual model evaluation on a held-out set.
**Fix:** Split the loaded Parquet into train/test; compute XGBoost prediction
accuracy on the test set and return real metrics.

### P2-7 · `mock_stm32.py` target IP hardcoded (Developer experience)
**File:** `tools/mock_stm32.py`
Default `COAP_SERVER` falls back to a hardcoded IP (`10.187.8.48`).
**Fix:** Change default to `127.0.0.1` so running locally works without setting
any environment variables. Keep the env var override.

### P2-8 · No `.env.example` for `server/` or `client/` (Documentation)
See P1-5 above — elevated from P2 because missing templates block new contributors.
