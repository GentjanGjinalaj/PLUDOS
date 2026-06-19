#!/usr/bin/env python3
"""Mock STM32 high-rate drain sender (ADR-020/021), for testing the gateway
drain path without flashed hardware. Replays the exact wire format that
client/drain_receiver.py parses: DrainBegin v2 (42 B) -> N DrainChunk -> DrainEnd.

The gateway therefore does REAL NIC-RX, CRC32 validation, reassembly and Parquet
writes — only the sensor payload is synthetic. Companion to mock_stm32.py (which
drives the live telemetry path on :5683); this drives the drain path on :5684.

Usage:
  DRAIN_HOST=100.119.83.35 python tools/mock_drain.py
  DRAIN_HOST=127.0.0.1 SHUTTLE_ID=9 CHUNKS=800 SPAN_S=6 python tools/mock_drain.py

Env knobs (all optional):
  DRAIN_HOST   target gateway IP            (default 127.0.0.1)
  DRAIN_PORT   drain UDP port               (default 5684)
  SHUTTLE_ID   synthetic shuttle id 0-255   (default 9 — keep out-of-fleet so the
                                             synthetic capture is easy to delete)
  MISSION_ID   firmware mission id 0-65535  (default 999)
  CHUNKS       number of chunk datagrams    (default 800 ~= 1.1 MB total)
  SPAN_S       seconds to pace the blast    (default 6.0 — mimics WiFi-paced RX)
"""
import os
import socket
import struct
import time
import zlib

# Wire constants — must match client/drain_receiver.py.
DRAIN_MAGIC = 0x52444C50  # "PLDR" little-endian
TYPE_BEGIN, TYPE_CHUNK, TYPE_END = 1, 2, 3
BEGIN_FMT = "<IBBHHHHhHBBIIIIBBHH"  # v2, 42 B
CHUNK_HDR_FMT = "<IBBHHHHI"          # 18 B header, payload follows
END_FMT = "<IBBHHHI"                 # 16 B
WORDS_PER_CHUNK = 200                # 200 * 7 B = 1400 B payload per chunk
# FIFO tag byte: the receiver demuxes on (tag >> 3) == 0x02 accel / 0x01 gyro.
TAG_ACCEL, TAG_GYRO = 0x02 << 3, 0x01 << 3
ODR_ACCEL_HZ, ODR_GYRO_HZ = 3332, 416  # MOVING-mission capture rates

HOST = os.getenv("DRAIN_HOST", "127.0.0.1")
PORT = int(os.getenv("DRAIN_PORT", "5684"))
SID = int(os.getenv("SHUTTLE_ID", "9"))
MID = int(os.getenv("MISSION_ID", "999"))
N = int(os.getenv("CHUNKS", "800"))
SPAN_S = float(os.getenv("SPAN_S", "6.0"))


# One 7-byte FIFO word: [tag, Xl, Xh, Yl, Yh, Zl, Zh] (int16 LE per axis).
def _word(tag: int, x: int, y: int, z: int) -> bytes:
    return bytes([tag]) + struct.pack("<hhh", x, y, z)


# Build one chunk payload: 8 accel : 1 gyro words, matching the 3332:416 ODR ratio.
def _payload(seq: int) -> bytes:
    out = bytearray()
    for i in range(WORDS_PER_CHUNK):
        if i % 9 == 8:
            out += _word(TAG_GYRO, 0, 0, 0)
        else:
            # ~1 g on z (16384 LSB at 0.061 mg/LSB) with a small x/y wobble.
            out += _word(TAG_ACCEL, (seq + i) % 200 - 100, i % 200 - 100, 16384)
    return bytes(out)


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    byte_count = N * WORDS_PER_CHUNK * 7
    word_count = N * WORDS_PER_CHUNK
    # DrainBegin v2: temp_c_x100=3756, pressure_hpa_x10=9906, is_idle_snapshot=0,
    # t0_tick=1000 / tx_tick=6000 -> 5 s capture age, proto=2, threshold_g2_x1000=200.
    begin = struct.pack(BEGIN_FMT, DRAIN_MAGIC, TYPE_BEGIN, SID, MID, N,
                        ODR_ACCEL_HZ, ODR_GYRO_HZ, 3756, 9906, 0, 0,
                        byte_count, word_count, 1000, 6000, 2, 0, 200, 0)
    sock.sendto(begin, (HOST, PORT))
    print(f"BEGIN -> {HOST}:{PORT} sid={SID} mid={MID} chunks={N} "
          f"bytes={byte_count} span={SPAN_S}s")

    t0 = time.time()
    per = SPAN_S / N
    for seq in range(N):
        p = _payload(seq)
        crc = zlib.crc32(p) & 0xFFFFFFFF
        hdr = struct.pack(CHUNK_HDR_FMT, DRAIN_MAGIC, TYPE_CHUNK, SID, MID,
                          seq, N, len(p), crc)
        sock.sendto(hdr + p, (HOST, PORT))
        # Pace the blast so RX spans SPAN_S — avoids overflowing the socket buffer
        # and mimics the firmware's WiFi-throttled chunk cadence.
        dt = t0 + (seq + 1) * per - time.time()
        if dt > 0:
            time.sleep(dt)
    sock.sendto(struct.pack(END_FMT, DRAIN_MAGIC, TYPE_END, SID, MID, N, 0, 0),
                (HOST, PORT))
    el = time.time() - t0
    print(f"END. {N} chunks in {el:.2f}s ({N / el:.0f} chunk/s, "
          f"{byte_count / el / 1024:.0f} KB/s)")


if __name__ == "__main__":
    main()
