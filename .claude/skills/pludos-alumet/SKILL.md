---
name: pludos-alumet
description: Guides integration of the Alumet energy measurement framework (developed by UGA/LIG) into the PLUDOS system. Use whenever the user asks about real energy measurement on the Jetson, replacing the AlumetProfiler placeholder, reading INA3221 or tegrastats power data, implementing the Alumet relay concept, or any work related to ADR-011 in docs/decisions.md. Also use when the user mentions Alumet, energy sensing, power monitoring, or InfluxDB energy writes from the Jetson side.
---

# PLUDOS Alumet Integration Skill

Alumet is an open-source energy measurement framework developed by the
LIG laboratory at Université Grenoble Alpes (UGA) — the academic partner
in the PLUDOS CIFRE PhD project. It is architecture-agnostic, plugin-based,
and has specific support for NVIDIA Jetson hardware.

**Current state in PLUDOS:** the `AlumetProfiler` class in `client/client.py`
is a placeholder. It writes `random.uniform(25, 45)` W in TEST_MODE and a
hardcoded `12.0` W in production. Nothing reaches InfluxDB from real sensors.
This is tracked as ADR-011 in `docs/decisions.md` and P2-3 in
`docs/current_problems.md`.

---

## What Alumet Does

Alumet is a measurement pipeline: plugins produce power/energy observations,
pipelines transform and aggregate them, and output plugins push to sinks
(InfluxDB, CSV, stdout). Key concepts:

- **Source plugin**: reads a hardware sensor (INA3221, RAPL, NVIDIA NVML)
- **Transform plugin**: computes energy (power × time), rate-limits, aggregates
- **Output plugin**: writes to InfluxDB, CSV, stdout

For PLUDOS, the relevant source is the **Jetson INA3221** (multi-channel
power monitor on the Orin Nano module).

---

## PLUDOS Alumet Architecture

```
Jetson Orin Nano                      Laptop (Central Server)
┌─────────────────────────┐           ┌──────────────────────────┐
│ client.py               │           │ server.py                │
│  └─ AlumetProfiler      │           │  └─ (future: Alumet      │
│      (placeholder)      │           │       central instance)  │
│                         │           │                          │
│ Alumet relay instance   │──────────►│ Alumet server instance   │
│  └─ INA3221 plugin      │ Tailscale │  └─ InfluxDB output      │
│  └─ tegrastats plugin   │  VPN      │  └─ Grafana dashboards   │
└─────────────────────────┘           └──────────────────────────┘
```

The Jetson runs an **Alumet relay** — a lightweight Alumet instance that
reads local sensors and forwards metric streams to the central Alumet
instance (or directly to InfluxDB). The Flower `ClientApp` triggers
measurement start/stop around `model.fit()`.

---

## Jetson Power Sensor Access

### Via tegrastats (simplest)

```bash
# On Jetson — outputs power for VDD_CPU, VDD_GPU, VDD_SOC, etc.
tegrastats --interval 100   # 100 ms = 10 Hz

# Example line:
# RAM 2135/7615MB ... VDD_CPU 1234mW VDD_GPU 3456mW VDD_SOC 789mW
```

Parse `VDD_GPU` for training energy; `VDD_SOC` for baseline.

### Via sysfs (INA3221 direct)

```bash
# List available channels
ls /sys/bus/i2c/drivers/ina3221/*/iio:device*/

# Read power channel (in milliwatts)
cat /sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power0_input
cat /sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power1_input
cat /sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power2_input
```

Channel numbers map to different power rails — check `in_label*` to identify
which channel is GPU vs. CPU vs. system.

### Via Alumet Jetson plugin

Alumet provides a Jetson plugin that wraps tegrastats internally:
```bash
# Install Alumet (Rust-based)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
cargo install alumet-cli

# Run with Jetson plugin
alumet-cli --plugin jetson --output influxdb \
  --influxdb-url http://localhost:8086 \
  --influxdb-token <token> \
  --influxdb-org pludos \
  --influxdb-bucket alumet_energy
```

---

## Replacing AlumetProfiler in client.py

The current placeholder:
```python
class AlumetProfiler:
    def _measure(self):
        while self.running:
            power_w = random.uniform(25.0, 45.0)  # PLACEHOLDER
            ...
```

### Short-term fix (tegrastats, no Alumet binary)

Replace `_measure` with a tegrastats reader:

```python
import subprocess, re

def _read_jetson_power_w() -> float:
    """Read current GPU power from tegrastats. Returns watts."""
    result = subprocess.run(
        ["tegrastats", "--interval", "100", "--count", "1"],
        capture_output=True, text=True, timeout=2
    )
    match = re.search(r'VDD_GPU (\d+)mW', result.stdout)
    return int(match.group(1)) / 1000.0 if match else 0.0
```

### Long-term fix (Alumet relay)

1. Start Alumet relay as a sidecar process at container startup.
2. Expose a local HTTP/gRPC endpoint that `client.py` can query for
   power readings.
3. Replace `AlumetProfiler._measure()` with a call to the relay endpoint.
4. Route relay output to the central Alumet instance (over Tailscale).

---

## InfluxDB Schema for Energy Data

The current placeholder writes to measurement `fl_energy`. Preserve this
measurement name when replacing with real Alumet so Grafana queries don't break.

```
Measurement: fl_energy
Tags:
  fl_round   = <round number, e.g. "1", "2", "3">
  device     = <hostname of Jetson, e.g. "jetson-warehouse1">
  nvpmodel   = <power mode, e.g. "MAXN_SUPER", "15W", "7W">
Fields:
  power_gpu_w   = <float, GPU rail power in watts>
  power_cpu_w   = <float, CPU rail power in watts>
  power_total_w = <float, total system power in watts>
  energy_j      = <float, cumulative joules since round start>
Timestamp: nanosecond precision
```

Tag `nvpmodel` is important — any benchmark that doesn't note the power mode
is not reproducible. Always set it at profiler init:
```python
import subprocess
nvpmodel = subprocess.check_output(["nvpmodel", "-q"]).decode().strip()
```

---

## Measurement Events to Capture

For the thesis, you want energy measurements at these granularities:

| Event | What to measure | Tag |
|---|---|---|
| Data ingestion (CoAP server) | Total power during packet receive burst | `phase=ingest` |
| Parquet write (flush) | Power spike during PyArrow write | `phase=flush` |
| Model training (`model.fit()`) | GPU + CPU power at 10 Hz | `phase=train` |
| Model send (gRPC to server) | Network + CPU power | `phase=transmit` |
| Idle (waiting for next round) | Baseline power | `phase=idle` |

This breakdown lets you compute the energy cost per operation and argue which
part of the FL pipeline is the dominant energy consumer.

---

## Before You Start ADR-011

Confirm these prerequisites on the Jetson:
```bash
# Check tegrastats is available
which tegrastats && tegrastats --interval 100 --count 1

# Check INA3221 sysfs path
ls /sys/bus/i2c/drivers/ina3221/ 2>/dev/null || echo "sysfs path not found"

# Check Rust is installed (for Alumet binary install)
rustc --version || echo "Rust not installed"

# Check current NVPModel
sudo nvpmodel -q
```

If `tegrastats` works, the short-term fix is the right first step. Get real
numbers flowing into InfluxDB first; refine the Alumet relay integration second.

---

## Anti-patterns

- **Don't claim energy numbers from the placeholder.** `random.uniform(25, 45)`
  is not a measurement. Any graph showing these values in the thesis is invalid.
- **Don't assume 12 W.** The hardcoded production value `12.0` is a rough
  midpoint of the 7–25 W power envelope. Actual power depends on NVPModel and
  workload. Measure it.
- **Don't forget NVPModel.** Energy benchmarks without the power mode are
  meaningless for reproducibility. Always tag it.
- **Don't conflate GPU and total system power.** InfluxDB should store both
  so the thesis can argue at the right granularity.
