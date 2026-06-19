# OVERVIEW — client/alumet-relay (Jetson power-measurement sidecar)

> Newcomer's map of the Jetson energy sidecar. Background: ADR-011 (Alumet
> integration) in `docs/decisions.md`. The `pludos-alumet` skill applies when
> editing anything here.

## Why this folder exists

PLUDOS is an **energy-aware** federated-learning project — the whole point of
the thesis is to measure what federated learning *costs in energy* on an edge
device. This sidecar is how the **Jetson measures its own power draw**.

It runs **Alumet** (an open-source energy-measurement framework from UGA/LIG)
as its own container, reads the Jetson's on-board **INA3221** power rails, and
publishes the numbers so the FL client (`client.py`'s `AlumetProfiler`) can tag
each training round with its energy cost.

This is **ADR-011 Phase 2** (ADR-011 closed). It is active on hardware — the
INA3221 is read from sysfs and all three output modes (Prometheus + InfluxDB +
CSV) were verified on the real Jetson on 2026-05-26.

## The files

This folder is just a container definition — two files, no application code.

| File | Responsibility | Weight |
|------|----------------|--------|
| `Containerfile` | Two-stage build. Stage 1 compiles `alumet-agent` **from Rust source** (pinned to alumet `v0.9.4`, with the `nvidia-jetson` plugin that reads the INA3221). Stage 2 is a minimal Debian runtime holding just the compiled binary + `entrypoint.sh`. **First build is slow (~20–30 min on the Jetson)**; later builds use the cache. | Helper / scaffolding (build recipe) |
| `entrypoint.sh` | The container's brain. Writes an Alumet TOML config, picks which output plugins to enable based on env vars, launches `alumet-agent`, and runs a **zero-power watchdog**. | Core of this sidecar |

## What `entrypoint.sh` does (in plain terms)

It always enables three Alumet plugins:

- **`jetson`** — reads the INA3221 rails at 1 Hz.
- **`prometheus-exporter`** — exposes the live readings at
  `localhost:9095/metrics`, which `client.py` scrapes during training.
- **`csv`** — writes every reading to a host-visible CSV log for inspection.

Then it picks **one** way to push to the central InfluxDB, decided purely by
`.env` (no rebuild needed to switch):

| Env state | Mode | Behaviour |
|-----------|------|-----------|
| `ALUMET_SERVER_ADDR` set | **relay** | streams over gRPC to the server's Alumet, which writes to InfluxDB |
| only `INFLUXDB_TOKEN` set | **direct** | Jetson pushes straight to server InfluxDB over HTTP (no Tailscale needed) |
| neither set | **local** | Prometheus + CSV only, no InfluxDB |

The **watchdog** (T7.2) tails the CSV; if it sees `ALUMET_ZERO_THRESHOLD`
consecutive zero power readings, it kills the agent so Podman's
`restart: unless-stopped` brings the container back — a self-heal for the case
where the INA3221 read silently stops producing values.

## Log housekeeping (eMMC growth control)

The CSV and the per-restart `.log` files live on the Jetson eMMC under
`client/logs/alumet/` and would otherwise grow without bound (the CSV reached
~330 MB in early data-collection runs). Alumet's `csv` plugin (v0.9.4) has **no
built-in rotation**, so `entrypoint.sh` handles it externally — no `logrotate`,
no new image dependency, no change to the energy-measurement logic.

- **CSV rotation (archive + agent restart).** The existing watchdog loop also
  checks the live CSV size each cycle. Past `ALUMET_CSV_MAX_MB` (default 200 MB,
  ≈ 2 days at 1 Hz) it snapshots the file to `alumet_readings_<timestamp>.csv`,
  then kills the agent so Podman restarts the container. The csv plugin
  **truncates `output_path` on open** (verified on hardware: the live file
  resets to ~0 on every agent start — it is *not* append-mode), so the restart
  reopens a fresh, empty CSV. An in-place truncate is deliberately avoided: it
  would leave a sparse file because the plugin holds its own write offset. The
  newest `ALUMET_CSV_KEEP` (default 3) archives are retained; older ones are
  deleted. *Caveat:* at most ~1 reading in the cp→restart window is lost
  (≈ once every 2 days); the restart also briefly drops `:9095`, so an FL round
  scraping at that exact moment falls back to DEGRADED.
- **Per-restart `.log` pruning.** Each container start writes a new
  `alumet-<timestamp>.log` (via `tee`). On startup the entrypoint keeps the
  newest `ALUMET_LOG_KEEP` (default 5, including the current run) and deletes the
  rest.

All three knobs are env-driven (`ALUMET_CSV_MAX_MB`, `ALUMET_CSV_KEEP`,
`ALUMET_LOG_KEEP`) — tune in `.env`, no rebuild needed.

## Weight and what breaks if it's gone

**Helper, not load-bearing for training.** If this sidecar is down:

- FL training still works — `client.py` falls back to a **DEGRADED** energy
  mode (no per-round power numbers).
- You lose the energy measurements that the thesis depends on, so for any real
  experiment it must be up. The compose **healthcheck** on this service gates
  `ai-worker` so the client doesn't scrape a dead endpoint.

## Relationships

```
INA3221 (Jetson HW) ─► alumet-agent (this sidecar)
                          ├─► :9095/metrics ──► client.py AlumetProfiler (per-round energy)
                          ├─► CSV log (host-visible, + zero-power watchdog)
                          └─► InfluxDB (relay or direct) ──► Grafana energy panels
```

This is the **gateway-side** energy path. The **server side** has a sibling
folder, `server/alumet/`, that measures the server's own power via Intel RAPL
(ADR-011 Phase 1). See `server/alumet/OVERVIEW.md`.
