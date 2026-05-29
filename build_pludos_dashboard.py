"""
Build and push the PLUDOS System Monitor dashboard to Grafana.
Run on the laptop: python3 /tmp/build_pludos_dashboard.py
"""
import json
import pathlib
import urllib.request
import urllib.error
import base64

GF_URL   = "http://localhost:3000"
GF_USER  = "admin"
GF_PASS  = "admin"
DS_UID   = "efmtstss6rcw0d"
BUCKET   = "alumet_energy"

DS = {"type": "influxdb", "uid": DS_UID}

def gf_post(path, payload):
    data  = json.dumps(payload).encode()
    creds = base64.b64encode(f"{GF_USER}:{GF_PASS}".encode()).decode()
    req   = urllib.request.Request(
        f"{GF_URL}{path}", data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {creds}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def gf_put(path, payload):
    data  = json.dumps(payload).encode()
    creds = base64.b64encode(f"{GF_USER}:{GF_PASS}".encode()).decode()
    req   = urllib.request.Request(
        f"{GF_URL}{path}", data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {creds}"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

def gf_get(path):
    creds = base64.b64encode(f"{GF_USER}:{GF_PASS}".encode()).decode()
    req   = urllib.request.Request(
        f"{GF_URL}{path}",
        headers={"Authorization": f"Basic {creds}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

# ── Ensure datasource token is set ──────────────────────────────────────────
print("Patching datasource token...")
ds_full = gf_get(f"/api/datasources/uid/{DS_UID}")
ds_full["secureJsonData"] = {"token": "pludos-secret-token"}
ds_full.pop("secureJsonFields", None)
resp, code = gf_put(f"/api/datasources/{ds_full['id']}", ds_full)
print(f"  datasource patch: HTTP {code}")

# ── Query helpers ────────────────────────────────────────────────────────────
def flux(q): return q.strip()

def ts_q(field, measurement="stm_telemetry", extra="", agg="mean", ref="A"):
    return {"refId": ref, "query": flux(f"""
from(bucket: "{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}"{extra})
  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: false)
  |> yield(name: "{ref}")"""), "datasource": DS}

def last_q(field, measurement="stm_telemetry", extra="", ref="A"):
    return {"refId": ref, "query": flux(f"""
from(bucket: "{BUCKET}")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}"{extra})
  |> last()
  |> yield(name: "{ref}")"""), "datasource": DS}

def raw_q(flux_str, ref="A"):
    return {"refId": ref, "query": flux(flux_str), "datasource": DS}

# ── Panel builder ────────────────────────────────────────────────────────────
pid_seq = [1]
def new_panel(ptype, title, targets, gridPos, options=None, fieldConfig=None):
    p = {"id": pid_seq[0], "type": ptype, "title": title,
         "datasource": DS, "targets": targets, "gridPos": gridPos}
    if options:     p["options"]     = options
    if fieldConfig: p["fieldConfig"] = fieldConfig
    pid_seq[0] += 1
    return p

def ts_defaults(unit="short", lw=2, fill=0, min_=None, max_=None, draw="line", step=False):
    custom = {"lineWidth": lw, "fillOpacity": fill, "drawStyle": draw,
              "lineInterpolation": "stepAfter" if step else "linear",
              "showPoints": "never", "spanNulls": False}
    d = {"unit": unit, "custom": custom}
    if min_ is not None: d["min"] = min_
    if max_ is not None: d["max"] = max_
    return {"defaults": d}

def stat_defaults(unit, color="blue", thresholds=None):
    if thresholds is None:
        fc = {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color}}}
    else:
        fc = {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                           "thresholds": {"mode": "absolute", "steps": thresholds}}}
    return fc

# ── Build panels ─────────────────────────────────────────────────────────────
panels = []
y = 0

# ─── ROW: section header rows ────
def row(title, y):
    p = {"id": pid_seq[0], "type": "row", "title": title, "collapsed": False,
         "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}}
    pid_seq[0] += 1
    return p

# ═══════════════════════════════════════════════════════════════
# ROW 0 — Status strip (6 stat panels)
# ═══════════════════════════════════════════════════════════════
panels.append(row("◉ Live Status", y)); y += 1

stat_specs = [
    # (title, targets, unit, thresholds_or_color, w, x)
    ("Shuttle 1 — State",
     [last_q("state", extra=' and r.shuttle_id == "1"')],
     "short", "blue", 4, 0),
    ("Shuttle 2 — State",
     [last_q("state", extra=' and r.shuttle_id == "2"')],
     "short", "blue", 4, 4),
    ("Shuttle 1 — TX Rate (Hz)",
     [last_q("tx_rate_hz", extra=' and r.shuttle_id == "1"')],
     "hertz",
     [{"value": 0, "color": "dark-blue"}, {"value": 0.05, "color": "green"}, {"value": 9, "color": "dark-green"}],
     4, 8),
    ("Shuttle 2 — TX Rate (Hz)",
     [last_q("tx_rate_hz", extra=' and r.shuttle_id == "2"')],
     "hertz",
     [{"value": 0, "color": "dark-blue"}, {"value": 0.05, "color": "green"}, {"value": 9, "color": "dark-green"}],
     4, 12),
    ("Missions — Last 24 h",
     [raw_q(f"""from(bucket:"{BUCKET}") |> range(start: -24h) |> filter(fn: (r) => r._measurement == "stm_mission" and r._field == "packets") |> count() |> yield(name: "A")""")],
     "short", "purple", 4, 16),
    ("Jetson Board Power (~W)",
     [raw_q(f"""from(bucket:"{BUCKET}") |> range(start: -2m) |> filter(fn: (r) => r._measurement == "input_current" and r._field == "value") |> last() |> map(fn: (r) => ({{r with _value: float(v: r._value) * 5.0 / 1000.0}})) |> yield(name: "A")""")],
     "watt",
     [{"value": 0, "color": "green"}, {"value": 15, "color": "yellow"}, {"value": 22, "color": "red"}],
     4, 20),
]

for title, targets, unit, color_or_thresh, w, x in stat_specs:
    if isinstance(color_or_thresh, list):
        fc = {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                           "thresholds": {"mode": "absolute", "steps": color_or_thresh}}}
    else:
        fc = {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color_or_thresh}}}

    opts_map = None
    if "State" in title:
        opts_map = [{"type": "value", "options": {
            "0": {"text": "IDLE",   "color": "blue",  "index": 0},
            "1": {"text": "MOVING", "color": "green", "index": 1},
        }}]

    opts = {"reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "background", "graphMode": "none", "orientation": "horizontal"}
    if opts_map:
        opts["mappings"] = opts_map
        fc = {"defaults": {"color": {"mode": "thresholds"},
                           "thresholds": {"mode": "absolute",
                                          "steps": [{"value": 0, "color": "blue"},
                                                    {"value": 1, "color": "green"}]}}}

    panels.append(new_panel("stat", title, targets,
                            {"h": 4, "w": w, "x": x, "y": y},
                            options=opts, fieldConfig=fc))

y += 4

# ═══════════════════════════════════════════════════════════════
# ROW 1 — Motion: raw accel XYZ + state timeline
# (raw-only collection: magnitude / tilt / horizontal are no longer stored —
#  derive them at analysis time from the raw axes if needed)
# ═══════════════════════════════════════════════════════════════
panels.append(row("📡 Shuttle Motion", y)); y += 1

panels.append(new_panel("timeseries", "Acceleration X / Y / Z (g) — both shuttles",
    [ts_q("accel_x", ref="A"), ts_q("accel_y", ref="B"), ts_q("accel_z", ref="C")],
    {"h": 8, "w": 16, "x": 0, "y": y},
    fieldConfig=ts_defaults("short", lw=1, fill=0)))

panels.append(new_panel("timeseries", "State (0 = IDLE · 1 = MOVING)",
    [ts_q("state", agg="last")],
    {"h": 8, "w": 8, "x": 16, "y": y},
    fieldConfig={
        "defaults": {
            "unit": "short", "min": -0.1, "max": 1.1,
            "custom": {"lineWidth": 0, "fillOpacity": 60,
                       "drawStyle": "bars", "lineInterpolation": "stepAfter",
                       "showPoints": "never"},
            "thresholds": {"mode": "absolute",
                           "steps": [{"value": 0, "color": "blue"},
                                     {"value": 0.5, "color": "green"}]},
            "color": {"mode": "thresholds"},
        }
    }))
y += 8

# ═══════════════════════════════════════════════════════════════
# ROW 3 — Gyro XYZ (raw)
# ═══════════════════════════════════════════════════════════════
panels.append(row("🔄 Gyroscope Components", y)); y += 1

panels.append(new_panel("timeseries", "Gyroscope X / Y / Z (dps)",
    [ts_q("gyro_x", ref="A"), ts_q("gyro_y", ref="B"), ts_q("gyro_z", ref="C")],
    {"h": 7, "w": 24, "x": 0, "y": y},
    fieldConfig=ts_defaults("angv", lw=1, fill=0)))

y += 7

# ═══════════════════════════════════════════════════════════════
# ROW 4 — Environment (HTS221)
# ═══════════════════════════════════════════════════════════════
panels.append(row("🌡 Environment (STM32 on-board HTS221)", y)); y += 1

panels.append(new_panel("timeseries", "Temperature (°C)",
    [ts_q("temp_c")],
    {"h": 7, "w": 12, "x": 0, "y": y},
    fieldConfig=ts_defaults("celsius", lw=2, fill=10, min_=15, max_=50)))

panels.append(new_panel("timeseries", "Relative Humidity (%RH)",
    [ts_q("humidity_pct")],
    {"h": 7, "w": 12, "x": 12, "y": y},
    fieldConfig=ts_defaults("humidity", lw=2, fill=10, min_=0, max_=100)))

y += 7

# ═══════════════════════════════════════════════════════════════
# ROW 6 — Mission duration / packets
# (shuttle-side energy + distance removed — raw-only collection)
# ═══════════════════════════════════════════════════════════════
panels.append(row("📦 Mission Summaries", y)); y += 1

panels.append(new_panel("timeseries", "Per-Mission Duration (s)",
    [raw_q(f"""from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "stm_mission" and r._field == "duration_ms")
  |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
  |> yield(name: "A")""")],
    {"h": 7, "w": 12, "x": 0, "y": y},
    fieldConfig={
        "defaults": {
            "unit": "s",
            "custom": {"lineWidth": 0, "fillOpacity": 70,
                       "drawStyle": "bars", "showPoints": "never"},
        }
    }))

panels.append(new_panel("timeseries", "Packets per Mission",
    [raw_q(f"""from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "stm_mission" and r._field == "packets")
  |> yield(name: "A")""")],
    {"h": 7, "w": 12, "x": 12, "y": y},
    fieldConfig={
        "defaults": {
            "unit": "short",
            "custom": {"lineWidth": 0, "fillOpacity": 70,
                       "drawStyle": "bars", "showPoints": "never"},
        }
    }))

y += 7

# ═══════════════════════════════════════════════════════════════
# ROW 7 — Jetson INA3221 power (alumet)
# ═══════════════════════════════════════════════════════════════
panels.append(row("🖥 Jetson Power — INA3221 via Alumet", y)); y += 1

# Alumet INA3221 schema (current alumet version): ina_channel_label is a field,
# not a tag. Only one channel is reported (total board input). Filter by _field only.
def ina_curr_q(ref="A"):
    return raw_q(f"""from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "input_current" and r._field == "value")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "{ref}")""", ref=ref)

def ina_volt_q(ref="A"):
    return raw_q(f"""from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "input_voltage" and r._field == "value")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "{ref}")""", ref=ref)

def ina_pwr_q(ref="A"):
    # Power = current_mA × voltage_mV / 1e6 = W; joined on _time.
    return raw_q(f"""c = from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "input_current" and r._field == "value")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> rename(columns: {{_value: "current_ma"}})

v2 = from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "input_voltage" and r._field == "value")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> rename(columns: {{_value: "voltage_mv"}})

join(tables: {{c: c, v: v2}}, on: ["_time", "resource_consumer_kind", "resource_kind"])
  |> map(fn: (r) => ({{r with _value: r.current_ma * r.voltage_mv / 1.0e6}}))
  |> yield(name: "{ref}")""", ref=ref)

# Board power (W — current × voltage)
panels.append(new_panel("timeseries", "Board Input Power (W)",
    [ina_pwr_q()],
    {"h": 8, "w": 8, "x": 0, "y": y},
    fieldConfig=ts_defaults("watt", lw=2, fill=20, min_=0, max_=25)))

# Board current
panels.append(new_panel("timeseries", "Board Input Current (mA)",
    [ina_curr_q()],
    {"h": 8, "w": 8, "x": 8, "y": y},
    fieldConfig=ts_defaults("mamp", lw=2, fill=0, min_=0)))

# Board voltage
panels.append(new_panel("timeseries", "Board Input Voltage (mV)",
    [ina_volt_q()],
    {"h": 8, "w": 8, "x": 16, "y": y},
    fieldConfig=ts_defaults("mvolt", lw=2, fill=0, min_=4500, max_=5500)))

y += 8

# ═══════════════════════════════════════════════════════════════
# ROW 8 — FL Energy (per-round and per-phase)
# Queries fl_phases measurement written by AlumetProfiler in client.py.
# fl_phases has fields: energy_j, duration_ms; tags: phase, fl_round, device.
# ═══════════════════════════════════════════════════════════════
panels.append(row("⚡ FL Energy — per Round & Phase", y)); y += 1

panels.append(new_panel("barchart", "Energy per FL Round (J) — all devices",
    [raw_q(f"""from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "fl_phases" and r._field == "energy_j" and r.phase == "round_total")
  |> group(columns: ["fl_round", "device"])
  |> sum()
  |> yield(name: "A")""")],
    {"h": 8, "w": 12, "x": 0, "y": y},
    options={"xField": "fl_round", "groupWidth": 0.7, "fillOpacity": 80,
             "legend": {"displayMode": "list", "placement": "bottom"}},
    fieldConfig={"defaults": {"unit": "joule"}}))

panels.append(new_panel("timeseries", "Phase Duration (ms) — load / train / round_total",
    [raw_q(f"""from(bucket:"{BUCKET}")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "fl_phases" and r._field == "duration_ms")
  |> group(columns: ["phase", "device"])
  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)
  |> yield(name: "A")""")],
    {"h": 8, "w": 12, "x": 12, "y": y},
    options={"tooltip": {"mode": "multi"}, "legend": {"displayMode": "list", "placement": "bottom"}},
    fieldConfig={"defaults": {"unit": "ms", "custom": {"lineWidth": 2, "fillOpacity": 10,
                              "showPoints": "always", "pointSize": 6},
                              "min": 0}}))

y += 8

# ═══════════════════════════════════════════════════════════════
# ROW 9 — Mission history table
# ═══════════════════════════════════════════════════════════════
panels.append(row("📋 Mission History", y)); y += 1

table_flux = f"""from(bucket:"{BUCKET}")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "stm_mission")
  |> pivot(rowKey: ["_time","shuttle_id","gateway"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time","shuttle_id","gateway","packets","duration_ms"])
  |> map(fn: (r) => ({{r with duration_ms: r.duration_ms / 1000.0}}))
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: 100)"""

panels.append(new_panel("table", "All Missions — last 24 h",
    [raw_q(table_flux)],
    {"h": 10, "w": 24, "x": 0, "y": y},
    options={"footer": {"show": False}, "sortBy": []},
    fieldConfig={
        "defaults": {},
        "overrides": [
            {"matcher": {"id": "byName", "options": "_time"},
             "properties": [{"id": "displayName", "value": "Time"},
                             {"id": "custom.width", "value": 200}]},
            {"matcher": {"id": "byName", "options": "shuttle_id"},
             "properties": [{"id": "displayName", "value": "Shuttle"},
                             {"id": "custom.width", "value": 100}]},
            {"matcher": {"id": "byName", "options": "gateway"},
             "properties": [{"id": "displayName", "value": "Gateway"},
                             {"id": "custom.width", "value": 100}]},
            {"matcher": {"id": "byName", "options": "duration_ms"},
             "properties": [{"id": "displayName", "value": "Duration (s)"},
                             {"id": "unit", "value": "s"},
                             {"id": "decimals", "value": 1},
                             {"id": "custom.width", "value": 120}]},
            {"matcher": {"id": "byName", "options": "packets"},
             "properties": [{"id": "displayName", "value": "Packets"},
                             {"id": "decimals", "value": 0},
                             {"id": "custom.width", "value": 80}]},
        ],
    }))

y += 10

# ═══════════════════════════════════════════════════════════════
# Assemble dashboard
# ═══════════════════════════════════════════════════════════════
dashboard_payload = {
    "dashboard": {
        "id": None,
        "uid": "pludos-main",
        "title": "PLUDOS System Monitor",
        "description": "Shuttle telemetry · Jetson INA3221 power · Mission history",
        "tags": ["pludos"],
        "timezone": "browser",
        "refresh": "5s",
        "schemaVersion": 39,
        "graphTooltip": 1,
        "panels": panels,
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {},
    },
    "folderId": 0,
    "overwrite": True,
    "message": "full build from build_pludos_dashboard.py",
}

print(f"Building dashboard with {len(panels)} panels across {y} grid rows...")
resp, code = gf_post("/api/dashboards/db", dashboard_payload)
if code == 200:
    url = resp.get("url", "")
    print(f"✓ Dashboard created: http://localhost:3000{url}")
    print(f"  UID: {resp.get('uid')}")
    print(f"  Version: {resp.get('version')}")
    # Write dashboard JSON for Grafana provisioning (git-committed, survives volume wipe).
    _json_dir = pathlib.Path(__file__).parent / "server" / "grafana" / "dashboards"
    _json_dir.mkdir(parents=True, exist_ok=True)
    _json_path = _json_dir / "pludos_system_monitor.json"
    _json_path.write_text(json.dumps(dashboard_payload["dashboard"], indent=2))
    print(f"  JSON: {_json_path}")
else:
    print(f"✗ HTTP {code}")
    print(json.dumps(resp, indent=2))
