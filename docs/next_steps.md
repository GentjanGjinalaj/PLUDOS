# Next Steps & Roadmap

Phased plan for bringing PLUDOS from the current prototype to a
thesis-defensible system. Fix P0/P1 issues first, then advance the phases.

See `current_problems.md` for the full backlog and `decisions.md` for open
research questions.

---

## Phase 0 — Fix Blocking Issues (Do first)

These prevent correct operation or represent security risks.

- [ ] **P0-1** Fix `jetson_ip` undeclared buffer in `main.c` (compile blocker)
- [ ] **P1-1** Move WiFi credentials to `Core/Inc/wifi_credentials.h` (gitignored)
- [ ] **P1-5** Create `client/.env.example` and `server/.env.example`
- [ ] **P1-6** Pin Tailscale image version in `client/compose.yaml`

---

## Phase 1 — Firmware Completeness

Get the STM32 to a state where all thesis claims are backed by real code.

- [ ] Configure ADC for power sensing in CubeMX → implement
      `ADC_ReadPowerMilliwatts()` (P2-2)
- [ ] Add I2C2 driver for HTS221/SHT41 (temperature/humidity) (P2-5)
- [ ] Resolve CoAP retry strategy: keep manual loop or RFC 7252? Document
      in `decisions.md` (P1-3)
- [ ] Implement or formally defer beacon discovery (P2-1)
- [ ] Test full mission cycle end-to-end: IDLE → MOVING → buffer fill →
      flush → CoAP ACK → IDLE

---

## Phase 2 — Real Energy Measurement (ADR-011)

Replace the placeholder `AlumetProfiler` with real Alumet integration.
This is critical for the thesis energy-awareness claim.

- [ ] Install Alumet on the Jetson; verify INA3221 / tegrastats access
- [ ] Replace `random.uniform` energy mock with Alumet relay client
- [ ] Validate InfluxDB shows real power curves during FL training
- [ ] Measure baseline (IDLE gateway), data ingestion, and training costs
- [ ] Compare `nvpmodel` modes (7 W / 15 W / 25 W) and document impact

See the `pludos-alumet` skill and ADR-011 in `decisions.md`.

---

## Phase 3 — Real Federated Aggregation (ADR-010)

Replace the "select largest booster" placeholder with a real aggregation strategy.
Required before the thesis can claim federated XGBoost as a contribution.

- [ ] Literature review: horizontal tree-set union, distillation, Flower's
      built-in XGBoost support
- [ ] Choose and document strategy in ADR-010 (`decisions.md`)
- [ ] Implement chosen strategy in `XGBoostStrategy.aggregate_fit`
- [ ] Test with ≥ 2 Jetson gateways and verify global model accuracy
- [ ] Measure aggregation energy cost on the server side

---

## Phase 4 — Multi-Gateway Deployment

Scale from 1 Jetson (dev) to ≥ 2 Jetsons (thesis evaluation setup).

- [ ] Deploy data-engine on 2–3 Jetson Orin Nano units
- [ ] Configure Tailscale on all gateways and the server
- [ ] Run FL rounds with multiple clients and verify strategy works
- [ ] Stress test: simulate 100+ shuttles via `tools/mock_stm32.py` against
      one gateway; measure memory, CPU, and latency
- [ ] Raise `min_fit_clients` in `server.py` to match actual gateway count

---

## Phase 5 — Research Evaluation

Collect the thesis data.

- [ ] Energy-aware FL: compare energy cost per round across NVPModel modes
- [ ] SRAM-pressure flush: measure impact on gateway-side buffer occupancy
      vs. fixed-interval flush (is SRAM-driven triggering better?)
- [ ] Predictive maintenance baseline: train on real shuttle vibration data,
      measure anomaly detection accuracy
- [ ] Compare against literature baselines for federated IoT / FL on edge

---

## Thesis Contribution Checklist

Before submitting, confirm each claimed contribution is backed by code:

| Claim | Status | Evidence |
|---|---|---|
| Energy-aware federated learning | ❌ Placeholder | ADR-011 |
| SRAM-pressure-driven flush trigger | ✅ Implemented | `main.c` FSM |
| CoAP CON for critical data | ✅ Implemented | `main.c`, `wire_protocol.md` |
| Federated XGBoost aggregation | ❌ Selection only | ADR-010 |
| Energy tagging per FL round | ❌ Mock values | ADR-011 |
| Multi-shuttle scalability | ❌ Untested at scale | Phase 4 |
