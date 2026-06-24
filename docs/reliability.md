# PLUDOS — End-to-End Packet-Loss Budget

Quantifies expected packet loss at each tier of the PLUDOS telemetry path and
defines the per-shuttle SLO. Numbers marked **estimated** lack hardware measurement;
those marked **design** follow from the protocol choice.

> **ADR-021 note.** Sections 1–3 below were written for the continuous live UDP
> stream, which is now **removed** — the radio is off except to drain finished
> captures. The per-hop *loss mechanisms* still apply, but the packet rates and the
> "× 50 Hz" wall-time figures describe the **superseded live-stream model**; keep
> them for history. The current path is the bursty drain on `:5684`, whose loss
> budget is now **measured** — see §0A.

---

## 0A — Drain-path loss budget (ADR-021, `:5684`) — measured

The live stream is gone; the real path is a per-mission burst of CRC32-framed UDP
chunks drained from PSRAM after a run. There is no application-layer ARQ — a lost
chunk is a permanent gap. The gateway reassembler stamps every drain with
`packets_total` / `packets_received` / `packets_lost` / `packet_loss_pct` /
`all_packets_received` (parquet columns, also fields on the `stm_mission(drain)`
InfluxDB point), so loss is observed, not estimated.

**[measured 2026-06-24, n = 878 distinct drain missions, gateway parquet corpus]**

| Regime | Missions | Complete (`all_packets_received`) | Aggregate chunk loss | Mean per-mission | Worst mission |
|--------|---------:|----------------------------------:|---------------------:|-----------------:|--------------:|
| Idle snapshots | 753 | **100.0 %** | **0.000 %** | 0.000 % | 0.00 % |
| Moving drains | 125 | **92.6 %** | 0.268 % | 0.311 % | 18.42 % |
| **All** | 878 | **99.0 %** | **0.263 %** | 0.043 % | 18.42 % |

Reading:

- **Idle snapshots never lose a packet.** They are small (10 s @ 12.5 Hz, a handful
  of chunks) and fit inside one MAC burst; 753/753 arrived whole.
- **Loss is confined to the multi-MB moving blasts.** ~9 of 125 moving drains lost
  at least one chunk; the aggregate is still 0.27 %, but the worst single mission
  lost 18.4 % — a fat-tail consistent with a transient WiFi event mid-blast, not
  steady-state loss.
- **No timestamp reconstruction events** (`t0_reconstructed = 0` across all 878):
  every `DrainBegin` landed, so no capture had its `t0` synthesised to arrival time
  (cf. council item 6 / `drain_receiver.py`).

### Per-drain SLO

> **Target:** ≥ 98 % of drains complete (`all_packets_received = true`); aggregate
> chunk loss < 1 % across a rolling window. **Met** at n = 878 (99.0 % complete,
> 0.26 % loss).

Observable live via the `packet_loss_pct` field and the per-drain comms-vs-storage
Grafana panel (added under council item N). A moving-drain completion rate dropping
below ~90 %, or a cluster of high-loss missions, indicates a WiFi/link regression and
warrants investigation — there is no ARQ to mask it.

---

## 1 — Loss points (by hop)

> §1.0 (PSRAM crash recovery) is **current**. §1.1 onward describes the removed
> continuous `:5683` live stream — **[estimated]** numbers kept for history; they do
> **not** describe the current drain path (§0A).

### 1.0 STM32 capture buffer (before drain) — current

| Scenario | Packets at risk | Mitigation |
|----------|-----------------|------------|
| MCU reset (IWDG watchdog / brownout) between *seal* and *drain* | All sealed-but-undrained captures staged in PSRAM (idle snapshots + the just-finished mission) | **Crash-recovery index** (ADR-021, 2026-06-16): the bookkeeping is mirrored into a CRC-validated 16 KB PSRAM region that survives a core reset; on warm boot the index is restored and the captures are re-drained. The PSRAM data itself is externally powered and persists across an MCU reset. |
| PSRAM power loss (battery removal) | Entire undrained ring | Not recoverable — PSRAM is volatile on power loss. Out of scope for the watchdog/brownout case above. |

### 1.1 STM32 → Jetson (UDP)

| Source | Loss mode | Expected rate |
|--------|-----------|---------------|
| WiFi frame error | Retransmitted at MAC layer (802.11 ARQ) | ≤ 0.1 % on 2.4 GHz short-range LOS (estimated) |
| WiFi disconnect | `STA_DOWN` → STM32 sends into void; reconnect < 500 ms | Up to ~50 consecutive packets lost during reconnect event, then resumes |
| Buffer overflow on Jetson (UDP socket) | asyncio loop blocked > 100 ms (e.g., Parquet flush) | ≤ 0.1 % (estimated; asyncio keeps socket buffer large by default) |
| **Total per-hop estimate** | | **≤ 1 % in steady state** |

Gaps are detectable via `seq_gap` column in Parquet: `seq_gap > 0` means at least one
packet was lost immediately before that row. A Grafana panel can track this directly.

### 1.2 Jetson → InfluxDB (stm_mission summary write)

| Source | Loss mode | Expected rate |
|--------|-----------|---------------|
| Network timeout (Tailscale, server unreachable) | `_write_mission_summary` exception → logged, not retried | ≤ 0.5 % per mission write (estimated; Tailscale route is stable) |
| InfluxDB overload | 429 / 503 from InfluxDB → exception → not retried | Rare in single-server deployment |

Mission summary writes are fire-and-forget on a daemon thread. A failed write means
the energy/packet summary is missing from InfluxDB but the Parquet file is unaffected.

### 1.3 Gateway crash / container restart

| Scenario | Packets at risk | Mitigation |
|----------|-----------------|------------|
| `data-engine` container restarts (Podman `restart: unless-stopped`) | In-memory buffer for the current MOVING mission | Buffer is lost; Parquet files already flushed survive (host bind-mount `./ram_buffer`) |
| Jetson power loss | Same as container restart | Parquet survives on ext4 bind-mount (fsync via `os.replace`) |
| Worst case | One entire MOVING run × 50 Hz × SHUTTLE_HARD_LIMIT packets | ≤ 4500 packets ≈ 1.5 min at 50 Hz |

### 1.4 Jetson → FL server (Flower booster bytes)

Flower uses TCP; no application-layer loss. If the round fails (server unreachable),
the round is retried by the fl-trigger on the next interval (`FL_TRIGGER_INTERVAL_S`).
The Parquet data remains on disk.

---

## 2 — Per-shuttle SLO

> **Target:** ≥ 99 % of MOVING packets emitted by the STM32 land in Parquet during
> a single mission, assuming no Jetson crash during the mission.

At 50 Hz MOVING with ≤ 1 % hop loss, a 1-minute mission (3000 packets) loses ~30
packets. Parquet rows for these are simply missing (observable via `seq_gap`). The
XGBoost model trains on the surviving 2970+ rows — a 1 % gap has no detectable
effect on anomaly labelling accuracy.

**SLO is not met if:** the Jetson crashes mid-mission. This is the only scenario with
> 1 % loss. Mitigation: the per-shuttle hard limit (`SHUTTLE_HARD_LIMIT`, default 4500
packets) triggers a mid-mission flush so at most the post-flush tail is lost.

---

## 3 — Grafana panel: `gateway_packet_loss_pct`

Flux query to compute per-shuttle loss rate from `seq_gap` in the most recent Parquet
files (loaded via `stm_mission` InfluxDB measurement — which reports `packets` received,
not packets emitted):

```flux
from(bucket: "alumet_energy")
  |> range(start: -1h)
  |> filter(fn: (r) => r["_measurement"] == "stm_mission")
  |> filter(fn: (r) => r["_field"] == "packets")
  |> last()
```

For a true seq_gap-based loss rate, query the Parquet files directly (e.g., via a
Grafana data source plugin or a scheduled Python script that exports an aggregate).
Alert threshold: loss rate > 2 % over a 10-minute window warrants investigation.

---

## 4 — Open items

| Item | Status |
|------|--------|
| Measure actual WiFi packet loss on the warehouse floor | Not done — needs hardware soak |
| Alert rule `gateway_packet_loss_pct > 2 %` in Grafana | Not configured |
| Retry stm_mission writes on transient InfluxDB failure | Deferred (daemon thread, low value) |
