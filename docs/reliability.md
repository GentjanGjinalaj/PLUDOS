# PLUDOS — End-to-End Packet-Loss Budget

Quantifies expected packet loss at each tier of the PLUDOS telemetry path and
defines the per-shuttle SLO. Numbers marked **estimated** lack hardware measurement;
those marked **design** follow from the protocol choice.

> **ADR-021 note.** This budget was written for the continuous live UDP stream,
> which is now **removed** — the radio is off except to drain finished captures.
> The per-hop *loss mechanisms* below still apply, but the packet rates and the
> "× 50 Hz" wall-time figures describe the superseded stream. Drain-path loss
> (bursty UDP on `:5684`, no ARQ yet) is not yet quantified — treat the numbers
> here as the legacy-stream model until the drain budget is measured.

---

## 1 — Loss points (by hop)

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
