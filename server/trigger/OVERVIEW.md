# OVERVIEW — server/trigger (FL auto-launcher)

> Newcomer's map of the `fl-trigger` container. Readiness contract is in
> `docs/DEPLOYMENT_GUIDE.md §3.8`.

## Why this folder exists

Federated-learning rounds used to need a human to type `flwr run .` at the
right moment — once the gateways had collected fresh data. This folder turns
that into an **autonomous watcher**: it polls InfluxDB, decides when enough
gateways are ready, and launches the round itself. It is the `fl-trigger`
container in `server/compose.yaml`.

## The files

| File | Responsibility | Weight |
|------|----------------|--------|
| `trigger.py` | **The watcher loop (~330 lines).** Every `FL_TRIGGER_INTERVAL_S` it checks InfluxDB for gateway readiness; when at least `FL_MIN_FIT_CLIENTS` gateways are ready it runs `flwr run .` against the bind-mounted repo, parses the round number and per-client accuracy from the output, and writes a `last_run.json` summary. A **pidfile** prevents a double-launch if the container restarts mid-round (stale pidfiles auto-reclaimed). A **heartbeat** file, touched every loop, backs the container healthcheck. | **Core** — unattended FL depends on it |
| `Containerfile` | Builds the container: `python:3.11-slim`, installs `trigger.py`'s deps, creates the state dir, and wires a `HEALTHCHECK` that fails if the heartbeat is older than 2 minutes. | Helper (build recipe) |
| `requirements.txt` | Python deps (chiefly `influxdb-client`). | Helper |

## What counts as "ready"

Either signal is enough (deduplicated per gateway):

- a recent **`gw_status`** heartbeat (written by `client/client.py` at startup
  and after each evaluate) — an active gateway with a Parquet buffer; or
- a **`stm_mission`** write (from `client/data-engine.py` on every mission-end
  flush) — fresh data has arrived since the last run.

## State (the `fl_trigger_state` volume)

Survives container restarts:

- `trigger.pid` — the active `flwr run` pid (or stale, auto-reclaimed)
- `last_run.json` — most recent round summary (round, exit code, participants,
  accuracy) — inspect with `podman exec pludos-fl-trigger cat /app/state/last_run.json`
- `logs/round_<ts>.log` — full `flwr` stdout/stderr per round
- `heartbeat` — touched each tick; backs the healthcheck

## Relationships

```
client/* ──gw_status / stm_mission──► InfluxDB ──(polled)──► trigger.py
                                                                │
                                                                ▼
                                                   `flwr run .` → server/server.py
```

`trigger.py` never imports `server.py` — it launches it as a subprocess inside
the read-only project mount. It is the automation layer *around* the Flower
server, not part of it.
