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

PHASE 1 = BLAST-ONLY: no NAK/ACK back-channel yet. The receiver reassembles by
`chunk_seq`, validates each chunk's CRC32, and writes one (or two) Parquet
file(s) per `(shuttle_id, mission_id)` on DRAIN_END or a quiet-timeout watchdog.
If chunks are missing it still writes with `complete=false` and records the gap
ranges. The code is structured (a `MissionReassembler` class, clear functions)
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
import struct
import time
import zlib

import pandas as pd

logger = logging.getLogger("data-engine")

# ---------------------------------------------------------------------------
# Configuration — env overrides with safe defaults.
# ---------------------------------------------------------------------------

# Dedicated drain port (retired NC-UDP port, reused by ADR-015/020).
DRAIN_PORT = int(os.getenv("DRAIN_PORT", "5684"))

# Quiet-timeout watchdog: Phase 1 is blast-only with no guaranteed END, so a
# mission that has seen no new chunk for this long is finalised regardless.
DRAIN_QUIET_TIMEOUT_S = float(os.getenv("DRAIN_QUIET_TIMEOUT_S", "3.0"))

# Watchdog poll period — how often we scan reassemblers for quiet timeout.
_WATCHDOG_PERIOD_S = 1.0

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md §2 exactly. All little-endian.
# ---------------------------------------------------------------------------

# 0x52444C50 = ASCII "PLDR" in memory order P,L,D,R (little-endian u32).
DRAIN_MAGIC = 0x52444C50

# Packet type bytes.
TYPE_BEGIN = 1
TYPE_CHUNK = 2
TYPE_END   = 3

# DrainBegin_t: magic(4) type(1) sid(1) mid(2) total_chunks(2) odr_a(2)
#               odr_g(2) _pad(2) byte_count(4) word_count(4) t0_tick_ms(4) = 28 B.
BEGIN_FMT  = "<IBBHHHHHIII"
BEGIN_SIZE = struct.calcsize(BEGIN_FMT)
assert BEGIN_SIZE == 28, f"DrainBegin must be 28 bytes, got {BEGIN_SIZE}"

# DrainChunkHdr_t: magic(4) type(1) sid(1) mid(2) chunk_seq(2) total_chunks(2)
#                  payload_len(2) crc32(4) = 18 B header, then payload bytes.
CHUNK_HDR_FMT  = "<IBBHHHHI"
CHUNK_HDR_SIZE = struct.calcsize(CHUNK_HDR_FMT)
assert CHUNK_HDR_SIZE == 18, f"DrainChunk header must be 18 bytes, got {CHUNK_HDR_SIZE}"

# DrainEnd_t: magic(4) type(1) sid(1) mid(2) total_chunks(2) _pad(2) crc32_all(4) = 16 B.
END_FMT  = "<IBBHHHI"
END_SIZE = struct.calcsize(END_FMT)
assert END_SIZE == 16, f"DrainEnd must be 16 bytes, got {END_SIZE}"

# FIFO word layout — 7 bytes per word: [tag, Xl, Xh, Yl, Yh, Zl, Zh].
FIFO_WORD_SIZE = 7
TAG_ACCEL = 0x02  # tag>>3 == XL_NC (accelerometer)
TAG_GYRO  = 0x01  # tag>>3 == GYRO_NC (gyroscope)

# Filename prefix so capture files never collide with live mission_s*.parquet.
CAP_PREFIX = "cap"

# ---------------------------------------------------------------------------
# t0 wall-clock resolution — reuse the per-shuttle NTP offset data-engine.py
# already maintains. Injected at startup to avoid a circular import.
# ---------------------------------------------------------------------------

# Callable (shuttle_id:int) -> int|None returning the per-shuttle NTP offset (ms),
# or None if no offset has been anchored for that shuttle yet.
_ntp_offset_lookup = None

# Parquet output directory — same dir data-engine uses; injected at startup.
_buffer_dir = "."


# Wire the NTP-offset accessor + output dir from data-engine before serving.
def configure(ntp_offset_lookup, buffer_dir: str) -> None:
    global _ntp_offset_lookup, _buffer_dir
    _ntp_offset_lookup = ntp_offset_lookup
    _buffer_dir = buffer_dir


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
        self.odr_accel_hz = begin["odr_accel_hz"]
        self.odr_gyro_hz = begin["odr_gyro_hz"]
        self.byte_count = begin["byte_count"]
        self.t0_tick_ms = begin["t0_tick_ms"]
        # Wall-clock time the BEGIN arrived — fallback t0 anchor if no NTP offset.
        self.begin_wall_ms = int(time.time() * 1000)
        # chunk_seq -> payload bytes (only CRC-valid, deduped).
        self.chunks: dict[int, bytes] = {}
        # Monotonic time of the most recent accepted chunk — drives quiet timeout.
        self.last_activity = time.monotonic()
        self.finalised = False

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

    # Map mission-start t0_tick_ms to wall clock via the per-shuttle NTP offset
    # data-engine maintains; fall back to BEGIN arrival time (approximation).
    def t0_wall_ms(self) -> int:
        if _ntp_offset_lookup is not None:
            offset = _ntp_offset_lookup(self.shuttle_id)
            if offset is not None:
                return self.t0_tick_ms + offset
        # Fallback: shuttle never streamed on 5683 this session, so no NTP anchor
        # exists — use BEGIN arrival wall time as an approximate mission t0.
        return self.begin_wall_ms


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
) -> str | None:
    if not samples:
        # No samples of this sensor in the stream — skip the file entirely.
        return None

    t0 = re.t0_wall_ms()
    # Derived time: t_ms = t0 + index * 1000 / odr (per stream, its own ODR).
    # Guard odr==0 (corrupt BEGIN) by leaving t_ms at t0 for all samples.
    step_ms = (1000.0 / odr_hz) if odr_hz > 0 else 0.0
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
    df["mission_id"] = pd.array([re.mission_id] * n, dtype="int32")
    df["odr_accel_hz"] = pd.array([re.odr_accel_hz] * n, dtype="int32")
    df["odr_gyro_hz"] = pd.array([re.odr_gyro_hz] * n, dtype="int32")
    df["t0_wall_ms"] = pd.array([t0] * n, dtype="int64")
    df["complete"] = complete
    df["missing_chunk_ranges"] = missing

    file_path = os.path.join(
        _buffer_dir, f"{CAP_PREFIX}_{sensor}_s{re.shuttle_id}_m{re.mission_id}.parquet"
    )
    tmp_path = file_path + ".tmp"
    # PyArrow write is sync but only fires on mission-end finalisation — acceptable.
    df.to_parquet(tmp_path, engine="pyarrow", index=False,
                  compression="zstd", compression_level=3)
    os.replace(tmp_path, file_path)  # atomic rename: crash-safe on Linux
    return file_path


# Finalise one mission: assemble bytes, demux, write accel + gyro Parquet files.
def _finalise_mission(re: MissionReassembler, reason: str) -> None:
    if re.finalised:
        return
    re.finalised = True

    complete = re.is_complete()
    missing = re.missing_ranges()
    stream = re.assemble()
    accel, gyro = _demux_fifo(stream)

    paths = []
    a_path = _write_stream_parquet("accel", accel, re.odr_accel_hz, re, complete, missing)
    g_path = _write_stream_parquet("gyro", gyro, re.odr_gyro_hz, re, complete, missing)
    if a_path:
        paths.append(os.path.basename(a_path))
    if g_path:
        paths.append(os.path.basename(g_path))

    logger.info(
        "[DRAIN] mission finalised (%s) s%d m%d | chunks %d/%d | accel=%d gyro=%d | "
        "complete=%s%s | %s",
        reason, re.shuttle_id, re.mission_id, len(re.chunks), re.total_chunks,
        len(accel), len(gyro), complete,
        "" if complete else f" missing=[{missing}]",
        ", ".join(paths) if paths else "(no samples)",
    )


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
        if len(data) < BEGIN_SIZE:
            return None
        (_, _, sid, mid, total, odr_a, odr_g, _pad,
         byte_count, word_count, t0_tick) = struct.unpack_from(BEGIN_FMT, data, 0)
        return (TYPE_BEGIN, {
            "shuttle_id": sid, "mission_id": mid, "total_chunks": total,
            "odr_accel_hz": odr_a, "odr_gyro_hz": odr_g,
            "byte_count": byte_count, "word_count": word_count, "t0_tick_ms": t0_tick,
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
        # In-flight reassemblers keyed by (shuttle_id, mission_id).
        self.missions: dict[tuple[int, int], MissionReassembler] = {}
        # Completed (shuttle_id, mission_id) keys — reject late/duplicate drains.
        self.done: set[tuple[int, int]] = set()

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
        if key in self.done:
            return  # already finalised this mission; ignore re-announce.
        if key in self.missions:
            return  # duplicate BEGIN (sent ×3) — keep the first.
        self.missions[key] = MissionReassembler(f["shuttle_id"], f["mission_id"], f)
        logger.info(
            "[DRAIN] BEGIN s%d m%d | chunks=%d odr_a=%d odr_g=%d bytes=%d from %s",
            f["shuttle_id"], f["mission_id"], f["total_chunks"],
            f["odr_accel_hz"], f["odr_gyro_hz"], f["byte_count"], addr,
        )

    # CHUNK: validate CRC32, dedup, store. Tolerates a missing BEGIN by
    # synthesising a minimal reassembler from the chunk header's total_chunks.
    def _on_chunk(self, key, f, addr) -> None:
        if key in self.done:
            return  # mission already written — drop late chunk.
        # CRC32 over payload bytes only (zlib/IEEE) — drop on mismatch.
        if zlib.crc32(f["payload"]) != f["crc32"]:
            logger.debug(
                "[DRAIN] CRC fail s%d m%d seq=%d — dropping", *key, f["chunk_seq"]
            )
            return
        re = self.missions.get(key)
        if re is None:
            # BEGIN lost (all 3 dropped): synthesise so chunks aren't wasted.
            # ODRs unknown here → 0; t0 falls back to arrival wall time.
            synth = {
                "total_chunks": f["total_chunks"], "odr_accel_hz": 0, "odr_gyro_hz": 0,
                "byte_count": 0, "word_count": 0, "t0_tick_ms": 0,
            }
            re = MissionReassembler(f["shuttle_id"], f["mission_id"], synth)
            self.missions[key] = re
            logger.warning(
                "[DRAIN] CHUNK before BEGIN s%d m%d — synthesising reassembler (odr unknown)",
                *key,
            )
        re.add_chunk(f["chunk_seq"], f["payload"])

    # DRAIN_END: finalise immediately (sent ×3 — first one wins).
    def _on_end(self, key, f, addr) -> None:
        if key in self.done:
            return
        re = self.missions.get(key)
        if re is None:
            return  # END with no BEGIN/chunks seen — nothing to write.
        _finalise_mission(re, "END")
        # TODO ARQ phase 2: instead of finalising here, compute re.missing_ranges()
        # and reply to `addr` with a NAK (type 4) listing missing chunk_seq ranges,
        # or ACK_COMPLETE (type 5) if re.is_complete(). Only finalise after
        # ACK_COMPLETE or a bounded retransmit-round cap.
        self.done.add(key)
        self.missions.pop(key, None)

    def error_received(self, exc: Exception) -> None:
        logger.error("[DRAIN] UDP socket error: %s", exc)


# ---------------------------------------------------------------------------
# Quiet-timeout watchdog — Phase 1 blast may lose all 3 END packets.
# ---------------------------------------------------------------------------

# Finalise any mission that has seen no new chunk for DRAIN_QUIET_TIMEOUT_S.
async def _drain_watchdog(proto: DrainProtocol) -> None:
    while True:
        await asyncio.sleep(_WATCHDOG_PERIOD_S)
        now = time.monotonic()
        for key in list(proto.missions.keys()):
            re = proto.missions.get(key)
            if re is None:
                continue
            if (now - re.last_activity) >= DRAIN_QUIET_TIMEOUT_S:
                _finalise_mission(re, "quiet-timeout")
                proto.done.add(key)
                proto.missions.pop(key, None)


# ---------------------------------------------------------------------------
# Entry point — called from data-engine.py main()
# ---------------------------------------------------------------------------

# Bind the drain UDP endpoint and start the quiet-timeout watchdog task.
async def start_drain_receiver(ntp_offset_lookup, buffer_dir: str) -> None:
    configure(ntp_offset_lookup, buffer_dir)
    loop = asyncio.get_running_loop()
    proto = DrainProtocol()
    await loop.create_datagram_endpoint(
        lambda: proto,
        local_addr=("0.0.0.0", DRAIN_PORT),
    )
    logger.info("[DRAIN] high-rate capture drain listener bound on port %d", DRAIN_PORT)
    asyncio.create_task(_drain_watchdog(proto))
