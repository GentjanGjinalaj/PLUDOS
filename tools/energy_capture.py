#!/usr/bin/env python3
"""Pull REAL gateway energy numbers from the Jetson's InfluxDB (alumet INA3221
feed). No modelling: board power W = measured input_current(mA) * input_voltage(mV)
/ 1e6, and energy J = integral of W over a real time window (1 s rectangle grid).

Two reports:
  [1] baseline idle board power per rail (last N minutes, gateway idle).
  [2] drain-ingest cost: for each recorded drain (stm_mission, kind=mission), the
      mean board power over its real reception window [recv_start_ms, recv_end_ms]
      vs the idle baseline -> the extra energy of receiving that drain.

A drain only shows here if the alumet feed was live during its reception window
(bump the alumet poll rate via ALUMET_POLL_INTERVAL if windows come back empty).
To generate a drain on demand without hardware, see tools/mock_drain.py.

Usage:
  INFLUX_HOST=100.119.83.35 python tools/energy_capture.py
Env:
  INFLUX_HOST     gateway IP                 (default 127.0.0.1)
  INFLUXDB_TOKEN  api token                  (default pludos-dev-token, repo dev default)
  INFLUXDB_ORG    org                        (default pludos)
  INFLUXDB_BUCKET bucket                     (default alumet_energy)
  BASELINE_MIN    idle baseline window (min) (default 5)
"""
import csv
import io
import os
import urllib.request

HOST = os.getenv("INFLUX_HOST", "127.0.0.1")
TOKEN = os.getenv("INFLUXDB_TOKEN", "pludos-dev-token")
ORG = os.getenv("INFLUXDB_ORG", "pludos")
BUCKET = os.getenv("INFLUXDB_BUCKET", "alumet_energy")
BASELINE_MIN = int(os.getenv("BASELINE_MIN", "5"))
URL = f"http://{HOST}:8086/api/v2/query?org={ORG}"
RAILS = ["VDD_IN", "VDD_CPU_GPU_CV", "VDD_SOC"]  # VDD_IN = board total input


# POST a Flux query; return rows as list-of-dict parsed from annotated CSV.
def flux(q: str) -> list[dict]:
    req = urllib.request.Request(
        URL, data=q.encode(),
        headers={"Authorization": f"Token {TOKEN}",
                 "Accept": "application/csv",
                 "Content-type": "application/vnd.flux"})
    raw = urllib.request.urlopen(req, timeout=60).read().decode()
    rows = [ln for ln in raw.splitlines() if ln and not ln.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(rows)))) if rows else []


# Mean board power (W) over [start,stop] for one rail. Pairs measured current and
# voltage on a common 1 s grid, then averages i*u — honest rectangle integration.
_MEANW = '''
cur = from(bucket:"{b}") |> range(start:{s}, stop:{e})
  |> filter(fn:(r)=>r._measurement=="input_current" and r._field=="value" and r.ina_channel_label=="{ch}")
  |> aggregateWindow(every:1s, fn:mean, createEmpty:false)
  |> keep(columns:["_time","_value"]) |> rename(columns:{{_value:"i"}})
volt = from(bucket:"{b}") |> range(start:{s}, stop:{e})
  |> filter(fn:(r)=>r._measurement=="input_voltage" and r._field=="value" and r.ina_channel_label=="{ch}")
  |> aggregateWindow(every:1s, fn:mean, createEmpty:false)
  |> keep(columns:["_time","_value"]) |> rename(columns:{{_value:"u"}})
join(tables:{{c:cur, v:volt}}, on:["_time"])
  |> map(fn:(r)=>({{_time:r._time, w: r.i * r.u / 1000000.0}}))
  |> mean(column:"w") |> yield(name:"m")
'''


def mean_w(start: str, stop: str, ch: str):
    r = flux(_MEANW.format(b=BUCKET, s=start, e=stop, ch=ch))
    return float(r[0]["w"]) if r and r[0].get("w") not in (None, "") else None


# RFC3339 timestamp from unix ms — Flux range() needs a time, not a duration literal.
def iso(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def main() -> None:
    print("=" * 68)
    print("PLUDOS ENERGY CAPTURE — real INA3221 samples, no modelling")
    print(f"host={HOST} bucket={BUCKET}")
    print("=" * 68)

    # [1] Baseline idle: all containers up, no drain in progress.
    print(f"\n[1] BASELINE idle gateway (last {BASELINE_MIN} min)")
    base = {ch: mean_w(f"-{BASELINE_MIN}m", "now()", ch) for ch in RAILS}
    for ch in RAILS:
        v = base[ch]
        print(f"  {ch:16s} {v:.3f} W" if v is not None else f"  {ch:16s} n/a")
    vin = base["VDD_IN"]
    if vin is not None:
        print(f"  -> board idle = {vin:.3f} W = {vin * 60:.1f} J/min")

    # [2] Drain-ingest: integrate board power over each drain's real recv window.
    print("\n[2] DRAIN-INGEST energy (per mission drain, recv window)")
    drains = flux(f'''
from(bucket:"{BUCKET}") |> range(start:-12h)
  |> filter(fn:(r)=>r._measurement=="stm_mission" and r.kind=="mission")
  |> filter(fn:(r)=>r._field=="recv_start_ms" or r._field=="recv_end_ms" or r._field=="accel_samples" or r._field=="write_start_ms" or r._field=="write_end_ms")
  |> last()
  |> drop(columns:["_time","_start","_stop","_measurement"])
  |> pivot(rowKey:["gw_mission_id","shuttle_id"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["recv_start_ms"])
''')
    hdr = f"  {'shuttle':7s} {'rx_s':>5s} {'meanW':>7s} {'J_ingest':>8s} | {'wr_s':>5s} {'J_store':>7s}"
    print(hdr)
    tot, cnt = 0.0, 0
    stot, scnt = 0.0, 0
    for d in drains:
        try:
            rs, re = int(float(d["recv_start_ms"])), int(float(d["recv_end_ms"]))
        except (KeyError, ValueError):
            continue
        if re <= rs:
            continue
        dur = (re - rs) / 1000.0
        mw = mean_w(iso(rs), iso(re), "VDD_IN")
        if mw is None:
            print(f"  s{d.get('shuttle_id','?'):6s} {dur:5.2f}  (no power samples in window)")
            continue
        over = (mw - vin) if vin is not None else None
        jing = over * dur if over is not None else None
        sid = d.get("shuttle_id", "?")
        ji = f"{jing:.2f}" if jing is not None else "n/a"

        # Storage window: integrate board power over the Parquet write. Often sub-second
        # at 5 Hz poll -> may land 0 power samples (shows n/a); bump ALUMET_POLL_INTERVAL
        # for a tighter grid. J_store is the extra energy of persisting the drain to disk.
        sdur_s = swj = None
        try:
            ws, we = int(float(d["write_start_ms"])), int(float(d["write_end_ms"]))
            if we > ws:
                sdur_s = (we - ws) / 1000.0
                smw = mean_w(iso(ws), iso(we), "VDD_IN")
                if smw is not None and vin is not None:
                    swj = (smw - vin) * sdur_s
        except (KeyError, ValueError):
            pass
        sd = f"{sdur_s:5.3f}" if sdur_s is not None else "  n/a"
        sj = f"{swj:.3f}" if swj is not None else "n/a"
        print(f"  s{sid:6s} {dur:5.2f} {mw:7.3f} {ji:>8} | {sd:>5} {sj:>7}")
        if jing is not None:
            tot += jing
            cnt += 1
        if swj is not None:
            stot += swj
            scnt += 1
    if cnt:
        print(f"  -> mean ingest overhead  = {tot / cnt:.2f} J/drain (n={cnt})")
    if scnt:
        print(f"  -> mean storage overhead = {stot / scnt:.3f} J/drain (n={scnt})")
    print("  J_ingest = (meanW_drain - idleW) * rx_dur   -> energy to receive the drain")
    print("  J_store  = (meanW_write - idleW) * wr_dur   -> energy to persist it to Parquet")


if __name__ == "__main__":
    main()
