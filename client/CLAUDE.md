# CLAUDE.md — client/

This is the Jetson Orin Nano gateway tree. Containerised Python that
ingests CoAP/UDP from shuttles, buffers in tmpfs, trains XGBoost via
Flower. Root `CLAUDE.md` applies; this file adds gateway-specifics.

## What this tree is

Two Python services managed by `compose.yaml` under Podman:

- `data-engine` (`data-engine.py`): CoAP server on UDP 5683, parses
  `CriticalPayload` from shuttles, sorts by `(shuttle_id, sequence_id)`,
  writes Parquet on mission-end or buffer-full. tmpfs volume.
- `ai-worker` (`client.py`): Flower client. Loads latest Parquet,
  trains XGBoost, sends booster bytes to central server. Gated behind
  `--profile vpn` so dev runs don't need Tailscale.

Plus an optional `tailscale` sidecar for joining the gateway to the
central server's tailnet.

Container image is built from `Containerfile` on top of
`nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3`.

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

`data-engine.py` runs on `aiocoap` (asyncio). Never block the event
loop:

- File writes via `aiofiles` if they're hot-path; the current
  PyArrow `to_parquet` is sync but only fires on flush, which is
  OK for now.
- DB writes (InfluxDB) — current code uses synchronous `write_api`
  in a thread, which is fine.
- Heavy CPU (XGBoost fit) lives in `client.py` (separate service,
  separate container, blocking is fine there).

## Buffer policy (data-engine)

- Hard limit: 500 packets in process memory.
- Soft flush: 80% (400 packets).
- Mission-end flush: any packet with `mission_active = 0` triggers
  sort + Parquet write + buffer clear.
- NTP offset: set once per shuttle on first packet, **never refreshed**
  (P1-4 in `current_problems.md`). Don't propose changes to the offset
  computation without reading that issue first.

## Federation (server.py side)

The current `XGBoostStrategy.aggregate_fit` selects the largest booster
(`max(streams, key=len)`) — this is **not** federated aggregation. It's
a placeholder for ADR-010 in `@docs/decisions.md`. If I ask you to
"improve the aggregation," that's a research task, not a quick fix —
walk through the candidates in ADR-010 first.

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

`mock_stm32.py` (referenced in QUICKSTART.md but currently missing —
P0-7 in current_problems) is the local-laptop test driver. It should
emit CoAP CON packets matching `@docs/wire_protocol.md` so the
data-engine can be exercised without hardware. If I ask you to write
or fix it, build the wire format directly (no aiocoap dependency, just
raw `socket.SOCK_DGRAM`) so it stays a single-file script.

## Skill triggers

When you see Podman compose changes, container builds, or Jetson
deployment work, the `pludos-podman-jetson` skill applies.
