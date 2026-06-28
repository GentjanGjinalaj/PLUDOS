"""
PLUDOS Mock STM32 OTA receiver (ADR-019 test/bench tier)
--------------------------------------------------------
Software stand-in for the STM32's OTA download path, used to exercise the Jetson
`client/ota_server.py` end-to-end *before* any real firmware is flashed to hardware.
It drives the full NAK selective-repeat ARQ loop against the live server:

  OTA_REQUEST → receive OTA_BEGIN + OTA_CHUNKs (per-chunk CRC32 checked) → NAK the
  missing chunk_seq ranges → repeat until the bitmap is full → verify the whole-image
  CRC32 against the manifest → OTA_ACK_COMPLETE.

Crucially it stops at the CRC gate: it stages chunks in a dict (the PSRAM analog) and
**never writes flash / swaps banks** — that step needs real hardware. So this validates
exactly the part that is safe to validate in software: transport, framing, loss
recovery, and the integrity gate. It mirrors the firmware's authority-on-the-receiver
ARQ design so a green light here means the protocol itself is sound.

Frame layout duplicated from wire_protocol.md §2b (kept self-contained, no import of
client code — same discipline as tools/mock_stm32.py).

Typical uses:
  # Point at a running data-engine / ota_server (local).
  python tools/mock_ota_stm.py

  # Induce 30% packet loss to stress the NAK loop, against a remote Jetson.
  OTA_HOST=192.168.0.100 DROP_PROB=0.30 python tools/mock_ota_stm.py

Environment variables:
  OTA_HOST        — gateway IP                                 (default: 127.0.0.1)
  OTA_PORT        — gateway OTA UDP port                       (default: 5685)
  SHUTTLE_ID      — mock shuttle id                            (default: 1)
  CURRENT_FW      — the version this mock claims to run        (default: 1)
  DROP_PROB       — simulated chunk loss probability [0..1]    (default: 0.15)
  MAX_ROUNDS      — max NAK rounds before giving up            (default: 8)
  RECV_QUIET_S    — quiet-window timeout that ends a burst     (default: 0.4)
"""

import asyncio
import logging
import os
import random
import socket
import struct
import zlib

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mock-ota-stm")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OTA_HOST     = os.getenv("OTA_HOST", "127.0.0.1")
OTA_PORT     = int(os.getenv("OTA_PORT", "5685"))
SHUTTLE_ID   = int(os.getenv("SHUTTLE_ID", "1"))
CURRENT_FW   = int(os.getenv("CURRENT_FW", "1"))
DROP_PROB    = float(os.getenv("DROP_PROB", "0.15"))
MAX_ROUNDS   = int(os.getenv("MAX_ROUNDS", "8"))
RECV_QUIET_S = float(os.getenv("RECV_QUIET_S", "0.4"))

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md §2b / client/ota_server.py exactly.
# ---------------------------------------------------------------------------

OTA_MAGIC = 0x4F44_4C50  # "PLDO"

TYPE_BEGIN = 1
TYPE_CHUNK = 2
TYPE_END = 3
TYPE_REQUEST = 4
TYPE_NAK = 5
TYPE_ACK_COMPLETE = 6

BEGIN_FMT = "<IBBIIHHI"          # magic,type,sid,fw_version,image_size,total_chunks,chunk_size,image_crc32
CHUNK_HDR_FMT = "<IBBHHHI"       # magic,type,sid,chunk_seq,total_chunks,payload_len,crc32 + payload
CHUNK_HDR_SIZE = struct.calcsize(CHUNK_HDR_FMT)
END_FMT = "<IBBHI"               # magic,type,sid,total_chunks,image_crc32
REQUEST_FMT = "<IBBI"            # magic,type,sid,current_fw_version
NAK_HDR_FMT = "<IBBH"            # magic,type,sid,n_ranges + ranges x (start,end) u16
ACK_FMT = "<IBBI"                # magic,type,sid,fw_version


# ---------------------------------------------------------------------------
# Missing-range RLE — collapse missing seqs into inclusive (start,end) ranges,
# the same compact form the firmware NAK uses to fit one datagram.
# ---------------------------------------------------------------------------

def _missing_ranges(have: set[int], total: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    seq = 0
    while seq < total:
        if seq in have:
            seq += 1
            continue
        start = seq
        while seq < total and seq not in have:
            seq += 1
        ranges.append((start, seq - 1))
    return ranges


# ---------------------------------------------------------------------------
# Receive one burst — drain frames until a quiet window, applying simulated loss.
# Returns updated manifest (or None if unchanged) after staging good chunks.
# ---------------------------------------------------------------------------

async def _recv_burst(sock, loop, staged: dict[int, bytes]):
    manifest = None
    dropped = 0
    while True:
        try:
            data = await asyncio.wait_for(loop.sock_recv(sock, 2048), RECV_QUIET_S)
        except asyncio.TimeoutError:
            break
        if len(data) < 5:
            continue
        magic, ptype = struct.unpack_from("<IB", data, 0)
        if magic != OTA_MAGIC:
            continue
        if ptype == TYPE_BEGIN:
            manifest = struct.unpack(BEGIN_FMT, data)
        elif ptype == TYPE_CHUNK:
            _, _, _, seq, total, plen, crc = struct.unpack_from(CHUNK_HDR_FMT, data, 0)
            payload = data[CHUNK_HDR_SIZE:CHUNK_HDR_SIZE + plen]
            # Reject a corrupt-on-wire chunk (per-chunk CRC32 gate) — the STM does this too.
            if len(payload) != plen or (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
                continue
            # Simulate radio loss at the receiver: pretend this chunk never landed.
            if random.random() < DROP_PROB:
                dropped += 1
                continue
            staged[seq] = payload
        # END / unknown: ignored here; completeness is decided by the bitmap.
    if dropped:
        logger.info("[mock-ota] burst: simulated-dropped %d chunks", dropped)
    return manifest


# ---------------------------------------------------------------------------
# Full client run — REQUEST → NAK loop → CRC gate → ACK. Never flashes.
# Returns a result dict for assertion by a harness / for the CLI summary.
# ---------------------------------------------------------------------------

async def run_ota_client() -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    addr = (OTA_HOST, OTA_PORT)

    logger.info("[mock-ota] shuttle %d at v%d → requesting update from %s:%d (drop=%.0f%%)",
                SHUTTLE_ID, CURRENT_FW, OTA_HOST, OTA_PORT, DROP_PROB * 100)

    # Trigger the session — server blasts BEGIN + all chunks + END if it offers a newer image.
    req = struct.pack(REQUEST_FMT, OTA_MAGIC, TYPE_REQUEST, SHUTTLE_ID, CURRENT_FW)
    sock.sendto(req, addr)

    staged: dict[int, bytes] = {}
    manifest = await _recv_burst(sock, loop, staged)
    if manifest is None:
        logger.warning("[mock-ota] no OTA_BEGIN received — server offers no newer firmware?")
        sock.close()
        return {"ok": False, "reason": "no_begin"}

    total_chunks = manifest[5]
    image_size = manifest[4]
    image_crc32 = manifest[7]
    fw_version = manifest[3]
    logger.info("[mock-ota] manifest: v%d | %d B | %d chunks | crc32=0x%08x",
                fw_version, image_size, total_chunks, image_crc32)

    # NAK loop: ask only for what the bitmap is still missing, bounded by MAX_ROUNDS.
    rounds = 0
    while len(staged) < total_chunks and rounds < MAX_ROUNDS:
        rounds += 1
        ranges = _missing_ranges(set(staged), total_chunks)
        nak = struct.pack(NAK_HDR_FMT, OTA_MAGIC, TYPE_NAK, SHUTTLE_ID, len(ranges))
        for start, end in ranges:
            nak += struct.pack("<HH", start, end)
        missing = total_chunks - len(staged)
        logger.info("[mock-ota] round %d: %d missing in %d ranges → NAK", rounds, missing, len(ranges))
        sock.sendto(nak, addr)
        await _recv_burst(sock, loop, staged)

    if len(staged) < total_chunks:
        logger.error("[mock-ota] FAILED: %d/%d chunks after %d rounds (deadline)",
                     len(staged), total_chunks, rounds)
        sock.close()
        return {"ok": False, "reason": "incomplete", "have": len(staged), "total": total_chunks}

    # Reassemble in seq order and run the whole-image CRC32 integrity gate.
    image = b"".join(staged[i] for i in range(total_chunks))
    calc = zlib.crc32(image) & 0xFFFFFFFF
    if len(image) != image_size or calc != image_crc32:
        logger.error("[mock-ota] FAILED CRC gate: size %d/%d crc 0x%08x/0x%08x — would NOT flash",
                     len(image), image_size, calc, image_crc32)
        sock.close()
        return {"ok": False, "reason": "crc_mismatch"}

    logger.info("[mock-ota] image complete + CRC verified in %d NAK round(s) — would flash now (skipped)",
                rounds)
    # Acknowledge: on real hardware this precedes the flash/bank-swap commit.
    ack = struct.pack(ACK_FMT, OTA_MAGIC, TYPE_ACK_COMPLETE, SHUTTLE_ID, fw_version)
    sock.sendto(ack, addr)
    await asyncio.sleep(0.05)
    sock.close()
    return {"ok": True, "rounds": rounds, "fw_version": fw_version,
            "image_size": image_size, "total_chunks": total_chunks}


async def main() -> None:
    result = await run_ota_client()
    if result.get("ok"):
        logger.info("[mock-ota] OK — fw v%d, %d B, %d chunks, %d NAK round(s). Flash step skipped (no hardware).",
                    result["fw_version"], result["image_size"], result["total_chunks"], result["rounds"])
    else:
        logger.error("[mock-ota] transfer failed: %s", result.get("reason"))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Mock OTA receiver aborted.")
