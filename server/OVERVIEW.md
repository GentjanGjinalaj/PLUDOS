# OVERVIEW — server/ (central server)

> Newcomer's map of the central-server folder. For agent rules (FL aggregation,
> credentials, run commands) see `CLAUDE.md` in this directory. ADR-010
> (aggregation) and ADR-011 (energy) in `docs/decisions.md` give the "why".

## Why this folder exists

This is the **top tier**: the central server that coordinates federated
learning across all Jetson gateways (**STM32 → Jetson gateway → central
server**). It currently runs on a development laptop; later it moves to a
dedicated machine. It does two largely independent jobs:

1. **Orchestrate federated learning** — run the Flower server that collects
   each gateway's XGBoost trees, merges them into one global model, and sends
   it back.
2. **Store and visualise telemetry & energy** — InfluxDB holds the time-series
   data; Grafana draws the dashboards; Alumet measures the server's own energy.

The FL process and the monitoring stack are deliberately decoupled — Flower
trains models, InfluxDB/Grafana observe the system.

## The core file

| File | Responsibility | Weight |
|------|----------------|--------|
| `server.py` | **The Flower `ServerApp` (~340 lines).** Defines `XGBoostStrategy`, which overrides `aggregate_fit` to do **horizontal tree-set union** (ADR-010 Option A): concatenate every client's booster trees, re-sequence the tree IDs, validate the merged model, and broadcast it. Also builds the per-round `fit_config` (passes the round number so gateways can tag energy samples), and **persists each merged model** to `models/`. The entry point is `server:app`, picked up by `flwr run .`. | **Core / critical** — this *is* the FL server |
| `__init__.py` | Marks `server/` as a Python package so `flwr run .` can import `server:app`. | Scaffolding |

## The monitoring stack (`compose.yaml`)

Four containers, started with `podman-compose up` from this folder:

| Service | Image | Role |
|---------|-------|------|
| `influxdb` | InfluxDB 2.7 (`:8086`) | Time-series store for `fl_energy`, `fl_phases`, `stm_mission`, `gw_status`. Its healthcheck gates the others so they don't start before the bucket exists. |
| `grafana` | Grafana 10 (`:3000`) | Dashboards over InfluxDB. Provisioned as code from `grafana/` (anonymous viewer access on by default). |
| `alumet` | built from `alumet/` | Server-side energy profiler — RAPL for the server's own CPU, plus a gRPC relay-server that receives Jetson power streams (ADR-011). |
| `fl-trigger` | built from `trigger/` | Watches InfluxDB and launches `flwr run .` automatically when enough gateways are ready — no manual operator step. |

> Note: the **Flower server (`server.py`) is *not* a compose service.** It is a
> short-lived process that `fl-trigger` (or you, manually) launches with
> `flwr run .`. The containers are the always-on infrastructure around it.

## Subfolders (each has its own OVERVIEW.md)

- `trigger/` — the `fl-trigger` container's code → `server/trigger/OVERVIEW.md`
- `alumet/` — the server energy profiler container → `server/alumet/OVERVIEW.md`
- `grafana/` — dashboards-as-code → `server/grafana/OVERVIEW.md`

## Config & artifact directories (no application code)

- `systemd/pludos-server.service` — a **systemd user service** that brings the
  whole compose stack up at boot without a logged-in shell (install steps are
  in the file's header comment; relies on `loginctl enable-linger`). Weight:
  deployment helper.
- `models/` — **generated, gitignored.** After each successful round,
  `server.py` writes the merged booster to `global_model_round_<N>.ubj` and
  refreshes the `latest.ubj` symlink. This is a crash-recovery store, not
  source — load `latest.ubj` with `xgb.Booster.load_model()` to resume.

## How it all connects

```
Jetson gateways ──XGBoost trees──► server.py (Flower)  ──merged model──► back to gateways
                                       │                       │
                                       │                       └─► models/*.ubj (recovery)
fl-trigger ──(watches readiness)──► launches `flwr run .`
gateways ──gw_status / stm_mission──► InfluxDB ◄── alumet (energy) ──► Grafana dashboards
```

The readiness signals `fl-trigger` watches (`gw_status` heartbeats,
`stm_mission` writes) are produced by the gateway tier in `client/`. Energy
points in InfluxDB come from both this server's `alumet` container and the
Jetsons' `client/alumet-relay/` sidecars.
