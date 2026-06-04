# Rebuilding the Jetson gateway containers

Quick reference for the three things you actually do day-to-day. All commands
run **on the Jetson**, from `~/PLUDOS/client` (the directory with `compose.yaml`).

> The Python code (`data-engine.py`, `drain_receiver.py`, `client.py`, …) is
> **baked into the image** by `Containerfile` (`COPY … .`) — it is *not*
> bind-mounted. So any code change needs an **image rebuild**, not just a
> restart. Only `./ram_buffer` and `./state` are mounted live.

---

## 1. I changed a `.py` file → rebuild that service

`data-engine.py` / `drain_receiver.py` live in the **data-engine** service.
`client.py` / `anomaly*.py` live in the **ai-worker** service.

```bash
cd ~/PLUDOS && git pull
cd client
podman-compose up -d --build data-engine     # rebuild + recreate just this service
```

- **Always name the service** (`data-engine`). A bare `podman-compose up --build`
  rebuilds *everything*, including `alumet-relay`, which compiles Rust from
  source (~20–30 min) and is the usual cause of a "rebuild error".
- For an `ai-worker` change, it has a profile, so add it:
  `podman-compose --profile standalone up -d --build ai-worker`.
- The build is fast: `pip install` is a cached layer, only the small `COPY`
  layers re-run.

Verify it picked up the change:
```bash
podman logs --tail 15 pludos-data-engine        # fresh "starting" banner + bound ports
ss -ulpn | grep -E '568[34]'                     # 5683 + 5684 bound to the new pid
```

---

## 2. I just want to restart (no code change)

Picks up `.env` / config changes and clears in-memory state — no rebuild.

```bash
podman restart pludos-data-engine
```

Use this when, e.g., the receiver's in-memory dedup state needs clearing, or
after editing `.env`.

---

## 3. Full rebuild from scratch (rare)

Only when a base image or `requirements.txt` changed, or a layer is corrupt.
Note `alumet-relay`'s Rust compile makes this slow.

```bash
cd ~/PLUDOS && git pull
cd client
podman-compose build --no-cache data-engine      # force a clean rebuild of one image
podman-compose up -d data-engine
```

To rebuild the whole stack (expect the long `alumet-relay` Rust compile):
```bash
podman-compose --profile standalone build
podman-compose --profile standalone up -d
```

---

## Profiles cheat-sheet

`data-engine` has no profile (always runs). Everything else is gated:

| Service          | Profile(s)            |
|------------------|-----------------------|
| `data-engine`    | none (always)         |
| `ai-worker`      | `vpn`, `standalone`   |
| `alumet-relay`   | none (always)         |
| `influxdb-local` | `standalone`          |
| `grafana-local`  | `standalone`          |
| `tailscale`      | `vpn`                 |

So a plain `podman-compose up -d data-engine` works on a bare Jetson with no
tailnet. Add `--profile standalone` to bring up the local Influx/Grafana/ai-worker.
