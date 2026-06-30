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
| `mock_stm32.py` | **Fake shuttle fleet.** Spawns N parallel virtual shuttles, each cycling IDLE → MOVING → long-IDLE forever. Exercises **both gateway ingest paths**: (1) the live **24-byte `PludosTelemetry` v3** telemetry stream on `:5683`, and (2) the high-rate **PSRAM drain** on `:5684` (`DRAIN_BEGIN` ×3 → CRC32 `CHUNK`s → `DRAIN_END` ×3, proto v2) emitted at each MOVING→IDLE boundary, plus a 12.5 Hz idle snapshot — byte-for-byte matching what the firmware sends per `docs/wire_protocol.md §1` (telemetry) and `§2` (drain). It does **not** simulate OTA firmware update. Self-contained: just `asyncio` + `socket` (+ `zlib` for the drain CRC32), no `aiocoap`. | Dev/test helper |

## How you use it

```bash
# Single shuttle against a local data-engine
python tools/mock_stm32.py

# Six shuttles in one process — stress-test the multi-shuttle path
MOCK_SHUTTLES=6 python tools/mock_stm32.py

# Point at a real remote Jetson (telemetry + drain follow TELEMETRY_HOST by default)
TELEMETRY_HOST=192.168.1.50 MOCK_SHUTTLES=2 python tools/mock_stm32.py

# Short mission, fake shuttle id 99 — keeps the run off the S1–S6 dashboards
FIRST_SHUTTLE_ID=99 MISSION_S=8 DRAIN_CAPTURE_S=2 python tools/mock_stm32.py
```

Env vars tune the fleet: `MOCK_SHUTTLES`, `FIRST_SHUTTLE_ID`, `MISSION_S`,
`IDLE_S`, `POST_MISSION_IDLE_S` (set ≥ 30 to trigger a gateway mission-end
flush), plus `TELEMETRY_HOST` / `TELEMETRY_PORT`.

Drain (`:5684`) knobs: `DRAIN_HOST` / `DRAIN_PORT` (default to the telemetry
target), `DRAIN_ENABLE`, `DRAIN_CAPTURE_S` (mission FIFO span to drain),
`IDLE_SNAPSHOT_ENABLE` / `IDLE_SNAP_S` (the 12.5 Hz at-rest snapshot),
`CHUNK_PAYLOAD_BYTES`, `DRAIN_CHUNK_GAP_MS`.

> No-pollution tip: run with a fake `FIRST_SHUTTLE_ID` (e.g. 99) that is outside
> the real fleet. The mock still lands in `pludos-data-engine` logs and writes
> `cap_*_s99_*` Parquet + an `stm_mission` row, but the Grafana panels filter
> S1–S6 so it never shows on the dashboards. Clean up afterwards with an InfluxDB
> predicate delete (`shuttle_id="99"`) and `rm ram_buffer/cap_*_s99_*.parquet`.

## Weight and the contract it must honour

**Helper, not shipped** — but the **most-used dev tool in the repo**. It is the
no-hardware path for everything downstream of ingest.

Critically, it must **agree with the firmware and the gateway on two wire
formats** — the live telemetry packet and the high-rate drain:

```
# Telemetry (:5683), docs/wire_protocol.md §1
main.c  (PludosTelemetry_t, the real firmware)
mock_stm32.py  (TELEMETRY_FMT = "<BHIBhhhhhhhh", 24 B — this file)
data-engine.py (_unpack_telemetry)

# Drain (:5684), docs/wire_protocol.md §2
main.c / psram.c (DRAIN_BEGIN / CHUNK / END framing)
mock_stm32.py  (BEGIN/CHUNK/END structs + "<Bhhh" FIFO words — this file)
drain_receiver.py (MissionReassembler, _demux_fifo, _parse_packet)
```

The script `assert`s the telemetry packet is 24 bytes and emits a continuous
live stream (50 Hz MOVING, 0.1 Hz IDLE) on `:5683`, plus a chunked CRC32 drain
blast on `:5684` after each MOVING run. Note the live stream is a **test-harness
convenience**: real firmware no longer streams continuously (ADR-021
capture-and-drain), so the drain path is the closer match to real-hardware
behaviour. If you change the telemetry packet in `main.c` **or** the drain
framing in `main.c` / `psram.c`, you must update this mock too, or your laptop
tests will silently diverge from real hardware.

> Note: a second small listener, `STM_Shuttles/PLUDOS_Edge_Node/tools/coap_udp_monitor.py`,
> lives in the firmware tree — that one *receives* and prints packets, whereas
> `mock_stm32.py` *sends* them.
