"""
PLUDOS Edge Gateway: High-rate capture drain receiver (ADR-020/021)
--------------------------------------------------------------------
Second UDP receive path on port 5684 for the STM32 high-rate capture "drain".
The 24-byte live telemetry path on 5683 (`data-engine.py`) is untouched.

After a mission ends (MOVING→IDLE), the STM32 drains the raw ISM330DHCX FIFO
words it buffered in PSRAM as a burst of UDP datagrams: DRAIN_BEGIN (×3), then
all CHUNK packets back-to-back, then DRAIN_END (×3). See `docs/wire_protocol.md
§2` for the authoritative byte layout and `docs/sampling_strategy.md §12` for
the design intent.

PHASE 1 = BLAST + BEGIN-ACK: the only back-channel is an 8-byte liveness echo
(`DrainAck`, type 6) replied on each DRAIN_BEGIN so the shuttle knows the drain
landed (ADR-021 delivery evidence) — this is NOT selective-repeat ARQ. The
receiver reassembles by `chunk_seq`, validates each chunk's CRC32, and writes
one (or two) Parquet file(s) per `(shuttle_id, mission_id)` on DRAIN_END or a
quiet-timeout watchdog. If chunks are missing it still writes with
`all_packets_received=false` and records the gap ranges plus per-mission packet
counts. The code is structured (a `MissionReassembler` class, clear functions)
so the Phase-2 NAK selective-repeat ARQ (`sampling_strategy.md §9`) can be
layered on without reworking the frame parsing.

Each 7-byte FIFO word is `[tag, X_L, X_H, Y_L, Y_H, Z_L, Z_H]` — axes int16
little-endian. Demuxed by `tag = byte[0] >> 3` (0x02=accel, 0x01=gyro) into two
SEPARATE streams (no upsampling/padding of gyro to the accel rate). Per-sample
time is derived, never per-sample stamped:
`t_ms = t0_wall_ms + sample_index * 1000 / odr`, per stream from its own ODR.
"""

import asyncio
import logging
import os
import socket
import struct
import time
import zlib
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger("data-engine")

# ---------------------------------------------------------------------------
# Configuration — env overrides with safe defaults.
# ---------------------------------------------------------------------------

# Dedicated drain port (retired NC-UDP port, reused by ADR-015/020).
DRAIN_PORT = int(os.getenv("DRAIN_PORT", "5684"))

# Kernel RX buffer for the burst drain socket; capped by host net.core.rmem_max.
DRAIN_RCVBUF_BYTES = int(os.getenv("DRAIN_RCVBUF_BYTES", str(4 * 1024 * 1024)))

# Quiet-timeout watchdog: Phase 1 is blast-only with no guaranteed END, so a
# mission that has seen no new chunk for this long is finalised regardless.
DRAIN_QUIET_TIMEOUT_S = float(os.getenv("DRAIN_QUIET_TIMEOUT_S", "3.0"))

# Watchdog poll period — how often we scan reassemblers for quiet timeout.
_WATCHDOG_PERIOD_S = 1.0

# Idle-snapshot settling trim: the ISM330 LPF2 resets on ODR change, so the first
# samples after the sensor is enabled clip at the ±2g rail before the filter settles.
# At the idle 12.5 Hz ODR this lasts ~1 s (~12 samples); at the mission 3332 Hz ODR it
# is gone by sample 0, so the trim is applied to IDLE snapshots ONLY — a mission's
# onset transient is real signal we must keep. Drop this many ms off the head and shift
# t0_wall forward by the same amount so timestamps stay honest. Set 0 to disable.
IDLE_TRIM_MS = float(os.getenv("IDLE_TRIM_MS", "1000"))

# Recently-finalised dedup window. Late duplicate packets of a just-finalised
# drain arrive within ~seconds; a firmware reset re-using the same mission_id
# comes tens of seconds later (re-init WiFi + run + drain). This TTL tells them
# apart, so a post-reset (or post-IWDG-watchdog) drain is accepted instead of
# silently dropped as a duplicate. See ADR-021: firmware mission_id restarts at 0
# on every STM32 reset, so it is unique only within one boot session.
#
# Margin analysis (DESIGN_COUNCIL item 7):
#   - Drop-window (want covered): a late duplicate must be dropped. Worst case is
#     a retransmit straggler of the same drain — bounded by the burst duration
#     plus the 1–15 s pre-drain anti-collision jitter, so up to ~15 s after the
#     first copy lands. A 10 s TTL does NOT fully cover a 15 s straggler; the
#     residual risk is a second Parquet file for the same capture (the gw-assigned
#     gw_mission_id differs, so it is a distinct, harmless file — not corruption).
#   - Accept-window (want NOT dropped): a genuine post-reset re-use of mission_id=0
#     is only at risk if the shuttle resets, reconnects WiFi, runs, and finalises a
#     new drain within 10 s. The EMW3080 WiFi reconnect time dominates this gap and
#     is UNMEASURED — needs a bench measurement before tightening the TTL. Until
#     then 10 s is a deliberate middle ground: long enough to swallow normal late
#     duplicates, short enough that a realistic reconnect-bound reset clears it.
# Net: a fast watchdog reset finalising inside 10 s would be wrongly dropped; this
# is accepted as low-probability pending the reconnect-time measurement.
DEDUP_TTL_S = float(os.getenv("DEDUP_TTL_S", "10.0"))

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md §2 exactly. All little-endian.
# ---------------------------------------------------------------------------

# 0x52444C50 = ASCII "PLDR" in memory order P,L,D,R (little-endian u32).
DRAIN_MAGIC = 0x52444C50
DRAIN_PROTO_VERSION = 2  # current DrainBegin wire-format generation (matches firmware)

# Packet type bytes.
TYPE_BEGIN = 1
TYPE_CHUNK = 2
TYPE_END   = 3
# BEGIN liveness echo sent back to the shuttle so it knows the drain landed (ADR-021
# delivery evidence). NOT selective-repeat ARQ — just "someone is listening". Types
# 4/5 stay reserved for the future NAK/ACK_COMPLETE back-channel.
TYPE_ACK   = 6

# DrainBegin_t: magic(4) type(1) sid(1) mid(2) total_chunks(2) odr_a(2) odr_g(2)
#               temp_c_x100(2,int16) pressure_hpa_x10(2) is_idle_snapshot(1) _pad(1)
#               byte_count(4) word_count(4) t0_tick_ms(4) tx_tick_ms(4)         = 36 B (v1).
# v2 tail (DRAIN_PROTO_VERSION 2): protocol_version(1) skipped_since_last(1)
#               threshold_g2_x1000(2) jitter_ms(2)                              = 42 B total.
# Appended after the v1 fields so v1 offsets are unchanged — a stale v1 node's
# 36-byte BEGIN is detectable here by its short length (see _parse_packet).
BEGIN_FMT_V1 = "<IBBHHHHhHBBIIII"
BEGIN_FMT    = BEGIN_FMT_V1 + "BBHH"
BEGIN_SIZE_V1 = struct.calcsize(BEGIN_FMT_V1)
BEGIN_SIZE    = struct.calcsize(BEGIN_FMT)
assert BEGIN_SIZE_V1 == 36, f"DrainBegin v1 must be 36 bytes, got {BEGIN_SIZE_V1}"
assert BEGIN_SIZE == 42, f"DrainBegin v2 must be 42 bytes, got {BEGIN_SIZE}"

# Sentinels for an absent env stamp (MOVING missions / failed sensor read).
TEMP_INVALID_X100 = 0x7FFF
# Idle snapshots run both axes at 12.5 Hz; the integer wire field can't carry .5,
# so the receiver uses this authoritative rate whenever is_idle_snapshot is set.
IDLE_SNAP_ODR_HZ = 12.5

# DrainChunkHdr_t: magic(4) type(1) sid(1) mid(2) chunk_seq(2) total_chunks(2)
#                  payload_len(2) crc32(4) = 18 B header, then payload bytes.
CHUNK_HDR_FMT  = "<IBBHHHHI"
CHUNK_HDR_SIZE = struct.calcsize(CHUNK_HDR_FMT)
assert CHUNK_HDR_SIZE == 18, f"DrainChunk header must be 18 bytes, got {CHUNK_HDR_SIZE}"

# DrainEnd_t: magic(4) type(1) sid(1) mid(2) total_chunks(2) _pad(2) crc32_all(4) = 16 B.
END_FMT  = "<IBBHHHI"
END_SIZE = struct.calcsize(END_FMT)
assert END_SIZE == 16, f"DrainEnd must be 16 bytes, got {END_SIZE}"

# DrainAck_t: magic(4) type(1) sid(1) mid(2) = 8 B. Echoed to the sender on each BEGIN.
ACK_FMT  = "<IBBH"
ACK_SIZE = struct.calcsize(ACK_FMT)
assert ACK_SIZE == 8, f"DrainAck must be 8 bytes, got {ACK_SIZE}"

# FIFO word layout — 7 bytes per word: [tag, Xl, Xh, Yl, Yh, Zl, Zh].
FIFO_WORD_SIZE = 7
TAG_ACCEL = 0x02  # tag>>3 == XL_NC (accelerometer)
TAG_GYRO  = 0x01  # tag>>3 == GYRO_NC (gyroscope)

# Filename prefix so capture files never collide with live mission_s*.parquet.
CAP_PREFIX = "cap"

# ISM330DHCX raw-LSB → physical scaling. FS kept at ±2 g / ±250 dps in capture mode
# (firmware main.c §"Capture-mode sensor config"): accel 0.061 mg/LSB, gyro 8.75 mdps/LSB.
ACCEL_G_PER_LSB  = 0.000061   # 0.061 mg/LSB  (DS13281 ±2 g)
GYRO_DPS_PER_LSB = 0.00875    # 8.75 mdps/LSB (DS13281 ±250 dps)

# ---------------------------------------------------------------------------
# Output wiring — injected at startup to avoid a circular import with data-engine.
# ---------------------------------------------------------------------------

# Parquet output directory — same dir data-engine uses; injected at startup.
_buffer_dir = "."

# Callable (summary: dict) -> None invoked once per finalised drain so data-engine
# can mirror a per-mission summary point into InfluxDB (Grafana). Optional — None
# means parquet-only. Kept here (not an import of data-engine) to avoid a circular import.
_summary_sink = None


# Wire the output dir (+ optional Influx sink) from data-engine. Drain timestamps are
# self-contained (tx_tick - t0_tick), so no NTP-offset accessor is needed any more.
def configure(buffer_dir: str, summary_sink=None) -> None:
    global _buffer_dir, _summary_sink
    _buffer_dir = buffer_dir
    _summary_sink = summary_sink


# ---------------------------------------------------------------------------
# Reassembly — one instance per (shuttle_id, mission_id) in flight.
# ---------------------------------------------------------------------------

class MissionReassembler:
    """Accumulates CRC-validated chunks for one (shuttle_id, mission_id) drain.

    Holds chunk payloads keyed by chunk_seq, dedups duplicates, and assembles
    the full byte stream in order on finalisation. Designed so a Phase-2 NAK
    back-channel can read `missing_seqs()` and reply without touching parse logic."""

    def __init__(self, shuttle_id: int, mission_id: int, begin: dict) -> None:
        self.shuttle_id = shuttle_id
        self.mission_id = mission_id
        self.total_chunks = begin["total_chunks"]
        # Idle snapshots are 12.5 Hz on both axes (overrides the integer wire field).
        self.is_idle_snapshot = bool(begin.get("is_idle_snapshot", 0))
        if self.is_idle_snapshot:
            self.odr_accel_hz = IDLE_SNAP_ODR_HZ
            self.odr_gyro_hz = IDLE_SNAP_ODR_HZ
        else:
            self.odr_accel_hz = float(begin["odr_accel_hz"])
            self.odr_gyro_hz = float(begin["odr_gyro_hz"])
        # Env stamp (idle snapshots only); None when absent/invalid.
        temp_raw = begin.get("temp_c_x100", TEMP_INVALID_X100)
        self.temp_c = (temp_raw / 100.0) if temp_raw != TEMP_INVALID_X100 else None
        press_raw = begin.get("pressure_hpa_x10", 0)
        self.pressure_hpa = (press_raw / 10.0) if press_raw else None
        self.byte_count = begin["byte_count"]
        # FIFO word count — duration_ms = word_count*1000/(odr_a+odr_g); anchors t0 wall.
        self.word_count = begin["word_count"]
        self.t0_tick_ms = begin["t0_tick_ms"]
        # Transmit-time STM tick: tx_tick - t0_tick is the exact age of this data at drain.
        self.tx_tick_ms = begin["tx_tick_ms"]
        # v2 provenance tail (defaults cover a synthesised/v1 BEGIN): wire generation,
        # drains skipped since the last success (item 15), the MOVING label boundary in
        # g² (item 10), and the pre-drain anti-collision wait in ms (item 13).
        self.protocol_version = begin.get("protocol_version", 1)
        self.skipped_since_last = begin.get("skipped_since_last", 0)
        self.threshold_g2 = begin.get("threshold_g2_x1000", 0) / 1000.0
        self.jitter_ms = begin.get("jitter_ms", 0)
        # Wall-clock time the BEGIN arrived — the reference the capture age is subtracted from.
        self.begin_wall_ms = int(time.time() * 1000)
        # Gateway-assigned output id (unix ms). The firmware mission_id resets to
        # low values on every STM32 reset, so it is unsafe as a filename/dedup key
        # across reboots; this monotonic id is. Used for the parquet filename, the
        # mission_id column and the Influx summary — never for in-flight keying
        # (that still uses the firmware mission_id to separate back-to-back drains).
        self.gw_mission_id = self.begin_wall_ms
        # chunk_seq -> payload bytes (only CRC-valid, deduped).
        self.chunks: dict[int, bytes] = {}
        # Monotonic time of the most recent accepted chunk — drives quiet timeout.
        self.last_activity = time.monotonic()
        self.finalised = False
        # True only when all 3 BEGINs were lost and this reassembler was synthesised
        # from a chunk header. With no BEGIN, t0_tick/tx_tick are 0 so the capture age
        # collapses to 0 and every t_ms is anchored to BEGIN-arrival (off by the true
        # capture age). Surfaced as the t0_reconstructed Parquet column so consumers
        # can exclude these timestamps without conflating it with chunk-loss.
        self.t0_reconstructed = False

    # Store one CRC-valid chunk, ignoring duplicates. Returns True if newly stored.
    def add_chunk(self, chunk_seq: int, payload: bytes) -> bool:
        self.last_activity = time.monotonic()
        if chunk_seq in self.chunks:
            return False
        self.chunks[chunk_seq] = payload
        return True

    # True once every chunk_seq in [0, total_chunks) has been received.
    def is_complete(self) -> bool:
        return len(self.chunks) == self.total_chunks

    # Run-length string of missing chunk_seq ranges, e.g. "3-5,9" ("" if none).
    def missing_ranges(self) -> str:
        present = self.chunks.keys()
        missing = [s for s in range(self.total_chunks) if s not in present]
        if not missing:
            return ""
        ranges: list[str] = []
        start = prev = missing[0]
        for seq in missing[1:]:
            if seq == prev + 1:
                prev = seq
                continue
            ranges.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = seq
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        return ",".join(ranges)

    # Concatenate stored chunks in chunk_seq order — gaps simply skipped.
    def assemble(self) -> bytes:
        return b"".join(self.chunks[s] for s in sorted(self.chunks))

    # Map capture-start to wall clock. tx_tick - t0_tick is the STM-measured age of the
    # data at transmit (same boot, same HAL_GetTick — exact, includes idle-exit + WiFi
    # power-on). Subtract from BEGIN arrival to recover the real capture wall time. Works
    # identically for idle snapshots (unbounded PSRAM sit-time) and MOVING missions, with
    # no boot anchor and no reboot ambiguity (volatile PSRAM ⇒ both ticks are same-boot).
    def t0_wall_ms(self) -> int:
        capture_age_ms = max(0, self.tx_tick_ms - self.t0_tick_ms)
        return self.begin_wall_ms - capture_age_ms


# ---------------------------------------------------------------------------
# FIFO demux + Parquet write
# ---------------------------------------------------------------------------

# Split the raw FIFO byte stream into per-sensor int16 (x,y,z) lists by tag.
def _demux_fifo(stream: bytes) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    accel: list[tuple[int, int, int]] = []
    gyro: list[tuple[int, int, int]] = []
    # Trailing bytes shorter than one FIFO word are ignored (truncated tail).
    n_words = len(stream) // FIFO_WORD_SIZE
    for i in range(n_words):
        base = i * FIFO_WORD_SIZE
        tag = stream[base] >> 3
        # int16 little-endian, signed: low byte then high byte per axis.
        x, y, z = struct.unpack_from("<hhh", stream, base + 1)
        if tag == TAG_ACCEL:
            accel.append((x, y, z))
        elif tag == TAG_GYRO:
            gyro.append((x, y, z))
        # Unknown tags are silently dropped — neither accel nor gyro.
    return accel, gyro


# Build a per-stream DataFrame with derived time + mission metadata, then write Parquet.
def _write_stream_parquet(
    sensor: str,
    samples: list[tuple[int, int, int]],
    odr_hz: int,
    re: MissionReassembler,
    complete: bool,
    missing: str,
    t0: int,
    trimmed: int = 0,
) -> str | None:
    if not samples:
        # No samples of this sensor in the stream — skip the file entirely.
        return None

    # Derived time: t_ms = t0 + index * 1000 / odr (per stream, its own ODR).
    # Guard odr==0 (corrupt BEGIN) by leaving t_ms at t0 for all samples.
    step_ms = (1000.0 / odr_hz) if odr_hz > 0 else 0.0

    # Idle settling trim is applied once in _finalise_mission before this call, so
    # `samples`/`t0` arrive already trimmed; `trimmed` is the dropped-sample count
    # carried through only for the [STORAGE] log line below.
    n = len(samples)

    df = pd.DataFrame(
        {
            "sample_index": range(n),
            "t_ms": [t0 + i * step_ms for i in range(n)],
            "x": [s[0] for s in samples],
            "y": [s[1] for s in samples],
            "z": [s[2] for s in samples],
        }
    )
    # Compact dtypes: int16 raw axes (at ISM330 FS scale), int32 index.
    df["sample_index"] = df["sample_index"].astype("int32")
    df["x"] = df["x"].astype("int16")
    df["y"] = df["y"].astype("int16")
    df["z"] = df["z"].astype("int16")
    # Mission metadata — constant per file, broadcast across all rows.
    df["shuttle_id"] = pd.array([re.shuttle_id] * n, dtype="int16")
    df["mission_id"] = pd.array([re.gw_mission_id] * n, dtype="int64")
    # float ODR columns — idle snapshots carry 12.5 Hz, which int can't represent.
    df["odr_accel_hz"] = pd.array([re.odr_accel_hz] * n, dtype="float64")
    df["odr_gyro_hz"] = pd.array([re.odr_gyro_hz] * n, dtype="float64")
    df["t0_wall_ms"] = pd.array([t0] * n, dtype="int64")
    # Capture mode + env stamp (idle snapshots only; NaN/False for MOVING missions).
    df["is_idle_snapshot"] = re.is_idle_snapshot
    df["temp_c"] = pd.array(
        [re.temp_c if re.temp_c is not None else float("nan")] * n, dtype="float32"
    )
    df["pressure_hpa"] = pd.array(
        [re.pressure_hpa if re.pressure_hpa is not None else float("nan")] * n,
        dtype="float32",
    )
    # Packet accounting — each drain chunk is one UDP datagram; counts are per-mission.
    received = len(re.chunks)
    lost = re.total_chunks - received
    loss_pct = (100.0 * lost / re.total_chunks) if re.total_chunks else 0.0
    df["all_packets_received"] = complete
    # Honesty flag for the lost-BEGIN synth path: when True, t_ms/t0_wall_ms are
    # anchored to BEGIN-arrival (not true capture time) and are off by the capture
    # age. Kept distinct from all_packets_received (chunk-loss) — a synth drain can
    # still receive every chunk.
    df["t0_reconstructed"] = bool(re.t0_reconstructed)
    # v2 BEGIN provenance, broadcast per-mission so the offline corpus is self-describing:
    # threshold_g2 = MOVING label boundary (item 10), jitter_ms = pre-drain wait used to
    # undo cross-shuttle t0 skew (item 13). 0 / version 1 on a synth or v1 BEGIN.
    df["protocol_version"] = pd.array([re.protocol_version] * n, dtype="int16")
    df["threshold_g2"] = pd.array([re.threshold_g2] * n, dtype="float32")
    df["jitter_ms"] = pd.array([re.jitter_ms] * n, dtype="int32")
    df["packets_total"] = pd.array([re.total_chunks] * n, dtype="int32")
    df["packets_received"] = pd.array([received] * n, dtype="int32")
    df["packets_lost"] = pd.array([lost] * n, dtype="int32")
    df["packet_loss_pct"] = pd.array([round(loss_pct, 2)] * n, dtype="float32")
    df["missing_chunk_ranges"] = missing

    file_path = os.path.join(
        _buffer_dir, f"{CAP_PREFIX}_{sensor}_s{re.shuttle_id}_m{re.gw_mission_id}.parquet"
    )
    tmp_path = file_path + ".tmp"
    # PyArrow write is sync but only fires on mission-end finalisation — acceptable.
    df.to_parquet(tmp_path, engine="pyarrow", index=False,
                  compression="zstd", compression_level=3)
    os.replace(tmp_path, file_path)  # atomic rename: crash-safe on Linux
    # Local-storage write log — Parquet (SD card) and the InfluxDB summary are two
    # independent per-drain writes; this line makes the SD write visible alongside
    # the [INFLUXDB] summary line so it's clear both fired.
    logger.info(
        "[STORAGE] wrote %s | %s | %d %s samples%s | %d KB",
        os.path.basename(file_path), _tag(re.shuttle_id, re.gw_mission_id, re.is_idle_snapshot),
        n, sensor,
        f" (trimmed {trimmed} settling)" if trimmed else "",
        os.path.getsize(file_path) // 1024,
    )
    return file_path


# Log tag: [shuttleN] iK for idle snapshots, mK for moving missions, #K when kind unknown.
def _tag(shuttle_id: int, mission_id: int, is_idle=None) -> str:
    prefix = "#" if is_idle is None else ("i" if is_idle else "m")
    return f"[shuttle{shuttle_id}] {prefix}{mission_id}"


# Magnitude RMS + peak (physical units) from raw int16 (x,y,z) samples — vibration
# intensity for the Grafana per-mission summary. Returns (nan, nan) for an empty stream.
def _mag_stats(samples: list[tuple[int, int, int]], unit_per_lsb: float) -> tuple[float, float]:
    if not samples:
        return (float("nan"), float("nan"))
    a = np.asarray(samples, dtype=np.float64)
    mag = np.sqrt((a * a).sum(axis=1)) * unit_per_lsb
    return (float(np.sqrt((mag * mag).mean())), float(mag.max()))


# Drop IDLE_TRIM_MS of LPF2 rail-clip settling off the head of an idle-snapshot stream.
# Returns (trimmed_samples, dropped_n); never empties the stream. Applied once per drain
# so Parquet, the InfluxDB summary, and the vibration stats all share the same trim.
def _trim_idle_settling(
    samples: list[tuple[int, int, int]], odr_hz: float
) -> tuple[list[tuple[int, int, int]], int]:
    if not samples or IDLE_TRIM_MS <= 0 or odr_hz <= 0:
        return samples, 0
    step_ms = 1000.0 / odr_hz
    trim_n = int(IDLE_TRIM_MS / step_ms)
    # Never trim away the whole snapshot — keep at least one sample.
    trim_n = min(trim_n, max(0, len(samples) - 1))
    if trim_n <= 0:
        return samples, 0
    return samples[trim_n:], trim_n


# Finalise one capture: assemble bytes, demux, write accel + gyro Parquet files.
def _finalise_mission(re: MissionReassembler, reason: str) -> None:
    if re.finalised:
        return
    re.finalised = True

    complete = re.is_complete()
    missing = re.missing_ranges()
    stream = re.assemble()
    accel, gyro = _demux_fifo(stream)

    # Anchor once; idempotent, so both parquet writers + the summary share one mission t0.
    t0_wall = re.t0_wall_ms()

    # Idle-only settling trim, applied ONCE here so Parquet, the InfluxDB summary, and the
    # vibration stats (_mag_stats below) all use identical trimmed samples and t0. MOVING
    # missions keep their onset transient (real signal). Both idle axes run at
    # IDLE_SNAP_ODR_HZ, so the accel/gyro trim counts match and the shared t0 advance is
    # unambiguous and keeps the per-sample waveform's accel[i]/gyro[i] pairing aligned.
    accel_trim = gyro_trim = 0
    if re.is_idle_snapshot:
        accel, accel_trim = _trim_idle_settling(accel, re.odr_accel_hz)
        gyro,  gyro_trim  = _trim_idle_settling(gyro,  re.odr_gyro_hz)
        if accel_trim > 0 and re.odr_accel_hz > 0:
            t0_wall += int(round(accel_trim * 1000.0 / re.odr_accel_hz))

    paths = []
    a_path = _write_stream_parquet("accel", accel, re.odr_accel_hz, re, complete, missing, t0_wall, accel_trim)
    g_path = _write_stream_parquet("gyro", gyro, re.odr_gyro_hz, re, complete, missing, t0_wall, gyro_trim)
    if a_path:
        paths.append(os.path.basename(a_path))
    if g_path:
        paths.append(os.path.basename(g_path))

    received = len(re.chunks)
    lost = re.total_chunks - received
    loss_pct = (100.0 * lost / re.total_chunks) if re.total_chunks else 0.0
    # Log capture wall-clock (t0_wall) not arrival time — the line is else misleading:
    # a drain arrives now but the data was captured when the mission ran. Rendered in
    # local time to line up with the log line's own local timestamp (Influx/Parquet
    # still store UTC; this is display-only).
    captured = datetime.fromtimestamp(t0_wall / 1000.0).strftime("%H:%M:%S")
    logger.info(
        "[DRAIN] finalised (%s) %s | captured %s | packets %d/%d recv (lost %d, %.1f%%) | "
        "accel=%d gyro=%d | all_received=%s%s | %s",
        reason, _tag(re.shuttle_id, re.mission_id, re.is_idle_snapshot), captured,
        received, re.total_chunks, lost, loss_pct,
        len(accel), len(gyro), complete,
        "" if complete else f" missing_seq=[{missing}]",
        ", ".join(paths) if paths else "(no samples)",
    )

    # Vibration intensity for Grafana — magnitude RMS/peak in physical units.
    accel_rms, accel_peak = _mag_stats(accel, ACCEL_G_PER_LSB)
    _,         gyro_peak  = _mag_stats(gyro,  GYRO_DPS_PER_LSB)

    # Mirror a per-mission summary into InfluxDB (Grafana). Sink failure must never
    # break the drain path, so swallow everything — parquet is already written above.
    if _summary_sink is not None:
        try:
            _summary_sink({
                "shuttle_id":       re.shuttle_id,
                "mission_id":       re.gw_mission_id,
                # Gateway-clock reception window (NOT capture time): begin_wall_ms is when
                # DRAIN_BEGIN landed, recv_end_ms is finalisation now. These bound the
                # interval during which the Jetson radio/CPU did the drain RX+reassembly,
                # so a Grafana panel can integrate INA3221 power over [start,end] to get the
                # gateway energy cost of receiving this drain (distinct from t0_wall_ms,
                # which is back-dated to when the shuttle captured the data).
                "recv_start_ms":    re.begin_wall_ms,
                "recv_end_ms":      int(time.time() * 1000),
                "is_idle_snapshot": re.is_idle_snapshot,
                "packets_total":    re.total_chunks,
                "packets_received": received,
                "packets_lost":     lost,
                "loss_pct":         loss_pct,
                "accel_samples":    len(accel),
                "gyro_samples":     len(gyro),
                "complete":         complete,
                "temp_c":           re.temp_c,
                "pressure_hpa":     re.pressure_hpa,
                "t0_wall_ms":       t0_wall,
                "accel_rms_g":      accel_rms,
                "accel_peak_g":     accel_peak,
                "gyro_peak_dps":    gyro_peak,
                # v2 BEGIN provenance (items 10/13/15) — see MissionReassembler.
                "protocol_version":   re.protocol_version,
                "skipped_since_last": re.skipped_since_last,
                "threshold_g2":       re.threshold_g2,
                "jitter_ms":          re.jitter_ms,
                # Idle snapshots are small (~12.5 Hz) — carry the trimmed samples so
                # data-engine can write a per-sample waveform to Influx that matches the
                # Parquet file; None for high-rate missions.
                "odr_hz":           re.odr_accel_hz,
                "accel_xyz":        accel if re.is_idle_snapshot else None,
                "gyro_xyz":         gyro  if re.is_idle_snapshot else None,
            })
        except Exception as exc:
            logger.warning("[DRAIN] summary sink failed (%s): %s",
                           _tag(re.shuttle_id, re.mission_id, re.is_idle_snapshot), exc)


# ---------------------------------------------------------------------------
# Packet parsing — defensive: skip anything malformed.
# ---------------------------------------------------------------------------

# Validate magic + type, return (type, fields_dict) or None for bad/unknown packets.
def _parse_packet(data: bytes):
    if len(data) < 5:
        return None
    magic, ptype = struct.unpack_from("<IB", data, 0)
    if magic != DRAIN_MAGIC:
        return None

    if ptype == TYPE_BEGIN:
        # Accept the v1 prefix even from a stale (un-reflashed) v1 node, but warn —
        # the v2 provenance tail (proto/threshold/jitter/skip) is then unavailable.
        if len(data) < BEGIN_SIZE_V1:
            return None
        (_, _, sid, mid, total, odr_a, odr_g, temp_x100, press_x10, is_idle, _pad,
         byte_count, word_count, t0_tick, tx_tick) = struct.unpack_from(BEGIN_FMT_V1, data, 0)
        if len(data) >= BEGIN_SIZE:
            proto, skipped, thr_x1000, jitter_ms = struct.unpack_from("<BBHH", data, BEGIN_SIZE_V1)
        else:
            logger.warning("[DRAIN] v1 BEGIN from shuttle %s (%d B) — node needs reflash to v%d",
                           sid, len(data), DRAIN_PROTO_VERSION)
            proto, skipped, thr_x1000, jitter_ms = 1, 0, 0, 0
        return (TYPE_BEGIN, {
            "shuttle_id": sid, "mission_id": mid, "total_chunks": total,
            "odr_accel_hz": odr_a, "odr_gyro_hz": odr_g,
            "temp_c_x100": temp_x100, "pressure_hpa_x10": press_x10,
            "is_idle_snapshot": is_idle,
            "byte_count": byte_count, "word_count": word_count,
            "t0_tick_ms": t0_tick, "tx_tick_ms": tx_tick,
            "protocol_version": proto, "skipped_since_last": skipped,
            "threshold_g2_x1000": thr_x1000, "jitter_ms": jitter_ms,
        })

    if ptype == TYPE_CHUNK:
        if len(data) < CHUNK_HDR_SIZE:
            return None
        (_, _, sid, mid, chunk_seq, total, plen, crc) = struct.unpack_from(CHUNK_HDR_FMT, data, 0)
        payload = data[CHUNK_HDR_SIZE:CHUNK_HDR_SIZE + plen]
        # payload_len must match what actually arrived — reject truncated datagrams.
        if len(payload) != plen:
            return None
        return (TYPE_CHUNK, {
            "shuttle_id": sid, "mission_id": mid, "chunk_seq": chunk_seq,
            "total_chunks": total, "crc32": crc, "payload": payload,
        })

    if ptype == TYPE_END:
        if len(data) < END_SIZE:
            return None
        (_, _, sid, mid, total, _pad, crc_all) = struct.unpack_from(END_FMT, data, 0)
        return (TYPE_END, {
            "shuttle_id": sid, "mission_id": mid,
            "total_chunks": total, "crc32_all": crc_all,
        })

    # Unknown type (e.g. future NAK/ACK on this port) — ignore.
    return None


# ---------------------------------------------------------------------------
# UDP protocol — drain control + chunks on port 5684
# ---------------------------------------------------------------------------

class DrainProtocol(asyncio.DatagramProtocol):
    """Asyncio datagram handler for the high-rate capture drain on port 5684."""

    def __init__(self) -> None:
        # In-flight reassemblers keyed by (shuttle_id, firmware_mission_id).
        self.missions: dict[tuple[int, int], MissionReassembler] = {}
        # Recently-finalised (shuttle_id, firmware_mission_id) -> monotonic finalise
        # time. TTL-expired (DEDUP_TTL_S) so a firmware reset re-using a low
        # mission_id is accepted as a new drain, not dropped as a late duplicate.
        self.recent_done: dict[tuple[int, int], float] = {}
        # Set in connection_made; used to echo the BEGIN liveness ack to the sender.
        self.transport: asyncio.DatagramTransport | None = None

    # Capture the transport so we can reply to the sender (BEGIN liveness echo).
    def connection_made(self, transport) -> None:
        self.transport = transport

    # Echo an 8-byte DrainAck to the shuttle so it knows this BEGIN was received
    # (ADR-021 delivery evidence). Best-effort: a failed send must never break ingest.
    def _send_ack(self, sid: int, mid: int, addr) -> None:
        if self.transport is None:
            return
        try:
            self.transport.sendto(struct.pack(ACK_FMT, DRAIN_MAGIC, TYPE_ACK, sid, mid), addr)
        except Exception as exc:
            logger.debug("[DRAIN] ack send failed %s: %s", _tag(sid, mid), exc)

    # True if key was finalised within DEDUP_TTL_S — a late duplicate of a still-
    # fresh drain. Expired entries are pruned so the id is re-usable after a reset.
    def _recently_done(self, key) -> bool:
        t = self.recent_done.get(key)
        if t is None:
            return False
        if (time.monotonic() - t) < DEDUP_TTL_S:
            return True
        del self.recent_done[key]
        return False

    def datagram_received(self, data: bytes, addr) -> None:
        parsed = _parse_packet(data)
        if parsed is None:
            # Bad magic / wrong size / unknown type — drop silently (debug only).
            logger.debug("[DRAIN] dropping unparseable %d-byte packet from %s", len(data), addr)
            return
        ptype, f = parsed
        key = (f["shuttle_id"], f["mission_id"])

        if ptype == TYPE_BEGIN:
            self._on_begin(key, f, addr)
        elif ptype == TYPE_CHUNK:
            self._on_chunk(key, f, addr)
        elif ptype == TYPE_END:
            self._on_end(key, f, addr)

    # DRAIN_BEGIN: create the reassembler (idempotent — sent ×3 for robustness).
    def _on_begin(self, key, f, addr) -> None:
        # Echo on every BEGIN copy (×3) so the shuttle gets up to three chances to see
        # the liveness ack even if one is lost in the post-power-on window. Sent before
        # the dedup/duplicate returns precisely so duplicates still echo.
        self._send_ack(f["shuttle_id"], f["mission_id"], addr)
        if self._recently_done(key):
            return  # late re-announce of a just-finalised drain; ignore.
        if key in self.missions:
            return  # duplicate BEGIN (sent ×3) — keep the first.
        re = MissionReassembler(f["shuttle_id"], f["mission_id"], f)
        self.missions[key] = re
        kind = "idle-snapshot" if re.is_idle_snapshot else "mission"
        logger.info(
            "[DRAIN] BEGIN %s (%s) | chunks=%d odr_a=%g odr_g=%g bytes=%d "
            "temp=%s press=%s from %s",
            _tag(f["shuttle_id"], f["mission_id"], re.is_idle_snapshot), kind, f["total_chunks"],
            re.odr_accel_hz, re.odr_gyro_hz, f["byte_count"],
            f"{re.temp_c:.2f}C" if re.temp_c is not None else "n/a",
            f"{re.pressure_hpa:.1f}hPa" if re.pressure_hpa is not None else "n/a",
            addr,
        )

    # CHUNK: validate CRC32, dedup, store. Tolerates a missing BEGIN by
    # synthesising a minimal reassembler from the chunk header's total_chunks.
    def _on_chunk(self, key, f, addr) -> None:
        if self._recently_done(key):
            return  # drain already written — drop late chunk.
        # CRC32 over payload bytes only (zlib/IEEE) — drop on mismatch.
        if zlib.crc32(f["payload"]) != f["crc32"]:
            logger.debug(
                "[DRAIN] CRC fail %s seq=%d — dropping", _tag(*key), f["chunk_seq"]
            )
            return
        re = self.missions.get(key)
        if re is None:
            # BEGIN lost (all 3 dropped): synthesise so chunks aren't wasted.
            # ODRs unknown here → 0; t0 falls back to arrival wall time.
            synth = {
                "total_chunks": f["total_chunks"], "odr_accel_hz": 0, "odr_gyro_hz": 0,
                "temp_c_x100": TEMP_INVALID_X100, "pressure_hpa_x10": 0, "is_idle_snapshot": 0,
                "byte_count": 0, "word_count": 0, "t0_tick_ms": 0, "tx_tick_ms": 0,
            }
            re = MissionReassembler(f["shuttle_id"], f["mission_id"], synth)
            re.t0_reconstructed = True
            self.missions[key] = re
            logger.warning(
                "[DRAIN] CHUNK before BEGIN %s — synthesising reassembler (odr unknown, "
                "t0 falls back to arrival time; timestamps off by capture age, "
                "t0_reconstructed=True)",
                _tag(*key),
            )
        re.add_chunk(f["chunk_seq"], f["payload"])

    # DRAIN_END: finalise immediately (sent ×3 — first one wins).
    def _on_end(self, key, f, addr) -> None:
        if self._recently_done(key):
            return
        re = self.missions.get(key)
        if re is None:
            return  # END with no BEGIN/chunks seen — nothing to write.
        # Mark done + remove BEFORE the (blocking) parquet write, then run that write
        # off the event loop. A back-to-back multi-mission drain (one wake drains the
        # recovered mission + the new one) would otherwise stall here: the sync
        # to_parquet blocks the loop so the next mission's BEGIN can't be acked in
        # time, and the shuttle skips its blast. Off-loop keeps the ack path live.
        self.recent_done[key] = time.monotonic()
        self.missions.pop(key, None)
        self._finalise_offloop(re, "END")
        # TODO ARQ phase 2: instead of finalising here, compute re.missing_ranges()
        # and reply to `addr` with a NAK (type 4) listing missing chunk_seq ranges,
        # or ACK_COMPLETE (type 5) if re.is_complete(). Only finalise after
        # ACK_COMPLETE or a bounded retransmit-round cap.

    # Run the sync parquet finalisation in a worker thread so the asyncio loop stays
    # free to service the socket (esp. ack the next mission's BEGIN). The reassembler
    # is already popped from self.missions, so no other path touches `re` concurrently.
    def _finalise_offloop(self, re: MissionReassembler, reason: str) -> None:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, _finalise_mission, re, reason)
        fut.add_done_callback(_log_finalise_exc)

    def error_received(self, exc: Exception) -> None:
        logger.error("[DRAIN] UDP socket error: %s", exc)


# ---------------------------------------------------------------------------
# Quiet-timeout watchdog — Phase 1 blast may lose all 3 END packets.
# ---------------------------------------------------------------------------

# Log a swallowed exception from an off-loop _finalise_mission worker.
def _log_finalise_exc(fut: "asyncio.Future") -> None:
    exc = fut.exception()
    if exc is not None:
        logger.error("[DRAIN] finalise (off-loop) failed: %s", exc)


# Finalise any mission that has seen no new chunk for DRAIN_QUIET_TIMEOUT_S.
async def _drain_watchdog(proto: DrainProtocol) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(_WATCHDOG_PERIOD_S)
        now = time.monotonic()
        for key in list(proto.missions.keys()):
            re = proto.missions.get(key)
            if re is None:
                continue
            if (now - re.last_activity) >= DRAIN_QUIET_TIMEOUT_S:
                # Pop first, then write off-loop — same reason as _on_end: keep the
                # event loop free to ack incoming BEGINs during the parquet write.
                proto.recent_done[key] = now
                proto.missions.pop(key, None)
                await loop.run_in_executor(None, _finalise_mission, re, "quiet-timeout")
        # Prune expired dedup entries so the map can't grow unbounded over a session.
        for key in [k for k, t in proto.recent_done.items() if (now - t) >= DEDUP_TTL_S]:
            del proto.recent_done[key]


# ---------------------------------------------------------------------------
# Entry point — called from data-engine.py main()
# ---------------------------------------------------------------------------

# Bind the drain UDP endpoint and start the quiet-timeout watchdog task.
async def start_drain_receiver(buffer_dir: str, summary_sink=None) -> None:
    configure(buffer_dir, summary_sink)
    loop = asyncio.get_running_loop()
    proto = DrainProtocol()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: proto,
        local_addr=("0.0.0.0", DRAIN_PORT),
    )
    # A drain is a multi-MB burst sent with no inter-chunk pacing; the default ~208 KB
    # kernel RX buffer overflows mid-burst if the event loop stalls (e.g. a prior
    # mission's Parquet write), dropping a consecutive run of chunks. Request 4 MB so
    # a full mission fits even if the loop briefly can't service the socket. Capped by
    # net.core.rmem_max — must be raised on the host (host netns) for this to take.
    sock = transport.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, DRAIN_RCVBUF_BYTES)
        actual = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        logger.info("[DRAIN] SO_RCVBUF requested=%d granted=%d", DRAIN_RCVBUF_BYTES, actual)
        # Linux reports back ~2× the requested value (bookkeeping overhead), so a fully
        # honoured request gives granted ≥ requested. granted < requested means the kernel
        # clamped to net.core.rmem_max — the 4 MB buffer we asked for is not in effect and
        # drains can still overflow mid-burst. The container runs with network_mode: host,
        # so this must be raised on the Jetson host, not via a compose sysctl.
        if actual < DRAIN_RCVBUF_BYTES:
            logger.warning(
                "[DRAIN] SO_RCVBUF clamped to %d < requested %d — raise host rmem_max: "
                "`sudo sysctl -w net.core.rmem_max=%d` (persist in /etc/sysctl.d/)",
                actual, DRAIN_RCVBUF_BYTES, DRAIN_RCVBUF_BYTES,
            )
    logger.info("[DRAIN] high-rate capture drain listener bound on port %d", DRAIN_PORT)
    asyncio.create_task(_drain_watchdog(proto))
