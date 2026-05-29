# OVERVIEW — scripts/ (calibration tools)

> Newcomer's map of the calibration scripts. These turn captured data into the
> magic numbers the live system uses.

## Why this folder exists

Several constants in PLUDOS were initially **guesses** (the comment in the
firmware literally says "0.05 is a guess"). These scripts replace guesses with
**measured values**: you capture a real run, point the script at it, and it
prints the recommended constant to paste into the relevant `.env` or `#define`.

They are **operator/dev tools**, run by hand, off the live data path. Nothing in
the running system imports them.

## The files

| File | Calibrates | How | Feeds |
|------|-----------|-----|-------|
| `calibrate_movement_threshold.py` | `MOVEMENT_THRESHOLD_G2` (the IDLE→MOVING trigger in firmware) | Reads an IDLE Parquet and a MOVING Parquet, computes `mean(idle_mag²) + 5σ` | The `#define MOVEMENT_THRESHOLD_G2` in `main.c` |
| `calibrate_energy_budget.py` | `FL_ENERGY_BUDGET_J` (energy-aware FL adaptation) | Queries the last N rounds of `fl_phases` from InfluxDB; budget = `margin × mean(round_total)` | The `FL_ENERGY_BUDGET_J` env in `server/.env` |

## Weight

**One-off / occasional helpers.** What breaks if they vanish? Nothing at
runtime — but you lose the principled way to re-derive these constants when the
hardware, fixture, or shuttle changes, and you'd be back to guessing.

## Relationships

```
captured Parquet / InfluxDB ──► scripts/*.py ──► "RECOMMENDED <CONST>=<value>"
                                                        │ (you paste it in)
                                                        ▼
                          main.c (#define) / data-engine.py / server/.env
```

Each calibration script deliberately **mirrors** a piece of live logic (e.g.
`calibrate_movement_threshold.py` recomputes the same `mag²` deviation the
firmware FSM uses) so its recommendation matches what the system will actually
do. If you change that live logic, update the mirror here too. The
thesis-validation scripts live one
level down in `scripts/experiments/` (see that folder's OVERVIEW.md).
