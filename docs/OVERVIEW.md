# OVERVIEW — `docs/` (documentation map)

> Navigation hub for PLUDOS documentation. This file follows the repo's
> per-folder `OVERVIEW.md` convention (see root `OVERVIEW.md`). It groups every
> doc by **purpose and audience** so you can find the right one fast — it does
> **not** duplicate their content (link, don't copy).
>
> **Reading order for a newcomer:** root `README.md` → `SYSTEM_OVERVIEW.md` →
> the Reference doc for the tier you're touching.

---

## 🧭 Start here — orientation

| Doc | Purpose | Read if you… |
|-----|---------|--------------|
| [`SYSTEM_OVERVIEW.md`](SYSTEM_OVERVIEW.md) | Single-file blueprint — demo walkthrough + thesis-defence reference | …want the whole system in one read |
| [`PRESENTATION.md`](PRESENTATION.md) | Slide/bullet deck: features, how it works, the hard-won details | …are presenting or want the highlights |
| [`MODULARITY_AND_PIPELINE.md`](MODULARITY_AND_PIPELINE.md) | How the edge works: STM↔Jetson data pipeline, module boundaries | …are wiring the edge data path |
| [`glossary.md`](glossary.md) | Domain terms (shuttle, mission, drain, FSM, …) | …hit a term you don't know |

## 📐 Reference — authoritative specs (source of truth)

| Doc | Covers | Tier |
|-----|--------|------|
| [`architecture.md`](architecture.md) | Three-tier design + current implementation status | all |
| [`state_machine.md`](state_machine.md) | STM32 idle/moving FSM, thresholds, dwell/debounce | shuttle |
| [`wire_protocol.md`](wire_protocol.md) | Exact byte layouts: telemetry (24 B), DrainBegin (42 B), ARQ | shuttle↔gateway |
| [`sampling_strategy.md`](sampling_strategy.md) | Why each channel is sampled at its rate (3332/416/12.5 Hz) | shuttle |
| [`hardware_refs.md`](hardware_refs.md) | Boards, pins, memory map, IMU, datasheets, errata | shuttle |
| [`energy_lpm_design.md`](energy_lpm_design.md) | Stop2 deep-sleep + ISM330 wake-on-motion low-power design | shuttle |
| [`parquet_schema.md`](parquet_schema.md) | Parquet file families + every column (`cap_*` / live) | gateway |
| [`reliability.md`](reliability.md) | End-to-end packet-loss budget + recovery behaviour | all |
| [`distance_estimation.md`](distance_estimation.md) | 1D-ZUPT odometry (⚠ **OBSOLETE** — schema v4, kept for history) | gateway |
| [`decisions.md`](decisions.md) | **ADRs** — every design decision, rationale, alternatives, open questions | all |
| [`conventions.md`](conventions.md) | Code style for C / Python / containers | all |

## 🚀 Deploy & operate — how-to / runbooks

| Doc | Use for |
|-----|---------|
| [`DEPLOYMENT_GUIDE.md`](DEPLOYMENT_GUIDE.md) | Full step-by-step deploy for a new engineer with the hardware |
| [`DEPLOYMENT_3JETSON.md`](DEPLOYMENT_3JETSON.md) | The 3-Jetson × 6-STM dev-rig recipe specifically |
| [`NETWORK_SETUP.md`](NETWORK_SETUP.md) | WiFi + Tailscale networking |
| [`OPS_COMMANDS.md`](OPS_COMMANDS.md) | Field/monitoring command reference (the operator runbook) |
| [`DATA_GUIDE.md`](DATA_GUIDE.md) | Accessing and reading the Parquet data |
| [`ANALYTICS.md`](ANALYTICS.md) | InfluxDB + Grafana monitoring/profiling stack |

## 📋 Backlog & history

| Doc | Purpose |
|-----|---------|
| [`current_problems.md`](current_problems.md) | Tracked issues, P0/P1/P2 — fix P0 before new features |
| [`WIFI_FIX_AND_BUILD.md`](WIFI_FIX_AND_BUILD.md) | History of the EMW3080 WiFi EXTI-ISR fix (linked from 3 docs — keep) |
| [root `CHANGELOG.md`](../CHANGELOG.md) | Chronological change log |

---

## Local-only docs (gitignored — NOT in the repo)

These live on the owner's machine only because they contain **deployment
secrets** (Tailscale IPs, InfluxDB tokens, host credentials) or transient
working notes. Do **not** merge them into tracked docs — that would leak
credentials. Listed here only so their absence from the repo is intentional,
not an oversight: `DEPLOYMENT_CHECKLIST.md`, `JETSON_DEPLOYMENT.md`,
`QUICK_REFERENCE.md`, `DOWNLOAD.md`, `DESIGN_COUNCIL.md`, `future_options.md`,
`next_steps.md`, `THESIS_KEYPOINTS.md`.

## Known overlaps to resolve (housekeeping)

- **`DATA_GUIDE.md` ↔ `parquet_schema.md`** — both enumerate Parquet columns.
  Recommend: `parquet_schema.md` stays the authoritative column spec;
  `DATA_GUIDE.md` becomes the *how-to-read-the-data* task guide and links to it
  instead of repeating the schema.
- **`SYSTEM_OVERVIEW.md` ↔ `architecture.md`** — heavy conceptual overlap by
  design (one is the narrated blueprint, the other the spec). Keep both; prefer
  cross-links over duplicated diagrams when either is edited.
