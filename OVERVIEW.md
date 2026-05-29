# OVERVIEW — repository root (top-level map)

> Orientation for the whole repo. This explains the loose top-level files and
> how the folders fit the three-tier system. Each major folder has its own
> `OVERVIEW.md` with file-by-file detail. For agent rules, see `CLAUDE.md`.

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

### Code

| File | Responsibility | Weight |
|------|----------------|--------|
| `build_pludos_dashboard.py` | **Grafana dashboard generator.** Builds the "PLUDOS System Monitor" panel layout in Python, POSTs it to Grafana's API, and writes the JSON into `server/grafana/dashboards/`. This is the **source of truth** for that dashboard — edit it, don't hand-edit the generated JSON. | Helper (run on the laptop) |

### Project / build config

| File | Responsibility |
|------|----------------|
| `pyproject.toml` | The **Flower app definition** and Python deps. Declares the FL entry points: `serverapp = "server.server:app"` and `clientapp = "client.client:app"`, which `flwr run .` uses. Also the simulation/real-deployment federation notes. |
| `requirements.txt` | Top-level Python deps for laptop/dev use (containers have their own `requirements.txt`). |
| `.gitignore` | Excludes runtime data, secrets, venv, and build artifacts (see below). |
| `LICENSE` | Proprietary licence (© Gentjan Gjinalaj & Savoye SASU). |

### Human-facing docs (root)

| File | What it is | Tracked in git? |
|------|-----------|-----------------|
| `README.md` | Project landing page: architecture, energy stack, quickstart, tech stack. | **Yes** |
| `CHANGELOG.md` | Reverse-chronological change log, each entry mapped to an ADR or backlog item. | **Yes** |
| `OPERATIONS.md` | Day-to-day hardware cheatsheet (SSH into Jetson, container commands). | No (local) |
| `QUICKSTART.md` | Step-by-step simulation walkthrough for laptop-only runs. | No (local) |
| `CLAUDE.md` | Agent instructions for this repo (the root one + per-tier copies). | No (local) |
| `GEMINI.md` | **Parallel agent-context file for the Gemini CLI** — duplicates much of `CLAUDE.md`'s project context for a different AI tool. Likely stale relative to `CLAUDE.md`; reconcile or remove if you don't use Gemini. | No (local) |

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
