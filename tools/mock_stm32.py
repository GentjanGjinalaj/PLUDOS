"""
PLUDOS Hardware Simulator (STM32 — ADR-016 v3)
----------------------------------------------
Spawns N parallel shuttles, each cycling IDLE → MOVING → long IDLE forever,
streaming 24-byte PludosTelemetry v3 packets over raw UDP to one Jetson
gateway. Matches `docs/wire_protocol.md §1` and `client/data-engine.py`.
Sensors encoded as int16 (×100 for g/dps/°C, ×10 for %RH). 0x7FFF = N/A.

Typical uses:
  # Single-shuttle smoke test against a local data-engine.
  python tools/mock_stm32.py

  # 6-shuttle stress test (one process emits all six).
  MOCK_SHUTTLES=6 python tools/mock_stm32.py

  # Point at a remote Jetson rather than localhost.
  TELEMETRY_HOST=192.168.1.50 MOCK_SHUTTLES=2 python tools/mock_stm32.py

This mock now exercises BOTH gateway receive paths, like real firmware:
  * :5683 live telemetry — the 24-byte hot loop (IDLE 0.1 Hz / MOVING 50 Hz).
  * :5684 high-rate drain — after each MOVING run the shuttle "drains" the raw
    ISM330DHCX FIFO it buffered in PSRAM: DRAIN_BEGIN (×3) → CRC32 CHUNKs →
    DRAIN_END (×3), matching `docs/wire_protocol.md §2` and `drain_receiver.py`.
    A small idle snapshot (12.5 Hz, temp/pressure stamped) is drained alongside.
There is NO OTA / live-firmware-update path here — only the data pipeline.

Environment variables:
  TELEMETRY_HOST    — gateway IP                            (default: 127.0.0.1)
  TELEMETRY_PORT    — gateway live-telemetry UDP port       (default: 5683)
  MOCK_SHUTTLES     — number of parallel shuttles           (default: 1)
  FIRST_SHUTTLE_ID  — starting shuttle ID (1-based)         (default: 1)
  MISSION_S         — MOVING phase duration in seconds      (default: 30)
  IDLE_S            — short IDLE phase before each MOVING   (default: 5)
  POST_MISSION_IDLE_S — long IDLE after MOVING (>= 30 triggers
                      gateway mission-end flush)            (default: 35)
  DRAIN_HOST        — drain target IP (default: TELEMETRY_HOST)
  DRAIN_PORT        — gateway drain UDP port                (default: 5684)
  DRAIN_ENABLE      — emit the high-rate drain after MOVING (default: 1)
  DRAIN_CAPTURE_S   — mission FIFO span to drain, seconds   (default: MISSION_S)
  IDLE_SNAPSHOT_ENABLE — also drain a 12.5 Hz idle snapshot (default: 1)
  IDLE_SNAP_S       — idle-snapshot capture length, seconds (default: 4)
  CHUNK_PAYLOAD_BYTES — drain chunk payload size            (default: 1024)
  DRAIN_CHUNK_GAP_MS — inter-chunk pacing in ms (0 = burst) (default: 0.2)
"""

import asyncio
import logging
import math
import os
import random
import socket
import struct
import time
import zlib

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mock-stm32")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TELEMETRY_HOST      = os.getenv("TELEMETRY_HOST", "127.0.0.1")
TELEMETRY_PORT      = int(os.getenv("TELEMETRY_PORT",      "5683"))
MOCK_SHUTTLES       = int(os.getenv("MOCK_SHUTTLES",       "1"))
FIRST_SHUTTLE_ID    = int(os.getenv("FIRST_SHUTTLE_ID",    "1"))
MISSION_S           = float(os.getenv("MISSION_S",         "30"))
IDLE_S              = float(os.getenv("IDLE_S",            "5"))
POST_MISSION_IDLE_S = float(os.getenv("POST_MISSION_IDLE_S", "35"))

# Wire format v3 — must match wire_protocol.md §1 and data-engine.py exactly.
TELEMETRY_FMT  = "<BHIBhhhhhhhh"
TELEMETRY_SIZE = struct.calcsize(TELEMETRY_FMT)
assert TELEMETRY_SIZE == 24, f"PludosTelemetry must be 24 B (got {TELEMETRY_SIZE})"

# Scale factors matching firmware encoding.
_SCALE_G   = 100   # g    → int16
_SCALE_DPS = 100   # dps  → int16
_SCALE_C   = 100   # °C   → int16
_SCALE_RH  = 10    # %RH  → int16
_SENTINEL  = 0x7FFF

STATE_IDLE   = 0
STATE_MOVING = 1

# TX cadence mirrors STM32 firmware: 50 Hz MOVING, 0.1 Hz IDLE.
TX_PERIOD_IDLE_S   = 10.0   # 0.1 Hz in IDLE   (TX_PERIOD_IDLE_MS=10000)
TX_PERIOD_MOVING_S = 0.02   # 50 Hz in MOVING  (SAMPLE_PERIOD_MOVING_MS=20)


# ---------------------------------------------------------------------------
# High-rate drain (:5684) — second receive path, mirrors PSRAM FIFO drain.
# Wire format copied verbatim from client/drain_receiver.py (wire_protocol.md §2).
# ---------------------------------------------------------------------------

DRAIN_HOST           = os.getenv("DRAIN_HOST", TELEMETRY_HOST)
DRAIN_PORT           = int(os.getenv("DRAIN_PORT",            "5684"))
DRAIN_ENABLE         = os.getenv("DRAIN_ENABLE",         "1") == "1"
DRAIN_CAPTURE_S      = float(os.getenv("DRAIN_CAPTURE_S", str(MISSION_S)))
IDLE_SNAPSHOT_ENABLE = os.getenv("IDLE_SNAPSHOT_ENABLE", "1") == "1"
IDLE_SNAP_S          = float(os.getenv("IDLE_SNAP_S",        "4"))
CHUNK_PAYLOAD_BYTES  = int(os.getenv("CHUNK_PAYLOAD_BYTES",  "1024"))
DRAIN_CHUNK_GAP_MS   = float(os.getenv("DRAIN_CHUNK_GAP_MS", "0.2"))

# 0x52444C50 = ASCII "PLDR" little-endian. Proto v2 = current 42-byte DrainBegin.
DRAIN_MAGIC         = 0x52444C50
DRAIN_PROTO_VERSION = 2
D_TYPE_BEGIN, D_TYPE_CHUNK, D_TYPE_END, D_TYPE_ACK = 1, 2, 3, 6

# DrainBegin v2 (42 B): magic,type,sid,mid,total_chunks,odr_a,odr_g,temp_x100(int16),
# pressure_x10,is_idle,_pad,byte_count,word_count,t0_tick,tx_tick | proto,skipped,thr_x1000,jitter.
BEGIN_FMT     = "<IBBHHHHhHBBIIII" + "BBHH"
CHUNK_HDR_FMT = "<IBBHHHHI"   # magic,type,sid,mid,chunk_seq,total_chunks,payload_len,crc32
END_FMT       = "<IBBHHHI"    # magic,type,sid,mid,total_chunks,_pad,crc32_all
ACK_FMT       = "<IBBH"       # magic,type,sid,mid  (echoed back by the gateway)

# FIFO word: 7 B = [tag, Xl,Xh, Yl,Yh, Zl,Zh]. Receiver reads tag as byte>>3.
FIFO_WORD_FMT  = "<Bhhh"
ACCEL_TAG_BYTE = 0x02 << 3   # tag>>3 == 0x02 (accelerometer)
GYRO_TAG_BYTE  = 0x01 << 3   # tag>>3 == 0x01 (gyroscope)

# Capture-mode ODRs (firmware main.c) and ISM330 ±2g/±250dps raw-LSB scaling.
ODR_ACCEL_MOVING = 3332
ODR_GYRO_MOVING  = 416
ODR_IDLE_INT     = 12        # wire int; receiver overrides to 12.5 for idle snapshots
ACCEL_G_PER_LSB  = 0.000061  # 0.061 mg/LSB
GYRO_DPS_PER_LSB = 0.00875   # 8.75 mdps/LSB
TEMP_INVALID_X100 = 0x7FFF   # absent env stamp (MOVING missions)

# MOVING-label boundary stamped into the BEGIN provenance tail (g² × 1000).
MOVEMENT_THRESHOLD_G2_X1000 = int(os.getenv("MOVEMENT_THRESHOLD_G2_X1000", "300"))


# ---------------------------------------------------------------------------
# Packet builder
# ---------------------------------------------------------------------------

def _to_i16(val: float, scale: int) -> int:
    # Clamp to int16 range; return sentinel on NaN or overflow.
    raw = round(val * scale)
    return raw if -32767 <= raw <= 32766 else _SENTINEL

def _pack(shuttle_id: int, seq: int, tick_ms: int, state: int,
          ax: float, ay: float, az: float,
          gx: float, gy: float, gz: float,
          temp: float, hum: float) -> bytes:
    # 24-byte little-endian PludosTelemetry v3 matching `<BHIBhhhhhhhh`.
    return struct.pack(
        TELEMETRY_FMT,
        shuttle_id & 0xFF,
        seq & 0xFFFF,
        tick_ms & 0xFFFFFFFF,
        state & 0xFF,
        _to_i16(ax, _SCALE_G),   _to_i16(ay, _SCALE_G),   _to_i16(az, _SCALE_G),
        _to_i16(gx, _SCALE_DPS), _to_i16(gy, _SCALE_DPS), _to_i16(gz, _SCALE_DPS),
        _to_i16(temp, _SCALE_C), _to_i16(hum, _SCALE_RH),
    )


# ---------------------------------------------------------------------------
# Drain (:5684) — raw FIFO capture builder + blast sender
# ---------------------------------------------------------------------------

def _clamp_i16(raw: int) -> int:
    # ISM330 raw axis is a signed 16-bit value; clamp so synthetic peaks never wrap.
    return max(-32768, min(32767, raw))

def _accel_raw(g: float) -> int:
    # Physical g → raw LSB at ±2 g full-scale (inverse of ACCEL_G_PER_LSB).
    return _clamp_i16(round(g / ACCEL_G_PER_LSB))

def _gyro_raw(dps: float) -> int:
    # Physical dps → raw LSB at ±250 dps full-scale (inverse of GYRO_DPS_PER_LSB).
    return _clamp_i16(round(dps / GYRO_DPS_PER_LSB))

def _accel_word(i: int, odr: int, moving: bool) -> bytes:
    # One synthetic accel FIFO word: gravity on Z plus (in MOVING) a vibration tone.
    t = i / odr
    if moving:
        vib = 0.15 * math.sin(2 * math.pi * 35.0 * t)   # ~35 Hz bearing-like tone
        ax = vib + random.gauss(0.0, 0.03)
        ay = 0.6 * vib + random.gauss(0.0, 0.03)
        az = 1.0 + 0.10 * math.sin(2 * math.pi * 12.0 * t) + random.gauss(0.0, 0.03)
    else:
        ax = random.gauss(0.0, 0.004)
        ay = random.gauss(0.0, 0.004)
        az = 1.0 + random.gauss(0.0, 0.004)
    return struct.pack(FIFO_WORD_FMT, ACCEL_TAG_BYTE,
                       _accel_raw(ax), _accel_raw(ay), _accel_raw(az))

def _gyro_word(i: int, odr: int, moving: bool) -> bytes:
    # One synthetic gyro FIFO word: small rotation rates (larger while MOVING).
    t = i / odr
    if moving:
        gx = 8.0 * math.sin(2 * math.pi * 5.0 * t) + random.gauss(0.0, 1.0)
        gy = random.gauss(0.0, 2.0)
        gz = random.gauss(0.0, 2.0)
    else:
        gx = random.gauss(0.0, 0.15)
        gy = random.gauss(0.0, 0.15)
        gz = random.gauss(0.0, 0.15)
    return struct.pack(FIFO_WORD_FMT, GYRO_TAG_BYTE,
                       _gyro_raw(gx), _gyro_raw(gy), _gyro_raw(gz))

def _build_capture(is_idle: bool, duration_s: float) -> tuple[bytes, int, int, int, int]:
    # Build an interleaved accel/gyro FIFO byte stream for `duration_s`.
    # MOVING captures accel 3332 Hz : gyro 416 Hz (~8:1); idle snapshots run both
    # at 12.5 Hz. Returns (stream, n_accel, n_gyro, odr_accel, odr_gyro).
    if is_idle:
        odr_a = odr_g = ODR_IDLE_INT
        n_acc = n_gyr = max(2, int(duration_s * 12.5))
    else:
        odr_a, odr_g = ODR_ACCEL_MOVING, ODR_GYRO_MOVING
        n_acc = int(duration_s * odr_a)
        n_gyr = int(duration_s * odr_g)

    # Emit one gyro word per `ratio` accel words so the FIFO is time-ordered the way
    # the sensor's combined FIFO would be; the receiver demuxes by tag regardless.
    ratio = max(1, round(odr_a / odr_g)) if odr_g else 1
    stream = bytearray()
    gi = 0
    for ai in range(n_acc):
        stream += _accel_word(ai, odr_a, not is_idle)
        if gi < n_gyr and (ai % ratio) == (ratio - 1):
            stream += _gyro_word(gi, odr_g, not is_idle)
            gi += 1
    # Flush any remaining gyro words (when n_gyr*ratio > n_acc).
    while gi < n_gyr:
        stream += _gyro_word(gi, odr_g, not is_idle)
        gi += 1
    return bytes(stream), n_acc, n_gyr, odr_a, odr_g


async def _send_drain(sock: socket.socket, shuttle_id: int, mission_id: int,
                      is_idle: bool, tick_now_ms: int,
                      temp_c: float | None, pressure_hpa: float | None) -> None:
    # Blast one PSRAM capture as a real drain: BEGIN×3 → CRC32 CHUNKs → END×3.
    # `sock` is the shuttle's non-blocking drain socket (also drains BEGIN-acks).
    duration_s = IDLE_SNAP_S if is_idle else DRAIN_CAPTURE_S
    stream, n_acc, n_gyr, odr_a, odr_g = _build_capture(is_idle, duration_s)
    byte_count = len(stream)
    word_count = n_acc + n_gyr
    total_chunks = max(1, (byte_count + CHUNK_PAYLOAD_BYTES - 1) // CHUNK_PAYLOAD_BYTES)

    # Capture age (tx_tick - t0_tick) is what the gateway subtracts from BEGIN
    # arrival to recover wall-clock — so t0 sits one capture-length before "now".
    duration_ms = int(duration_s * 1000)
    t0_tick = max(0, tick_now_ms - duration_ms)
    tx_tick = tick_now_ms

    temp_x100 = int(round(temp_c * 100)) if (is_idle and temp_c is not None) else TEMP_INVALID_X100
    press_x10 = int(round(pressure_hpa * 10)) if (is_idle and pressure_hpa is not None) else 0
    jitter_ms = random.randint(5, 50)

    # Pre-drain anti-collision wait (firmware jitters 1–15 s; scaled down for the mock),
    # stamped into the BEGIN so the gateway records the same provenance a real node sends.
    await asyncio.sleep(jitter_ms / 1000.0)

    begin = struct.pack(
        BEGIN_FMT, DRAIN_MAGIC, D_TYPE_BEGIN, shuttle_id & 0xFF, mission_id & 0xFFFF,
        total_chunks, odr_a, odr_g, temp_x100, press_x10,
        1 if is_idle else 0, 0, byte_count, word_count, t0_tick, tx_tick,
        DRAIN_PROTO_VERSION, 0, MOVEMENT_THRESHOLD_G2_X1000, jitter_ms,
    )
    for _ in range(3):
        sock.sendto(begin, (DRAIN_HOST, DRAIN_PORT))
    # Best-effort: drain any 8-byte BEGIN-ack the gateway echoed (liveness evidence).
    try:
        while True:
            sock.recv(64)
    except (BlockingIOError, OSError):
        pass

    for seq in range(total_chunks):
        payload = stream[seq * CHUNK_PAYLOAD_BYTES:(seq + 1) * CHUNK_PAYLOAD_BYTES]
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        hdr = struct.pack(CHUNK_HDR_FMT, DRAIN_MAGIC, D_TYPE_CHUNK,
                          shuttle_id & 0xFF, mission_id & 0xFFFF,
                          seq, total_chunks, len(payload), crc)
        sock.sendto(hdr + payload, (DRAIN_HOST, DRAIN_PORT))
        # Pace the blast so a localhost kernel buffer doesn't shed the whole drain.
        await asyncio.sleep(DRAIN_CHUNK_GAP_MS / 1000.0 if DRAIN_CHUNK_GAP_MS > 0 else 0)

    crc_all = zlib.crc32(stream) & 0xFFFFFFFF
    end = struct.pack(END_FMT, DRAIN_MAGIC, D_TYPE_END, shuttle_id & 0xFF,
                      mission_id & 0xFFFF, total_chunks, 0, crc_all)
    for _ in range(3):
        sock.sendto(end, (DRAIN_HOST, DRAIN_PORT))

    logger.info(
        "[shuttle-%d] drain %s mid=%d | %d chunks / %d KB | accel=%d gyro=%d @ %d/%d Hz",
        shuttle_id, "idle-snapshot" if is_idle else "mission", mission_id,
        total_chunks, byte_count // 1024, n_acc, n_gyr, odr_a, odr_g,
    )


# ---------------------------------------------------------------------------
# Per-shuttle simulation coroutine
# ---------------------------------------------------------------------------

async def _send_phase(sock: socket.socket, shuttle_id: int, state: int,
                      duration_s: float, seq_ref: list[int], boot_ms: int,
                      ax_range: tuple[float, float],
                      az_dc: float, az_jitter: float) -> None:
    """Emit packets at the state-appropriate cadence for `duration_s`.

    `seq_ref` is a 1-element mutable container so the per-shuttle sequence
    counter survives across phase calls (uint16 wrap by design).
    """
    period = TX_PERIOD_MOVING_S if state == STATE_MOVING else TX_PERIOD_IDLE_S
    end_t  = time.monotonic() + duration_s

    while time.monotonic() < end_t:
        seq_ref[0] = (seq_ref[0] + 1) & 0xFFFF
        tick_ms = int(time.monotonic() * 1000) - boot_ms

        ax = round(random.uniform(*ax_range), 2)
        ay = round(random.uniform(*ax_range), 2)
        az = round(az_dc + random.uniform(-az_jitter, az_jitter), 2)
        # Simulated gyro: small random rotation rates during MOVING, near-zero at IDLE.
        g_scale = 5.0 if state == STATE_MOVING else 0.5
        gx = round(random.uniform(-g_scale, g_scale), 2)
        gy = round(random.uniform(-g_scale, g_scale), 2)
        gz = round(random.uniform(-g_scale, g_scale), 2)
        temp = round(random.uniform(20.0, 25.0), 1)
        hum  = round(random.uniform(40.0, 60.0), 1)

        sock.sendto(
            _pack(shuttle_id, seq_ref[0], tick_ms, state, ax, ay, az, gx, gy, gz, temp, hum),
            (TELEMETRY_HOST, TELEMETRY_PORT),
        )
        await asyncio.sleep(period)


async def simulate_shuttle(shuttle_id: int) -> None:
    # One shuttle loops: short IDLE → MOVING (mission) → drain → long IDLE (flush trigger).
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Separate non-blocking socket for the :5684 drain blast + BEGIN-ack reads.
    drain_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    drain_sock.setblocking(False)
    seq_ref = [0]
    boot_ms = int(time.monotonic() * 1000)
    # Firmware mission_id restarts at 0 each boot and increments per capture; one
    # value per drained capture (mission + idle snapshot each consume one).
    mission_id = 0

    logger.info("[shuttle-%d] starting → telemetry %s:%d | drain %s:%d",
                shuttle_id, TELEMETRY_HOST, TELEMETRY_PORT, DRAIN_HOST, DRAIN_PORT)

    try:
        cycle = 0
        while True:
            cycle += 1
            logger.info("[shuttle-%d] cycle %d: pre-IDLE %.0fs", shuttle_id, cycle, IDLE_S)
            await _send_phase(sock, shuttle_id, STATE_IDLE, IDLE_S,
                              seq_ref, boot_ms,
                              ax_range=(-0.02, 0.02), az_dc=1.0, az_jitter=0.02)

            logger.info("[shuttle-%d] cycle %d: MOVING %.0fs (50 Hz)", shuttle_id, cycle, MISSION_S)
            await _send_phase(sock, shuttle_id, STATE_MOVING, MISSION_S,
                              seq_ref, boot_ms,
                              ax_range=(-0.20, 0.20), az_dc=1.0, az_jitter=0.15)

            # MOVING→IDLE boundary: radio was off during motion, so the PSRAM FIFO is
            # drained now. Mission capture first, then the piggybacked idle snapshot.
            if DRAIN_ENABLE:
                tick_now = int(time.monotonic() * 1000) - boot_ms
                mission_id += 1
                await _send_drain(drain_sock, shuttle_id, mission_id, False, tick_now, None, None)
                if IDLE_SNAPSHOT_ENABLE:
                    mission_id += 1
                    await _send_drain(drain_sock, shuttle_id, mission_id, True, tick_now,
                                      temp_c=round(random.uniform(20.0, 25.0), 2),
                                      pressure_hpa=round(random.uniform(1008.0, 1018.0), 1))

            logger.info("[shuttle-%d] cycle %d: post-IDLE %.0fs (triggers mission flush)",
                        shuttle_id, cycle, POST_MISSION_IDLE_S)
            await _send_phase(sock, shuttle_id, STATE_IDLE, POST_MISSION_IDLE_S,
                              seq_ref, boot_ms,
                              ax_range=(-0.01, 0.01), az_dc=1.0, az_jitter=0.01)
    finally:
        sock.close()
        drain_sock.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info(
        "Mock STM32 (ADR-016 v3) | telemetry=%s:%d drain=%s:%d | %d shuttle(s) from ID %d "
        "| mission=%.0fs idle=%.0fs post-idle=%.0fs | 50Hz MOVING 0.1Hz IDLE | drain=%s",
        TELEMETRY_HOST, TELEMETRY_PORT, DRAIN_HOST, DRAIN_PORT,
        MOCK_SHUTTLES, FIRST_SHUTTLE_ID, MISSION_S, IDLE_S, POST_MISSION_IDLE_S,
        "on" if DRAIN_ENABLE else "off",
    )

    # Stagger shuttle starts by a fraction of a second so their IDLE/MOVING
    # boundaries are not perfectly phase-locked — closer to real-world spread.
    tasks = []
    for i in range(MOCK_SHUTTLES):
        sid = FIRST_SHUTTLE_ID + i
        tasks.append(asyncio.create_task(simulate_shuttle(sid)))
        await asyncio.sleep(0.25)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Simulation aborted.")
