# CLAUDE.md — server/

This is the central server tree. Currently runs on the development laptop;
eventually migrates to a dedicated server. Root `CLAUDE.md` applies; this
file adds server-specific context.

## What this tree is

Two components:

- `server.py`: Flower `ServerApp` that orchestrates federated learning rounds.
  Started via `flwr run .` from the project root.
- `compose.yaml`: Podman compose for InfluxDB + Grafana. Started separately
  from `server/` via `podman-compose up -d`.

The Flower server and the monitoring stack are independent processes. Flower
coordinates model training; InfluxDB stores energy metrics written by the
Jetson-side `AlumetProfiler`.

## Federated learning (server.py)

Current config:
- **3 rounds**, `min_fit_clients=1`, `min_available_clients=1`
- `fit_config` passes `server_round` to each client so the `AlumetProfiler`
  can tag energy samples by round in InfluxDB.
- `XGBoostStrategy(FedAvg)` overrides `aggregate_fit`.

**Current aggregation: horizontal tree-set union (ADR-010 Option A, Closed).**
`aggregate_fit` in `XGBoostStrategy` calls `_merge_boosters()`, which
concatenates all client booster trees, re-sequences tree IDs, and validates
the merged model before broadcast. Single-client rounds are a no-op passthrough.
See `docs/decisions.md` ADR-010 and `docs/future_options.md §1` for alternatives.

## Monitoring stack (compose.yaml)

- **InfluxDB 2.7** on `localhost:8086`
  - Org: `pludos`, Bucket: `alumet_energy`
  - Default token: `pludos-secret-token` — rotate this before any
    non-local deployment
- **Grafana** on `localhost:3000`
  - Default creds: `admin / admin` — change on first login

Credentials for both services go in `server/.env` (gitignored). Commit
`server/.env.example` showing the expected keys. See `docs/ANALYTICS.md`
for the Flux queries and dashboard setup.

## Running the server

```bash
# Start monitoring stack (InfluxDB + Grafana)
cd server
podman-compose up -d

# Start Flower server (separate process, from repo root)
flwr run .
```

The Flower `ServerApp` entry point is `server:app` in `pyproject.toml`.
`flwr run .` picks this up automatically.

## Energy data flow

1. Jetson's `AlumetProfiler` samples power at 10 Hz during `model.fit()`.
2. Writes to InfluxDB measurement `fl_energy`, tagged `fl_round=<n>`.
3. Grafana queries via Flux: filter by `fl_energy`, group by `fl_round`.
4. **Current state**: profiler writes mock values (random or hardcoded).
   Real Alumet integration is ADR-011 (`@docs/decisions.md`).

## Scaling notes

- Server is designed for 1:N topology — one server, many Jetson gateways.
- Currently `min_fit_clients=1` for dev. In multi-gateway deployment,
  raise this to the number of active Jetsons.
- Tailscale (configured in `client/compose.yaml`) is the VPN overlay
  between Jetson gateways and this server.

## Skill triggers

When touching federated learning strategy or aggregation logic, recheck
ADR-010 in `@docs/decisions.md` before editing. If the aggregation change
is non-trivial, use plan mode.
