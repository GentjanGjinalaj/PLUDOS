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
| `dashboards/pludos_system_monitor.json` | The actual **"PLUDOS System Monitor" dashboard** (~680 lines): live-status KPIs, environment, drain quality/packet-loss, vibration, idle + mission waveforms, Jetson power, federated-learning, and mission-history panels. Reads the **local** `efmtstss6rcw0d` datasource. | Core artifact — the **single hand-maintained source of truth** |
| `provisioning/datasources/influxdb_jetson.yaml` | A **second datasource** (`uid: jetson-influx`) pointing at a warehouse Jetson's standalone InfluxDB over Tailscale (`http://100.119.83.35:8086`). Lets the laptop Grafana see live field data that the standalone Jetson only writes locally. Token from `INFLUXDB_ADMIN_TOKEN` in `.env`. | Per-warehouse remote view |
| `dashboards/pludos_edge_pipeline_jetson.json` | **"PLUDOS Edge Pipeline (warehouse1)"** — a 1:1 mirror of the Jetson's own `client/grafana/dashboards/pludos_edge_pipeline.json` (the regulated edge dashboard), with every datasource ref swapped to `jetson-influx`. This is how you view warehouse1's edge panels from the laptop. | Per-warehouse dashboard (warehouse1) |
| `dashboards/pludos_jetson.json` | Older system-monitor-style mirror of warehouse1 on `jetson-influx` (`uid: pludos-jetson`). **Superseded** by `pludos_edge_pipeline_jetson.json`; kept only to avoid breaking a saved link. Candidate for removal. | Legacy |

## Per-warehouse dashboards (multi-Jetson)

The laptop Grafana is the **single pane of glass** over every Jetson. Each
standalone Jetson writes shuttle/drain data only to its *own* local InfluxDB, so
the laptop reaches each one through a dedicated remote datasource over Tailscale.
The per-warehouse dashboard is a verbatim copy of that Jetson's regulated
`client/grafana/dashboards/pludos_edge_pipeline.json` with the datasource uid
re-pointed — same panels, same queries, same layout, just a different data
source. **The Jetson's `pludos_edge_pipeline.json` is the source of truth; the
laptop copies are derived from it** (regenerate after the client dashboard
changes, don't hand-edit the copy).

**To regenerate warehouse1's copy** after the client dashboard changes:

```bash
python3 - <<'PY'
import json
raw = open("client/grafana/dashboards/pludos_edge_pipeline.json").read()
raw = raw.replace("efmtstss6rcw0d", "jetson-influx")   # local -> warehouse1 remote
d = json.loads(raw)
d["uid"]   = "pludos-edge-jetson"
d["title"] = "PLUDOS Edge Pipeline (warehouse1)"
json.dump(d, open("server/grafana/dashboards/pludos_edge_pipeline_jetson.json","w"), indent=2)
PY
```

**To add warehouse2** (when a second Jetson exists), repeat with new uids:

1. New datasource `provisioning/datasources/influxdb_jetson2.yaml` → `uid: jetson2-influx`, `url: http://<warehouse2-tailscale-ip>:8086`.
2. New dashboard via the snippet above but swap to `jetson2-influx`, `uid: pludos-edge-jetson2`, title `...(warehouse2)`.

The file provider loads any JSON under `/dashboards` and re-scans every 30 s, so
new warehouse dashboards appear without a restart.

## How the dashboard is maintained

`dashboards/pludos_system_monitor.json` is the **single source of truth** and is
edited by hand (or exported from the Grafana UI). Every panel query targets the
measurements actually written today (ADR-021 drain path): `stm_mission`
(`source == "drain"`), `stm_idle_wave`, `stm_mission_wave`, `input_current` / `input_voltage`,
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
