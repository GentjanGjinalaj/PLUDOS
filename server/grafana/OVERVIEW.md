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
| `dashboards/pludos_system_monitor.json` | The actual **"PLUDOS System Monitor" dashboard** (~680 lines): live-status KPIs, environment, drain quality/packet-loss, vibration, idle waveforms, Jetson power, federated-learning, and mission-history panels. | Core artifact — the **single hand-maintained source of truth** |

## How the dashboard is maintained

`dashboards/pludos_system_monitor.json` is the **single source of truth** and is
edited by hand (or exported from the Grafana UI). Every panel query targets the
measurements actually written today (ADR-021 drain path): `stm_mission`
(`source == "drain"`), `stm_idle_wave`, `input_current` / `input_voltage`,
`fl_phases`, and `fl_train_metrics`.

```
server/grafana/dashboards/pludos_system_monitor.json   (source of truth, edit by hand)
        │  loaded by
        ▼
provisioning/dashboards/pludos.yaml  ──► Grafana panels
```

**To change a panel:** edit the committed JSON directly, or export from the
Grafana UI and overwrite the file, then commit. The provisioning loader
re-scans every 30 s, so a container restart is not required.

> A Python generator (`build_pludos_dashboard.py`) once produced this JSON, but
> it diverged from the hand-tuned file and queried dead pre-ADR-021 measurements,
> so it was deleted (2026-06). The JSON is now maintained directly.

## Dashboard rows at a glance (slide caption)

**The PLUDOS System Monitor reads top-to-bottom as the data's journey from
shuttle to model.** **◉ Live Status** gives four at-a-glance KPIs — drains
received in the last 24 h, the most recent drain's packet-loss and accel peak,
and live Jetson board power. **🌡 Environment** plots on-board temperature and
pressure per shuttle. **📦 Drain Quality & Volume** shows per-drain packet loss
(the Phase-1 reliability metric) and how many accelerometer samples each
idle/mission drain delivered. **📈 Vibration Intensity** charts per-drain accel
RMS/peak and gyro peak for each shuttle — the headline health signal for
predictive maintenance. **〰 Idle Waveforms** drills into the raw per-sample idle
snapshots. **🖥 Jetson Power** breaks out INA3221 board power, current and
voltage from the energy profiler. **⚡ Federated Learning** reports energy per FL
round, per-phase durations, and training quality (logloss + anomaly rate).
**📋 Mission History** is the audit table of every drain in the last 24 h. The
rows are ordered to mirror the pipeline: capture → transport quality → derived
signal → device energy → learning → record.

## Relationships

This folder only draws pictures of data that other tiers produce. The numbers
come from InfluxDB (`server/compose.yaml`), written by the energy profilers
(`server/alumet/`, `client/alumet-relay/`) and by the gateway
(`client/data-engine.py` mission summaries, `client/client.py` heartbeats).
