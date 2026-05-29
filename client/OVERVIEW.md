# OVERVIEW — client/ (Jetson gateway)

> Newcomer's map of the gateway folder. For agent rules (async discipline,
> buffer policy, Jetson SSH workflow) see `CLAUDE.md` in this directory.

## Why this folder exists

This is the **middle tier**: the Jetson Orin Nano gateway that sits between the
shuttles and the central server (**STM32 → Jetson gateway → central server**).
One Jetson "adopts" one or more shuttles. Its jobs:

1. **Ingest** the raw 24-byte UDP telemetry the shuttles broadcast, anchor each
   packet's relative timestamp to wall-clock time, and **buffer it to Parquet
   files** — one file per shuttle per mission.
2. **Label** that data for anomalies (vibration / bearing-fault signatures).
3. **Train an XGBoost model** locally and ship the trees to the central server
   as a Flower federated-learning client.
4. **Measure its own energy** while training (Alumet / INA3221), so the project
   can study the energy cost of federated learning.

Everything here runs in **Podman containers** defined by `compose.yaml`.

## The Python modules

| File | Responsibility | Weight |
|------|----------------|--------|
| `data-engine.py` | **The ingest service (~1120 lines).** An `asyncio` UDP listener on `:5683`. Unpacks each `PludosTelemetry` packet, keeps a per-shuttle in-memory buffer, computes derived columns (magnitudes, rolling vibration stats, ZUPT distance, mission/phase segmentation), and **flushes one Parquet file per shuttle** on mission-end (30 s idle after a moving run) or buffer pressure. Also broadcasts the `PLUDOS-GW:<ip>` discovery **beacon** on `:5000` so shuttles know where to send, and writes mission summaries to InfluxDB. | **Core / critical** — nothing works without this |
| `client.py` | **The Flower FL client (~750 lines).** Loads recent Parquet files, calls the anomaly module to get labels, trains XGBoost, and sends the booster bytes to the server each round. Contains `AlumetProfiler` (samples power during `fit()`), a gateway-readiness **heartbeat** writer, and a **standalone retrain loop** for when no central server is reachable. | **Core / critical** — this is the "AI worker" |
| `anomaly.py` | **Anomaly label generator (IsolationForest path).** Loads Parquet, builds a 5-feature vibration view (`accel_mag`, rolling accel std, `gyro_x`, `gyro_mag`, `accel_z`), and produces the pseudo-labels XGBoost trains on. Has **no Flower dependency** — importable standalone. Backend is selected by the `ANOMALY_MODEL` env var. | Core (feeds training labels) |
| `anomaly_cnn.py` | **Alternative anomaly labeller: a 1D-CNN autoencoder** (~6 K params, replaced an earlier LSTM). Uses 6 raw axes, **Welford running stats persisted across FL rounds**, and an IDLE-baseline reconstruction-error threshold. Selected via `ANOMALY_MODEL` instead of IsolationForest. | Core (alternative labeller) |
| `__init__.py` | Marks `client/` as a Python package. | Scaffolding |

> **Why two anomaly modules?** They are two interchangeable ways to generate
> the same thing — anomaly pseudo-labels for XGBoost. `anomaly.py` is the
> statistical IsolationForest approach; `anomaly_cnn.py` is the deep
> reconstruction approach. The `ANOMALY_MODEL` env var picks one at runtime;
> neither is dead code. `client.py` calls into whichever is active.

## Container & dependency files

- `compose.yaml` — **defines up to six services across deployment profiles**
  (the `PLUDOS_MODE` profiles let one file serve federated, standalone, and
  headless deployments):
  - **always on:** `data-engine` (ingest), `alumet-relay` (power sidecar).
  - **`ai-worker`** (the `client.py` FL client) — runs in `vpn` and
    `standalone` profiles.
  - **`tailscale`** sidecar — `vpn` profile only (joins the server's tailnet).
  - **`influxdb-local` + `grafana-local`** — `standalone` profile only; a local
    time-series store + dashboard for when no central server exists.
  - Most services use `network_mode: host` (needed for the UDP broadcast beacon
    and to reach the relay's Prometheus endpoint on `localhost:9095`).
- `Containerfile` — the image both `data-engine` and `ai-worker` are built from
  (Python 3.10-slim base, CPU XGBoost for now).
- `requirements.txt` — Python deps for that image.

## Runtime directories (gitignored — not source)

You'll see these on a running Jetson; they are **data, not code**, and are not
committed:

- `ram_buffer/` — where `data-engine.py` writes Parquet files and where
  `client.py` reads them. A bind-mount shared between the two containers.
- `ram_buffer_archive/` — older/rotated Parquet, kept for inspection.

Subfolder with its own explainer:

- `alumet-relay/` — the INA3221 power-measurement sidecar (ADR-011 Phase 2).
  See `client/alumet-relay/OVERVIEW.md`.

## How it all connects

```
shuttles ──UDP :5683──► data-engine.py ──► ram_buffer/*.parquet
        ◄─beacon :5000──┘                        │
                                                 ▼
                          anomaly.py / anomaly_cnn.py  (labels)
                                                 │
                                                 ▼
                            client.py  ──XGBoost trees──► server (Flower)
                                  │
                            AlumetProfiler ◄── alumet-relay :9095 (power)
```

`data-engine.py` and `client.py` never call each other directly — they
communicate **only through the Parquet files** in `ram_buffer/`. That decoupling
is deliberate: ingest must never block on training. The packet format
`data-engine.py` unpacks must stay in lock-step with the firmware struct in
`main.c` and the mock in `tools/mock_stm32.py`.
