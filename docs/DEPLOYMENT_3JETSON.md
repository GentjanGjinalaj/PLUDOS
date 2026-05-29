# 3-Jetson × 6-STM Dev Rig — Deployment Recipe

End-to-end checklist for the test deployment. Pairs 2 STMs per Jetson, 3 Jetsons
plus 1 server (the laptop). All seven devices share one WiFi network.

```
              ┌──────────┐         ┌──────────┐         ┌──────────┐
   STM-1 ────►│          │  STM-3 ►│          │  STM-5 ►│          │
              │ jetson-1 │         │ jetson-2 │         │ jetson-3 │
   STM-2 ────►│  group   │  STM-4 ►│  group   │  STM-6 ►│  group   │
              │   1,2    │         │   3,4    │         │   5,6    │
              └─────┬────┘         └─────┬────┘         └─────┬────┘
                    │ Tailscale          │ Tailscale          │ Tailscale
                    └────────────────────┼────────────────────┘
                                         │
                                  ┌──────▼──────┐
                                  │   server    │
                                  │ InfluxDB +  │
                                  │ Grafana +   │
                                  │  Flower     │
                                  └─────────────┘
```

---

## 0. Repo state (already done)

- `tools/mock_stm32.py` rewritten for the unified UDP stream and supports
  `MOCK_SHUTTLES=N` for laptop-side stress testing.
- `client/data-engine.py` accepts `SHUTTLE_GROUP=1,2`; broadcasts beacon as
  `PLUDOS-GW:<ip>:1,2` and drops out-of-group ingress.
- STM32 firmware (`Core/Src/main.c`) parses the beacon `:<csv-ids>` suffix and
  bonds only to a Jetson whose group includes its own `SHUTTLE_ID`.
- `SHUTTLE_NAMES` default in the gateway now covers IDs 1-6.

---

## 1. STM32 side (per device, 6 total)

Each STM32 needs a unique `wifi_credentials.h`. The template lives at
`STM_Shuttles/PLUDOS_Edge_Node/Core/Inc/wifi_credentials.h.example`. Copy it
to `wifi_credentials.h` (gitignored) and set:

| STM serial number | SHUTTLE_ID | Bonded Jetson |
|---|---|---|
| STM-1 | `1U` | jetson-1 |
| STM-2 | `2U` | jetson-1 |
| STM-3 | `3U` | jetson-2 |
| STM-4 | `4U` | jetson-2 |
| STM-5 | `5U` | jetson-3 |
| STM-6 | `6U` | jetson-3 |

Build in STM32CubeIDE for each device; flash via ST-Link. `WIFI_SSID`,
`WIFI_PASSWORD`, and `JETSON_IP` (compile-time fallback) are the same on all
six. The beacon will override `JETSON_IP` at runtime.

---

## 2. Each Jetson (3 total)

### 2a. SSH in and pull the repo
```bash
ssh jetson-1
cd ~/PLUDOS && git pull
```

### 2b. Per-Jetson `.env`
Copy `client/.env.example` to `client/.env` and set ONLY these two values
per Jetson (the rest of the file is identical across all three):

| Jetson | `JETSON_HOSTNAME` | `SHUTTLE_GROUP` |
|---|---|---|
| jetson-1 | `jetson-1` | `1,2` |
| jetson-2 | `jetson-2` | `3,4` |
| jetson-3 | `jetson-3` | `5,6` |

Also fill in:
- `TS_AUTHKEY=<one-shot key from the Tailscale admin console>`
- `INFLUXDB_URL=http://<server-tailscale-ip>:8086`
- `INFLUXDB_TOKEN=<same admin token as server/.env>`

### 2c. Bring up the data-engine
```bash
cd ~/PLUDOS/client
podman-compose up -d data-engine
podman logs -f pludos-data-engine | head -20
```

Expect the startup banner to read `… | group=1,2 | …` and the beacon line to
read `[BEACON] announcing <ip> on UDP port 5000 every 10 s (group=1,2)`.

### 2d. Power on the paired STMs
Within 30 s each STM should log on UART:
```
[BEACON] Listening on UDP 5000 (10 x 3000 ms)...
[BEACON] Gateway found: <jetson-ip>
[NETWORK] PludosTelemetry stream armed → udp://<jetson-ip>:5683
```
Beacons from the other two Jetsons appear in the STM UART as:
```
[BEACON] Ignored beacon (different group): <other-jetson-ip>:3,4
```
That confirms the pairing filter is working.

### 2e. (Optional now, required for FL) bring up Tailscale + ai-worker
```bash
podman-compose --profile vpn up -d
podman exec pludos-tailscale tailscale ip -4   # confirm the Jetson got a tailnet IP
```

---

## 3. Server (laptop for now)

### 3a. `server/.env`
Copy `server/.env.example` → `server/.env` and rotate at least
`INFLUXDB_ADMIN_TOKEN` and the Grafana password before any non-local
deployment.

### 3b. Start the monitoring stack
```bash
cd server
podman-compose up -d
# InfluxDB on :8086, Grafana on :3000, Alumet relay on :50051 / :9091
```

### 3c. Verify ingress
Once any STM has finished one mission (≥ 30 s of IDLE after MOVING), check
that `stm_mission` has at least one point per shuttle:
```bash
podman exec -it pludos-influxdb influx query '
from(bucket:"alumet_energy")
  |> range(start:-1h)
  |> filter(fn:(r) => r._measurement == "stm_mission")
  |> group(columns:["shuttle_id","gateway"])
  |> count()
'
```
You should see one row per `(shuttle_id, gateway)` combination — six rows
once all shuttles have run a mission.

### 3d. Flower server with 3 clients
```bash
export FL_MIN_FIT_CLIENTS=3
export FL_NUM_ROUNDS=3
flwr run .
```
The server waits for all 3 Jetsons (`ai-worker`) to connect before starting
round 1. Watch for `--- ROUND 1: aggregating from 3 gateway(s) ---` in the
server log — that confirms the horizontal tree-set union (ADR-010 Option A)
is firing on a real multi-gateway round.

---

## 4. Laptop-only dry run (before touching hardware)

Useful when you want to validate the pipeline end-to-end without any STMs
or Jetsons powered on. From the repo root:

```bash
# Terminal 1: the gateway (no InfluxDB needed; writes Parquet to ./ram_buffer)
cd client && TEST_MODE=1 SHUTTLE_GROUP=1,2 python3 data-engine.py

# Terminal 2: six mock shuttles
MOCK_SHUTTLES=6 python3 tools/mock_stm32.py
```

Expected: STM32-Alpha and STM32-Beta logs appear; STM32-Charlie..Foxtrot
packets are silently dropped (visible at `--log-level debug`). After ~70 s
the first mission flush writes a Parquet file under `client/ram_buffer/`.

To exercise the InfluxDB + Grafana side as well, bring up `server/`
compose first, then point `INFLUXDB_URL` in `client/.env` at
`http://127.0.0.1:8086` instead of the Tailscale address.

---

## 5. Smoke-test matrix before declaring "ready"

| Check | How | Pass criterion |
|---|---|---|
| STM bonds to correct Jetson | UART log on STM-3 | `[BEACON] Gateway found: <jetson-2 ip>` |
| Cross-Jetson beacons ignored | UART log on STM-3 | `[BEACON] Ignored beacon (different group): <jetson-1 ip>:1,2` |
| Out-of-group packets dropped | jetson-1 logs at DEBUG | `shuttle_id=3 not in SHUTTLE_GROUP={1,2}` |
| Mission flush works | Per-shuttle Parquet file appears in `ram_buffer/` after ~30 s IDLE | one file per shuttle per mission |
| `stm_mission` InfluxDB points | Flux count query in §3c | 6 rows after one full cycle |
| `fl_phases` per gateway | Same bucket, `phase=round_total` | 3 rows per FL round |
| Multi-gateway FL round | Server log | `aggregating from 3 gateway(s)` |
| Grafana sees device split | `device` tag in `fl_energy` dashboard | three distinct series |

---

## 6. Known caveats

- WiFi capacity. 3 Jetsons × 2 STMs × 50 Hz MOVING = 300 packets/s on one
  2.4 GHz channel. Not stressed yet — first stress observation lives at
  P2-14 in `current_problems.md`.
- `client/Containerfile` still uses CPU-only `python:3.10-slim`. The
  ai-worker XGBoost fit runs on CPU until a JetPack 6 CUDA image is wired.
- No shuttle-side energy figure. The `POWER_*_MW` gateway estimate was
  removed in the schema-v4 raw-only cull; only Jetson/server energy (Alumet)
  is instrument-grade. Add an STM32 INA219 before claiming shuttle energy.
- `client/CLAUDE.md` and several other docs still reference the pre-ADR-015
  CoAP architecture. They are local-only (gitignored) and need a separate
  pass.
