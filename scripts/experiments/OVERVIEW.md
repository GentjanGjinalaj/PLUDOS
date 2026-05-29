# OVERVIEW — scripts/experiments/ (thesis validation)

> Newcomer's map of the Phase 8 thesis-validation scripts.

## Why this folder exists

PLUDOS is a PhD project, and a thesis needs **evidence**. These four scripts
(the "T8" series) are the **post-hoc analysers** that turn data already
collected by the running system into the figures and tables that defend the
thesis claims — convergence, energy adaptation, anomaly-detector quality, and
fault-detection latency.

They are **not part of the production pipeline.** None of them drive hardware or
run during a live mission; they read InfluxDB / Parquet *after* a session and
produce a `.png` or `.csv`.

## The files

| File | Claim it tests | Inputs | Output |
|------|----------------|--------|--------|
| `t8_1_convergence.py` | Federated XGBoost converges, and warm-start helps | `fl_train_metrics` from InfluxDB | Plots: logloss vs round, `n_estimators` adaptation, cumulative trees |
| `t8_2_energy_ablation.py` | The energy budget actually shapes model growth | `fl_phases` + `fl_train_metrics`, across runs with different `FL_ENERGY_BUDGET_J` | Plot: `n_estimators` convergence per budget |
| `t8_3_anomaly_comparison.py` | CNN-AE vs IsolationForest quality | A Parquet file with a manually-annotated `ground_truth` column | Precision / recall / F1 table |
| `t8_4_fault_detection.py` | Federated detects a seeded fault faster than standalone | Two Parquet directories (standalone vs federated) + the fault-injection time | Time-to-detect comparison plot |

## How you actually use them

Each script's docstring carries the exact procedure. The common shape:

1. Run real FL/mission sessions (sometimes several, varying one parameter).
2. For T8.3 you must **hand-annotate** a fault session with a `ground_truth`
   column; for T8.4 you physically **seed a fault** (e.g. loosen a roller).
3. Run the script against the resulting data window / files.

## Weight

**Scaffolding for the thesis, not the system.** Deleting them breaks no
deployment — but you lose the reproducible analysis behind the thesis figures.
They depend on data shapes produced upstream: the `fl_*` InfluxDB measurements
(`server/server.py`, `client/client.py`) and the Parquet anomaly-label columns
(`client/anomaly.py`, `client/anomaly_cnn.py`). If those change, these scripts
need updating.
