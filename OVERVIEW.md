# OVERVIEW — repository root (top-level map)

> Orientation for the whole repo. This explains the loose top-level files and
> how the folders fit the three-tier system. Each major folder has its own
> `OVERVIEW.md` with file-by-file detail.

## What PLUDOS is (the whole system in one paragraph)

PLUDOS is a three-tier, energy-aware federated-learning system for
predictive-maintenance monitoring of Savoye XTPS warehouse shuttles. Each
shuttle carries an **STM32U585 board (Tier 1)** that reads its IMU, decides
**IDLE vs MOVING** with a small state machine, and streams compact 24-byte
telemetry over 2.4 GHz Wi-Fi — while during motion it also captures
high-rate vibration into on-board PSRAM and drains those bursts to the
gateway afterwards. A **Jetson Orin Nano gateway (Tier 2)** ingests that data
over UDP, buffers it to Parquet, labels anomalies, and trains a local XGBoost
model. A **central server (Tier 3)** runs Flower to merge every gateway's
model into one shared model — raw data never leaves the edge — while
InfluxDB + Grafana track energy and health. Data flows **sensor → gateway
Parquet → local model → federated global model**, and the project measures
the energy cost at every hop.

## The three tiers (where the real code lives)

```
STM_Shuttles/PLUDOS_Edge_Node/   Tier 1 — STM32 firmware on each shuttle
client/                          Tier 2 — Jetson Orin Nano gateway
server/                          Tier 3 — central server (FL + monitoring)
```

Supporting folders:

```
scripts/             calibration tools (turn captured data into constants)
scripts/experiments/ thesis-validation analysers (the "T8" series)
tools/               mock_stm32.py — fake shuttle fleet for no-hardware testing
docs/                committed reference docs (architecture, ADRs, wire protocol)
```

Quick links to the per-folder guides:
`STM_Shuttles/PLUDOS_Edge_Node/OVERVIEW.md` ·
`client/OVERVIEW.md` · `client/alumet-relay/OVERVIEW.md` ·
`server/OVERVIEW.md` · `server/trigger/OVERVIEW.md` ·
`server/alumet/OVERVIEW.md` · `server/grafana/OVERVIEW.md` ·
`scripts/OVERVIEW.md` · `scripts/experiments/OVERVIEW.md` · `tools/OVERVIEW.md`.

## Loose top-level files

### Project / build config

| File | Responsibility |
|------|----------------|
| `pyproject.toml` | The **Flower app definition** and Python deps. Declares the FL entry points: `serverapp = "server.server:app"` and `clientapp = "client.client:app"`, which `flwr run .` uses. Also the simulation/real-deployment federation notes. |
| `requirements.txt` | Top-level Python deps for laptop/dev use (containers have their own `requirements.txt`). |
| `.gitignore` | Excludes runtime data, secrets, venv, and build artifacts (see below). |
| `LICENSE` | Proprietary licence (© Gentjan Gjinalaj & Savoye SASU). |

### Human-facing docs (root)

| File | What it is |
|------|-----------|
| `README.md` | Project landing page: architecture, energy stack, quickstart, tech stack. |
| `CHANGELOG.md` | Reverse-chronological change log, each entry mapped to an ADR or backlog item. |

## Runtime directories (gitignored — data, not source)

You will see these appear when the system runs; none are committed:

- `ram_buffer/`, `client/ram_buffer/`, `client/ram_buffer_archive/` — Parquet
  telemetry buffers written by `data-engine.py`, read by `client.py`.
- `server/models/` — persisted global XGBoost models (`*.ubj`) written after
  each FL round.
- `logs/`, `client/logs/` — run logs (including the Alumet CSV).
- `__pycache__/`, `pludos_venv/` — Python bytecode and the virtualenv.

## Trees intentionally without an OVERVIEW.md

- `docs/` — already the canonical reference docs; explained by its own contents.
- `.claude/` — Claude Code skills and config.
- `.github/`, `.vscode/`, `.git/` — CI, editor, and VCS metadata.
- Inside the firmware: `Drivers/`, `Debug/`, `.settings/` are vendor/generated
  (see the firmware OVERVIEW).

## How a packet flows through the repo

```
main.c (firmware)
   │ 24-byte UDP :5683
   ▼
client/data-engine.py ──► ram_buffer/*.parquet
   │                              │
   │                  client/anomaly*.py (labels)
   │                              ▼
   │                  client/client.py ──XGBoost──► server/server.py (Flower)
   │                                                      │
   └─ mission summaries ─► InfluxDB ◄─ alumet (energy) ──► server/grafana dashboards
                              ▲
                  server/trigger auto-launches `flwr run .`
```
