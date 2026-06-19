# CLAUDE.md — client/

This is the Jetson Orin Nano gateway tree. Containerised Python that
ingests raw UDP telemetry from shuttles (ADR-015 v2), buffers to disk-
backed Parquet, trains XGBoost via Flower. Root `CLAUDE.md` applies;
this file adds gateway-specifics.

## What this tree is

Three services managed by `compose.yaml` under Podman:

- `data-engine` (`data-engine.py`): UDP listener on `:5683` for the unified
  24-byte `PludosTelemetry` v3 stream (ADR-016). Per-shuttle in-memory buffer; flushes
  one Parquet file per shuttle on mission-end (30 s of state==IDLE after a
  MOVING run) or on per-shuttle buffer pressure. Also broadcasts the
  beacon (`PLUDOS-GW:<ip>[:csv-ids]`) on `:5000`.
- `ai-worker` (`client.py`): Flower client. Loads recent Parquet files
  (`MAX_PARQUET_FILES`, default 20), trains XGBoost, sends booster bytes
  to the central server. Gated behind `--profile vpn` so dev runs don't
  need Tailscale.
- `alumet-relay`: sidecar that runs `alumet-cli` for INA3221 power
  measurement (ADR-011, closed). Active on hardware — INA3221 read from
  sysfs, all three output modes (Prometheus + InfluxDB + CSV) verified
  2026-05-26.

Plus an optional `tailscale` sidecar (also behind `--profile vpn`) for
joining the gateway to the central server's tailnet.

Container image for `data-engine` and `ai-worker` is built from
`Containerfile` on top of `python:3.10-slim`. (The earlier
`nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3` base is tracked as a
follow-up — CPU-only XGBoost for now.)

## Hardware target

- Jetson Orin Nano Super Developer Kit, 8 GB module
- Ampere GPU, 1024 CUDA cores + 32 Tensor cores, 67 TOPS
- 6-core Cortex-A78AE @ up to ~1.7 GHz
- 7–25 W power envelope (matters for energy-aware adaptation)
- JetPack r35.x, Ubuntu 22.04 base
- Power monitor: INA3221 on the module (read via `tegrastats` or
  `/sys/bus/i2c/.../in_power*_input`)

## Working with a remote Jetson

I can give you SSH access to a physical Jetson during dev. Workflow:

```bash
# From my laptop, you run via bash tool:
ssh jetson "podman ps"
ssh jetson "podman logs -f pludos-data-engine"
ssh jetson "cd ~/PLUDOS && git pull && cd client && podman-compose up --build -d data-engine"
```

You don't have an SSH agent of your own; you use `bash` to invoke `ssh`
from the laptop where you're running. Don't store keys, don't generate
keys, don't add `Host` blocks — I configure SSH on my side.

For commands likely to take more than 60 seconds (image build, FL
training run), background them or pipe to `tee` so logs survive even
if the SSH session times out. For long-running observation, I'd rather
you advise me to open a tmux session and paste the logs back than have
you sit on a blocked tool call.

## Async/await discipline

`data-engine.py` runs on a plain `asyncio` UDP `DatagramProtocol`. Never block the event
loop:

- File writes: PyArrow `to_parquet` is sync but only fires on a mission-
  end / buffer-pressure flush — acceptable latency spike.
- DB writes: InfluxDB `stm_mission` summary writes are dispatched on a
  fire-and-forget daemon thread (`_write_mission_summary`); the event
  loop is never blocked by InfluxDB I/O.
- Heavy CPU (XGBoost fit) lives in `client.py` (separate service,
  separate container, blocking is fine there).

## Buffer policy (data-engine)

- Per-shuttle soft limit: `SHUTTLE_SOFT_LIMIT` (default 3000 packets, ≈1 min
  at 50 Hz MOVING) — proactive flush, mission continues buffering.
- Per-shuttle hard limit: `SHUTTLE_HARD_LIMIT` (default 4500 packets, ≈1.5 min)
  — emergency mid-mission flush.
- Gateway-wide ceiling: `GATEWAY_HARD_LIMIT` (default 100 000) across all
  shuttles combined. Last-resort safety valve.
- Mission-end flush: detected purely on the gateway. After a run of state
  ==MOVING packets, when the shuttle stays in state==IDLE for
  `MISSION_END_IDLE_S` (default 30 s), that shuttle's buffer is sorted by
  `sequence_monotonic` and written to one Parquet file. There is no
  `mission_active` end-marker on the wire — the firmware (post-ADR-015)
  doesn't transmit one.
- NTP offset (live `:5683` stream only): established on the first packet per
  shuttle; refreshed every `NTP_REFRESH_INTERVAL` packets (default 100) to
  bound STM32 crystal drift. Reset on mission-end. Sort key is
  `sequence_monotonic` not `timestamp_ms`. The high-rate drain path does
  *not* use this offset — see the drain-receiver section below.

## Drain receiver (data-engine, UDP `:5684`)

High-rate PSRAM captures (ADR-020/021) arrive on a separate port from the
live hot loop and are reassembled by `drain_receiver.py` into
`cap_accel_*` / `cap_gyro_*` Parquet files, distinct from the live telemetry
files. Key behaviours:

- **Self-timed timestamps (no NTP offset):** each `DrainBegin` (42 bytes, proto v2)
  carries `t0_tick_ms` and `tx_tick_ms`. Capture age = `tx_tick - t0_tick`
  (same-boot `HAL_GetTick`, exact), so capture wall-clock =
  `BEGIN_arrival - capture_age`. The old per-shuttle NTP-offset / boot-anchor
  machinery was removed — volatile PSRAM means both ticks are same-boot, so
  there is no reboot ambiguity.
- **IDLE-only settling trim** (`IDLE_TRIM_MS`, default 1000): the ISM330 LPF2
  resets on ODR change, so the first ~1 s of an idle snapshot clips at the
  ±2 g rail; those samples are dropped off the head and `t0` advanced to keep
  timestamps honest. MOVING streams are *not* trimmed — their onset transient
  is real signal.
- **`[STORAGE]` write log:** each drain Parquet write logs filename,
  shuttle/mission tag, sample count (and any settling-trim count), and file
  size — a separate write from the `[INFLUXDB]` summary line, so both are
  visible.
- **Off-loop finalisation:** mission finalisation (the sync `to_parquet`
  write) runs in a worker thread via `run_in_executor`, not on the asyncio
  loop. A wake that drains several missions back-to-back (e.g. a recovered
  mission + a fresh one after a watchdog reset) would otherwise stall: the
  blocking write delays the *next* mission's BEGIN-ack past the shuttle's
  ack-wait budget, so the shuttle skips that mission's blast and the queue
  stays one drain behind forever. The reassembler is popped from `missions`
  before the write is scheduled, so no other path touches it concurrently.
- **TTL dedup (`DEDUP_TTL_S`, default 10 s):** the firmware `mission_id` resets
  to low values on every STM32 reset, so it is unique only within one boot
  session. A finalised `(shuttle_id, mission_id)` is held in `recent_done` for
  `DEDUP_TTL_S` — late duplicate packets of a just-finalised drain (arrive in
  ~seconds) are dropped, but a post-reset drain re-using the same `mission_id`
  (tens of seconds later) is accepted as new. Expired entries are pruned so the
  map can't grow unbounded. Parquet filenames / Influx use the gateway-assigned
  monotonic `gw_mission_id` (unix-ms), never the firmware id.

## Multi-Jetson pairing

When more than one Jetson runs on the same WiFi (e.g. the 3-Jetson dev
rig — see `docs/DEPLOYMENT_3JETSON.md`), set `SHUTTLE_GROUP=1,2` (comma-
separated IDs) per Jetson. This (a) appends a `:<csv-ids>` suffix to the
beacon so STMs bond only when their `SHUTTLE_ID` is in the list, and
(b) drops out-of-group packets at ingress as defence-in-depth. Empty
`SHUTTLE_GROUP` = accept all (single-Jetson dev default).

## Federation (server.py side)

`XGBoostStrategy.aggregate_fit` implements horizontal tree-set union
(ADR-010 Option A — closed). Each client's booster trees are concatenated,
tree IDs are re-sequenced, and the merged booster is validated with
`xgb.Booster.load_model()` before broadcast. Single-client rounds are a
no-op passthrough.

For runs with all 3 Jetsons online, export `FL_MIN_FIT_CLIENTS=3` so the
Flower server waits for all three gateways before starting round 1.

## Container hygiene

- Pin base images. Don't use `:latest`.
- Named volumes only. `shared_ram_buffer` is tmpfs; `tailscale_data`
  is persistent.
- `profiles: [vpn]` gates anything that needs Tailscale, so plain
  `podman-compose up data-engine` works on a fresh Jetson without a
  tailnet joined.
- Credentials (`INFLUXDB_TOKEN`, `TS_AUTHKEY`) come from `.env` files,
  never hardcoded in YAML. The `.env` files are gitignored; commit
  `.env.example` showing the expected keys.

## Tests / mocks

`tools/mock_stm32.py` (at repo root) is the local-laptop test driver. It
emits raw UDP `PludosTelemetry` packets (24 B, `<BHIBhhhhhhhh`) matching
`@docs/wire_protocol.md §1`. Set `MOCK_SHUTTLES=N` to spawn N parallel
shuttles in one process — useful for exercising the gateway with the
6-shuttle dev rig before any hardware is flashed. Default target is
`127.0.0.1:5683`; override via `TELEMETRY_HOST` / `TELEMETRY_PORT`.

Keep the mock self-contained — no `aiocoap`, just raw `socket.SOCK_DGRAM`
on a per-shuttle async task.

## Python commenting rules

One `#` comment line above every function and every non-obvious block.
Keep it to one line. State the purpose, contract, or constraint — not
what the next line of code does. Examples:

```python
# Write sorted packets atomically; os.replace() is crash-safe on Linux.
def flush_to_parquet(buffer: list[dict], path: Path) -> None: ...

# Per-shuttle offset: anchors STM32 relative tick to gateway NTP time.
offset_ms = receipt_time_ms - tick_ms

# Hard limit: drop oldest packets if gateway RAM fills beyond 500 entries.
if len(self.ram_buffer) >= MAX_BUFFER_SIZE:
    ...
```

Async gotcha — always comment why a call is safe to block on (or why
it's on an executor):
```python
# PyArrow write is sync but fires only on flush — acceptable latency spike.
table.to_parquet(tmp_path)
```

## Skill triggers

When you see Podman compose changes, container builds, or Jetson
deployment work, the `pludos-podman-jetson` skill applies.
