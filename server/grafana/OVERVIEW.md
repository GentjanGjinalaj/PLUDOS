# OVERVIEW — server/grafana (dashboards-as-code)

> Newcomer's map of the Grafana provisioning folder. Flux queries and panel
> details are in `docs/ANALYTICS.md`.

## Why this folder exists

Grafana is how the project *sees* its data — energy per FL round, mission
timelines, per-shuttle phase durations. This folder makes those dashboards
**reproducible**: instead of clicking panels together by hand and losing them
when the container is recreated, the datasource and dashboards are checked in
as files and auto-loaded at container start.

The `grafana` service in `server/compose.yaml` mounts this folder read-only.

## The files

| File | Responsibility | Weight |
|------|----------------|--------|
| `provisioning/datasources/influxdb.yaml` | Tells Grafana, on boot, to wire up the **InfluxDB datasource** (Flux query language, org `pludos`, bucket `alumet_energy`, token from `.env`). Fixed `uid` so dashboards can reference it. | Core (without it, panels have no data source) |
| `provisioning/dashboards/pludos.yaml` | A **file provider** that tells Grafana to load every dashboard JSON it finds under `/dashboards` and re-scan every 30 s. | Core (loader) |
| `dashboards/pludos_system_monitor.json` | The actual **"PLUDOS System Monitor" dashboard** (~1480 lines of generated JSON): energy, mission, and phase panels. | Core artifact — but **generated, don't hand-edit** (see below) |

## Important: who generates the dashboard JSON

`dashboards/pludos_system_monitor.json` is **not meant to be edited by hand.**
It is produced by the repo-root script **`build_pludos_dashboard.py`**, which
builds the panel layout in Python, POSTs it live to Grafana's API, *and* writes
the JSON back into this folder. The provisioning loader then serves that file
on the next container start.

```
build_pludos_dashboard.py  (source of truth, run on the laptop)
        │  writes
        ▼
server/grafana/dashboards/pludos_system_monitor.json
        │  loaded by
        ▼
provisioning/dashboards/pludos.yaml  ──► Grafana panels
```

**To change a panel:** edit `build_pludos_dashboard.py` and re-run it, or edit
in the Grafana UI and re-export through the script. Editing the JSON directly
works until the next regeneration overwrites it — so treat the script as
authoritative.

## Relationships

This folder only draws pictures of data that other tiers produce. The numbers
come from InfluxDB (`server/compose.yaml`), written by the energy profilers
(`server/alumet/`, `client/alumet-relay/`) and by the gateway
(`client/data-engine.py` mission summaries, `client/client.py` heartbeats).
