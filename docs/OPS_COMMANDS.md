# PLUDOS Operations Reference

Quick-access commands for field use, monitoring, and data retrieval.
Server Tailscale IP: **100.93.249.37** (laptop `lh25408`).
Jetson Tailscale IP: **100.119.83.35** (Jetson `warehouse1-desktop-1`).

---

## 1. Remote access

### Find the Jetson's Tailscale IP

```bash
# On the laptop — list all Tailscale peers:
tailscale status

# On the Jetson directly:
tailscale ip -4
```

The Jetson registers as `warehouse1-desktop-1` on the tailnet.
Its Tailscale IP is stable (100.x.x.x) as long as the device stays on the account.

### SSH to the Jetson

```bash
# Via local network (same WiFi):
ssh warehouse1@192.168.0.100

# Via Tailscale (remote, any network):
ssh warehouse1@100.119.83.35

# One-liner with password (dev/lab only — not for production):
sshpass -p 'Warehouse1savoye!' ssh warehouse1@100.119.83.35

# Check Tailscale is up on Jetson before trying:
tailscale status | grep warehouse1-desktop-1
```

> Note: you will see a `--restore-mark` connmark warning in `tailscale status`.
> This is a JetPack 5 / legacy iptables kernel limitation. It is non-fatal —
> routing and SSH work normally.

---

## 2. Container status

```bash
# On the Jetson:
ssh warehouse1@100.119.83.35 "podman ps -a"

# Live data-engine log (ctrl-C to exit):
ssh warehouse1@100.119.83.35 "podman logs -f pludos-data-engine"

# Last 100 lines of alumet-relay:
ssh warehouse1@100.119.83.35 "podman logs --tail 100 pludos-alumet-relay"

# Restart a container after .env change:
ssh warehouse1@100.119.83.35 "podman restart pludos-data-engine"

# Restart the full stack (no --profile = data-engine only):
ssh warehouse1@100.119.83.35 "cd ~/PLUDOS/client && podman-compose up -d data-engine"

# Full stack with FL + Tailscale sidecar:
ssh warehouse1@100.119.83.35 "cd ~/PLUDOS/client && podman-compose --profile vpn up -d"

# With INA3221 alumet relay (already started separately):
ssh warehouse1@100.119.83.35 "cd ~/PLUDOS/client && podman-compose --profile energy up -d alumet-relay"
```

---

## 3. Flux query reference

All queries use org `pludos`, bucket `alumet_energy`, token `pludos-dev-token`.

### Run a query from the laptop

```bash
curl -s -X POST "http://localhost:8086/api/v2/query?org=pludos" \
  -H "Authorization: Token pludos-dev-token" \
  -H "Content-Type: application/vnd.flux" \
  --data-raw '<paste Flux query here>'
```

### Current shuttle state (IDLE=0 / MOVING=1)

```flux
from(bucket: "alumet_energy")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "stm_telemetry" and r._field == "state")
  |> last()
```

### Live TX rate (Hz) per shuttle

```flux
from(bucket: "alumet_energy")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "stm_telemetry" and r._field == "tx_rate_hz")
  |> last()
```

### Acceleration magnitude — last hour

```flux
from(bucket: "alumet_energy")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "stm_telemetry" and r._field == "accel_mag")
  |> aggregateWindow(every: 5s, fn: mean, createEmpty: false)
```

### Temperature and humidity — last hour

```flux
from(bucket: "alumet_energy")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "stm_telemetry"
      and (r._field == "temp_c" or r._field == "humidity_pct"))
  |> aggregateWindow(every: 10s, fn: mean, createEmpty: false)
```

### All missions today (energy + duration)

```flux
from(bucket: "alumet_energy")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "stm_mission")
  |> pivot(rowKey: ["_time","shuttle_id","gateway"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time","shuttle_id","gateway","energy_j","packets","duration_ms"])
  |> sort(columns: ["_time"], desc: true)
```

### Total energy consumed today (per shuttle)

```flux
from(bucket: "alumet_energy")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "stm_mission" and r._field == "energy_j")
  |> group(columns: ["shuttle_id"])
  |> sum()
```

### Jetson INA3221 power — board input (VDD_IN, last hour)

```flux
from(bucket: "alumet_energy")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "input_current"
      and r._field == "value"
      and r.ina_channel_label == "VDD_IN")
  |> aggregateWindow(every: 10s, fn: mean, createEmpty: false)
  |> map(fn: (r) => ({r with _value: float(v: r._value) * 5.0 / 1000.0}))
```

### Jetson INA3221 — all channels, last value

```flux
from(bucket: "alumet_energy")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "input_current" and r._field == "value")
  |> last()
  |> keep(columns: ["_time","ina_channel_label","_value"])
```

---

## 4. Download Parquet files from the Jetson

Parquet files live in `~/PLUDOS/client/ram_buffer/` on the Jetson.

### List files

```bash
ssh warehouse1@100.119.83.35 "ls -lh ~/PLUDOS/client/ram_buffer/"

# Disk usage total:
ssh warehouse1@100.119.83.35 "du -sh ~/PLUDOS/client/ram_buffer/"
```

### Download a single mission file

```bash
scp warehouse1@100.119.83.35:~/PLUDOS/client/ram_buffer/mission_s1_*.parquet ./data/
```

### Download the daily consolidated file (all missions for a day)

```bash
# Replace YYYY-MM-DD with the target date:
scp warehouse1@100.119.83.35:~/PLUDOS/client/ram_buffer/2026-05-20.parquet ./data/

# Download all daily files:
scp "warehouse1@100.119.83.35:~/PLUDOS/client/ram_buffer/*.parquet" ./data/
```

### Download everything (rsync — skips files already present)

```bash
rsync -avz --progress \
  warehouse1@100.119.83.35:~/PLUDOS/client/ram_buffer/ \
  ./data/jetson-1/
```

### Inspect a Parquet file locally (Python)

```python
import pyarrow.parquet as pq, pandas as pd

df = pq.read_table("data/mission_s1_TIMESTAMP.parquet").to_pandas()
print(df.info())
print(df.describe())
print(df.head())
```

### Inspect inside the Jetson container (no local Python needed)

```bash
ssh warehouse1@100.119.83.35 \
  "podman exec -i pludos-data-engine python3" << 'EOF'
import pyarrow.parquet as pq, pandas as pd, glob, os
files = sorted(glob.glob("/app/ram_buffer/mission_s1_*.parquet"))
df = pq.read_table(files[-1]).to_pandas()
print(df.describe().round(3))
EOF
```

---

## 5. Alumet / INA3221 power data

Alumet (`pludos-alumet-relay`) writes directly to InfluxDB.
Measurements: `input_current` (mA) and `input_voltage` (mV), tagged by `ina_channel_label`.

### Watch live in terminal

```bash
# Last value for all INA3221 channels:
watch -n 5 "curl -s -X POST 'http://localhost:8086/api/v2/query?org=pludos' \
  -H 'Authorization: Token pludos-dev-token' \
  -H 'Content-Type: application/vnd.flux' \
  --data-raw 'from(bucket:\"alumet_energy\") |> range(start: -30s) |> filter(fn: (r) => r._measurement == \"input_current\" and r._field == \"value\") |> last() |> keep(columns: [\"ina_channel_label\",\"_value\"])' | grep _result | awk -F, '{print \$NF, \$(NF-1)}'"
```

Or via the Grafana dashboard (Jetson Power row — updates every 5s).

### Export alumet data to CSV (from InfluxDB)

```bash
# Download input_current for all channels, last 24h:
curl -s -X POST "http://localhost:8086/api/v2/query?org=pludos" \
  -H "Authorization: Token pludos-dev-token" \
  -H "Content-Type: application/vnd.flux" \
  --data-raw '
from(bucket: "alumet_energy")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "input_current" and r._field == "value")
  |> keep(columns: ["_time","ina_channel_label","_value"])
  |> pivot(rowKey: ["_time"], columnKey: ["ina_channel_label"], valueColumn: "_value")
' > alumet_current_24h.csv

# Same for voltage:
curl -s -X POST "http://localhost:8086/api/v2/query?org=pludos" \
  -H "Authorization: Token pludos-dev-token" \
  -H "Content-Type: application/vnd.flux" \
  --data-raw '
from(bucket: "alumet_energy")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "input_voltage" and r._field == "value")
  |> keep(columns: ["_time","ina_channel_label","_value"])
  |> pivot(rowKey: ["_time"], columnKey: ["ina_channel_label"], valueColumn: "_value")
' > alumet_voltage_24h.csv
```

### Compute power and export (VDD_IN only, mA × mV → µW → W)

```bash
# This exports current only; multiply offline by voltage (≈5V) if needed.
curl -s -X POST "http://localhost:8086/api/v2/query?org=pludos" \
  -H "Authorization: Token pludos-dev-token" \
  -H "Content-Type: application/vnd.flux" \
  --data-raw '
from(bucket: "alumet_energy")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "input_current"
      and r._field == "value"
      and r.ina_channel_label == "VDD_IN")
  |> map(fn: (r) => ({r with _value: float(v: r._value) * 5.0 / 1000.0}))
' > vdd_in_power_watts.csv
```

---

## 6. InfluxDB health and token check

```bash
# Health:
curl http://localhost:8086/health
# Expected: {"status":"pass",...}

# Token check from Jetson to server:
ssh warehouse1@100.119.83.35 \
  "curl -s -o /dev/null -w '%{http_code}' \
   -H 'Authorization: Token pludos-dev-token' \
   http://100.93.249.37:8086/health"
# Expected: 200
```

---

## 7. Grafana

Dashboard URL: **http://localhost:3000/d/pludos-main**
Default credentials: `admin` / `admin` (change on first login — §9 in DEPLOYMENT_CHECKLIST.md).

```bash
# Open from terminal (macOS):
open http://localhost:3000/d/pludos-main

# Check Grafana is running:
curl -s http://localhost:3000/api/health | python3 -m json.tool
```

---

## 8. Update the Jetson code

```bash
# Pull latest code on Jetson and rebuild + restart data-engine:
ssh warehouse1@100.119.83.35 \
  "cd ~/PLUDOS && git pull && cd client && podman-compose up --build -d data-engine"

# Pull only (no rebuild — for config/script changes):
ssh warehouse1@100.119.83.35 "cd ~/PLUDOS && git pull"
```

---

## 9. FL Round — real deployment (Jetson SuperNode)

```bash
# ONE-TIME host setup on the server laptop (survives until reboot):
sudo chmod a+r -R /sys/devices/virtual/powercap/intel-rapl
# Make permanent across reboots:
echo 'z /sys/devices/virtual/powercap/intel-rapl - a+r - -' | sudo tee /etc/tmpfiles.d/rapl.conf

# 1. Start SuperLink (from PLUDOS root, in pludos_venv):
cd ~/PLUDOS && source pludos_venv/bin/activate
flower-superlink --insecure &>/tmp/superlink.log &

# 2. Start SuperNode on Jetson (inside the data-engine container):
sshpass -p 'Warehouse1savoye!' ssh warehouse1@192.168.0.100 \
  "podman exec pludos-data-engine sh -c \
   'nohup flower-supernode --insecure --superlink 192.168.0.101:9092 >/tmp/sn.log 2>&1 &'"

# 3. Submit the FL run:
INFLUXDB_URL=http://localhost:8086 INFLUXDB_TOKEN=pludos-dev-token \
INFLUXDB_ORG=pludos INFLUXDB_BUCKET=alumet_energy \
flwr run . pludos-network

# 4. Stream training logs (copy the run-id from the submit output):
flwr log <run-id> pludos-network

# 5. Check saved global model on server:
ls -lh server/models/

# 6. Check saved model on Jetson (saved by client.py evaluate):
sshpass -p 'Warehouse1savoye!' ssh warehouse1@192.168.0.100 \
  "ls -lh ~/PLUDOS/client/ram_buffer/model/"

# Stop SuperLink when done:
pkill -f flower-superlink
```

---

## 10. Standalone inference on Jetson (no server)

```python
# Load the persisted global model and run a prediction:
import xgboost as xgb, numpy as np
booster = xgb.Booster()
booster.load_model("/app/ram_buffer/model/latest.ubj")   # inside container
# or: ~/PLUDOS/client/ram_buffer/model/latest.ubj        # on host
preds = (booster.predict(xgb.DMatrix(X)) > 0.5).astype(int)
```
