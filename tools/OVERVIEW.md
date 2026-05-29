# OVERVIEW — tools/ (local test driver)

> Newcomer's map of the repo-root tooling folder.

## Why this folder exists

You can't always have real STM32 shuttles on the bench. This folder holds the
**hardware simulator** that lets you exercise the entire gateway pipeline
without flashing a single board — essential for developing and stress-testing
`client/data-engine.py` on a laptop.

## The file

| File | Responsibility | Weight |
|------|----------------|--------|
| `mock_stm32.py` | **Fake shuttle fleet.** Spawns N parallel virtual shuttles, each cycling IDLE → MOVING → long-IDLE forever, and streams real **24-byte `PludosTelemetry` v3** packets over raw UDP to a gateway — byte-for-byte matching what the firmware sends. Self-contained: just `asyncio` + `socket`, no `aiocoap`. | Dev/test helper |

## How you use it

```bash
# Single shuttle against a local data-engine
python tools/mock_stm32.py

# Six shuttles in one process — stress-test the multi-shuttle path
MOCK_SHUTTLES=6 python tools/mock_stm32.py

# Point at a real remote Jetson
TELEMETRY_HOST=192.168.1.50 MOCK_SHUTTLES=2 python tools/mock_stm32.py
```

Env vars tune the fleet: `MOCK_SHUTTLES`, `FIRST_SHUTTLE_ID`, `MISSION_S`,
`IDLE_S`, `POST_MISSION_IDLE_S` (set ≥ 30 to trigger a gateway mission-end
flush), plus `TELEMETRY_HOST` / `TELEMETRY_PORT`.

## Weight and the contract it must honour

**Helper, not shipped** — but the **most-used dev tool in the repo**. It is the
no-hardware path for everything downstream of ingest.

Critically, it is **one of three places that must agree on the wire format**:

```
main.c  (PludosTelemetry_t, the real firmware)
mock_stm32.py  (TELEMETRY_FMT = "<BHIBhhhhhhhh", 24 B — this file)
data-engine.py (_unpack_telemetry)
```

The script even `assert`s the packet is 24 bytes and mirrors the firmware's TX
cadence (10 Hz MOVING, 0.1 Hz IDLE) and int16 scaling. If you change the packet
in `main.c`, you must update this mock too, or your laptop tests will silently
diverge from real hardware. `docs/wire_protocol.md §1` is the spec all three
follow.

> Note: a second small listener, `STM_Shuttles/PLUDOS_Edge_Node/tools/coap_udp_monitor.py`,
> lives in the firmware tree — that one *receives* and prints packets, whereas
> `mock_stm32.py` *sends* them.
