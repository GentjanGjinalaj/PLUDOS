"""
PLUDOS Edge Gateway: OTA firmware-update server (ADR-019, test/bench tier)
--------------------------------------------------------------------------
Third UDP path on port 5685: serves a new STM32U585 firmware image to shuttles.
This is the *reverse* of the high-rate capture drain (`drain_receiver.py`, :5684):
there the STM sends and the Jetson reassembles; here the Jetson sends and the STM
reassembles into its inactive flash bank. The frame layout, magic+type dispatch,
little-endian structs, and zlib CRC32 all mirror the drain so the two paths share
one mental model.

Reliability = NAK selective-repeat ARQ (the receiver is the authority on what it
has, so the STM drives retransmission — this server is near-stateless):

  1. STM sends OTA_REQUEST(current_fw_version).
  2. If a newer image is loaded, the server replies OTA_BEGIN (manifest:
     fw_version, image_size, total_chunks, chunk_size, whole-image CRC32), then
     blasts every OTA_CHUNK back-to-back (lightly paced for the EMW3080 RX), then
     OTA_END (x3).
  3. The STM stages chunks in PSRAM, tracks a received-bitmap, and after END (or a
     quiet timeout) sends OTA_NAK with the missing chunk_seq ranges.
  4. The server resends only those chunks + OTA_END. Repeat until the STM has the
     whole image, verifies the whole-image CRC32, flashes the inactive bank, and
     sends OTA_ACK_COMPLETE.

Security (signing/encryption/auth) is OUT of scope for this tier — trusted bench
LAN only. The OTA_BEGIN manifest reserves no signature field yet; the production
path (ADR-019) is ST SBSFU / MCUboot. See docs/wire_protocol.md and ADR-019.

Firmware source: OTA_FIRMWARE_DIR holds `firmware.bin` + `manifest.json`
({"fw_version": <int>}). Dropping a new pair (or bumping fw_version) is picked up
on the next request via an mtime check — no container restart needed.
"""

import asyncio
import json
import logging
import os
import struct
import zlib

logger = logging.getLogger("data-engine")

# ---------------------------------------------------------------------------
# Configuration — env overrides with safe defaults.
# ---------------------------------------------------------------------------

# Dedicated OTA port (distinct from telemetry :5683 and drain :5684).
OTA_PORT = int(os.getenv("OTA_PORT", "5685"))

# Where firmware.bin + manifest.json live (a bind-mount on the Jetson host).
OTA_FIRMWARE_DIR = os.getenv("OTA_FIRMWARE_DIR", "/app/firmware")

# Per-chunk payload bytes. 1400 keeps the datagram (16 B header + payload = 1416 B)
# under the 1472 B non-fragmenting UDP limit (sampling_strategy.md §1), matching the
# drain chunk sizing so word/page alignment reasoning carries over.
OTA_CHUNK_SIZE = int(os.getenv("OTA_CHUNK_SIZE", "1400"))

# Inter-chunk pacing during a blast. Unlike the drain (STM→Jetson, 4 MB kernel RX
# buffer), here the EMW3080's small RX buffer is the bottleneck, so a tiny gap between
# sends curbs receiver overrun. The NAK loop recovers whatever still drops.
OTA_CHUNK_PACING_MS = float(os.getenv("OTA_CHUNK_PACING_MS", "2.0"))

# Times each control frame (BEGIN / END) is repeated, mirroring the drain's x3
# robustness against losing a lone control packet.
OTA_CONTROL_REPEAT = int(os.getenv("OTA_CONTROL_REPEAT", "3"))

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md (OTA section) exactly. All little-endian.
# ---------------------------------------------------------------------------

# 0x4F44_4C50 = ASCII "PLDO" in memory order P,L,D,O (little-endian u32).
# Distinct from the drain's "PLDR" (0x52444C50) so the two paths never alias.
OTA_MAGIC = 0x4F44_4C50

# Packet type bytes. 1-3 Jetson→STM (server-sent); 4-6 STM→Jetson (received here).
TYPE_BEGIN = 1         # manifest (server → STM)
TYPE_CHUNK = 2         # data     (server → STM)
TYPE_END = 3           # end marker, sent x3 (server → STM)
TYPE_REQUEST = 4       # start/resume a session (STM → server)
TYPE_NAK = 5           # missing chunk_seq ranges (STM → server)
TYPE_ACK_COMPLETE = 6  # image whole + CRC-verified (STM → server)

# OTA_BEGIN: magic(4) type(1) sid(1) fw_version(4) image_size(4) total_chunks(2)
#            chunk_size(2) image_crc32(4) = 22 B.
BEGIN_FMT = "<IBBIIHHI"
BEGIN_SIZE = struct.calcsize(BEGIN_FMT)
assert BEGIN_SIZE == 22, f"OTA_BEGIN must be 22 bytes, got {BEGIN_SIZE}"

# OTA_CHUNK header: magic(4) type(1) sid(1) chunk_seq(2) total_chunks(2)
#                   payload_len(2) crc32(4) = 16 B, then payload bytes.
CHUNK_HDR_FMT = "<IBBHHHI"
CHUNK_HDR_SIZE = struct.calcsize(CHUNK_HDR_FMT)
assert CHUNK_HDR_SIZE == 16, f"OTA_CHUNK header must be 16 bytes, got {CHUNK_HDR_SIZE}"

# OTA_END: magic(4) type(1) sid(1) total_chunks(2) image_crc32(4) = 12 B.
END_FMT = "<IBBHI"
END_SIZE = struct.calcsize(END_FMT)
assert END_SIZE == 12, f"OTA_END must be 12 bytes, got {END_SIZE}"

# OTA_REQUEST: magic(4) type(1) sid(1) current_fw_version(4) = 10 B.
REQUEST_FMT = "<IBBI"
REQUEST_SIZE = struct.calcsize(REQUEST_FMT)
assert REQUEST_SIZE == 10, f"OTA_REQUEST must be 10 bytes, got {REQUEST_SIZE}"

# OTA_NAK header: magic(4) type(1) sid(1) n_ranges(2) = 8 B, then n_ranges x (start,end) u16 pairs.
NAK_HDR_FMT = "<IBBH"
NAK_HDR_SIZE = struct.calcsize(NAK_HDR_FMT)
assert NAK_HDR_SIZE == 8, f"OTA_NAK header must be 8 bytes, got {NAK_HDR_SIZE}"

# OTA_ACK_COMPLETE: magic(4) type(1) sid(1) fw_version(4) = 10 B.
ACK_FMT = "<IBBI"
ACK_SIZE = struct.calcsize(ACK_FMT)
assert ACK_SIZE == 10, f"OTA_ACK_COMPLETE must be 10 bytes, got {ACK_SIZE}"


# ---------------------------------------------------------------------------
# Firmware image — loaded from disk, pre-chunked, CRCs cached.
# ---------------------------------------------------------------------------

class FirmwareImage:
    """An offered firmware: the raw .bin pre-split into chunks with cached CRC32s.

    Recomputed only when firmware.bin's mtime changes, so a freshly dropped image
    is served without a container restart and steady-state requests are cheap."""

    def __init__(self, fw_version: int, data: bytes, chunk_size: int) -> None:
        self.fw_version = fw_version
        self.image_size = len(data)
        self.chunk_size = chunk_size
        self.image_crc32 = zlib.crc32(data) & 0xFFFFFFFF
        # Pre-split into payloads and cache each chunk's CRC32 (the STM checks both
        # per-chunk and the whole-image CRC; precomputing keeps the blast cheap).
        self.payloads: list[bytes] = [
            data[i:i + chunk_size] for i in range(0, len(data), chunk_size)
        ] or [b""]
        self.crcs: list[int] = [zlib.crc32(p) & 0xFFFFFFFF for p in self.payloads]
        self.total_chunks = len(self.payloads)


# ---------------------------------------------------------------------------
# Loader — reads firmware.bin + manifest.json, caches by mtime.
# ---------------------------------------------------------------------------

class FirmwareStore:
    """Owns the on-disk firmware and reloads it lazily when firmware.bin changes."""

    def __init__(self, firmware_dir: str) -> None:
        self.firmware_dir = firmware_dir
        self.bin_path = os.path.join(firmware_dir, "firmware.bin")
        self.manifest_path = os.path.join(firmware_dir, "manifest.json")
        self._image: FirmwareImage | None = None
        self._loaded_mtime: float | None = None

    # Return the current image, reloading if firmware.bin changed or none is cached.
    # None means no valid firmware is on disk (OTA simply offers nothing).
    def get(self) -> FirmwareImage | None:
        try:
            mtime = os.path.getmtime(self.bin_path)
        except OSError:
            self._image = None
            self._loaded_mtime = None
            return None
        if self._image is not None and mtime == self._loaded_mtime:
            return self._image
        self._image = self._load()
        self._loaded_mtime = mtime if self._image is not None else None
        return self._image

    # Read + validate firmware.bin and manifest.json into a FirmwareImage, or None.
    def _load(self) -> FirmwareImage | None:
        try:
            with open(self.bin_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            logger.warning("[OTA] cannot read %s: %s", self.bin_path, exc)
            return None
        if not data:
            logger.warning("[OTA] %s is empty — offering no firmware", self.bin_path)
            return None
        try:
            with open(self.manifest_path) as fh:
                manifest = json.load(fh)
            fw_version = int(manifest["fw_version"])
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("[OTA] cannot read fw_version from %s: %s", self.manifest_path, exc)
            return None
        img = FirmwareImage(fw_version, data, OTA_CHUNK_SIZE)
        logger.info(
            "[OTA] loaded firmware v%d | %d B | %d chunks (%d B) | crc32=0x%08x",
            img.fw_version, img.image_size, img.total_chunks, img.chunk_size, img.image_crc32,
        )
        return img

    # fw_version currently offered (for the beacon `:fw=` token), or None.
    def offered_version(self) -> int | None:
        img = self.get()
        return img.fw_version if img is not None else None


# ---------------------------------------------------------------------------
# Frame builders — server-sent frames (BEGIN / CHUNK / END).
# ---------------------------------------------------------------------------

# Pack the OTA_BEGIN manifest for one shuttle.
def _build_begin(sid: int, img: FirmwareImage) -> bytes:
    return struct.pack(
        BEGIN_FMT, OTA_MAGIC, TYPE_BEGIN, sid, img.fw_version, img.image_size,
        img.total_chunks, img.chunk_size, img.image_crc32,
    )


# Pack one OTA_CHUNK (header + payload) for chunk_seq.
def _build_chunk(sid: int, img: FirmwareImage, seq: int) -> bytes:
    payload = img.payloads[seq]
    hdr = struct.pack(
        CHUNK_HDR_FMT, OTA_MAGIC, TYPE_CHUNK, sid, seq, img.total_chunks,
        len(payload), img.crcs[seq],
    )
    return hdr + payload


# Pack the OTA_END marker.
def _build_end(sid: int, img: FirmwareImage) -> bytes:
    return struct.pack(END_FMT, OTA_MAGIC, TYPE_END, sid, img.total_chunks, img.image_crc32)


# ---------------------------------------------------------------------------
# Inbound parsing — defensive: skip anything malformed (mirrors drain _parse_packet).
# ---------------------------------------------------------------------------

# Validate magic + type, return (type, fields) or None for bad/unknown packets.
def _parse_packet(data: bytes):
    if len(data) < 5:
        return None
    magic, ptype = struct.unpack_from("<IB", data, 0)
    if magic != OTA_MAGIC:
        return None

    if ptype == TYPE_REQUEST:
        if len(data) < REQUEST_SIZE:
            return None
        _, _, sid, cur_ver = struct.unpack_from(REQUEST_FMT, data, 0)
        return (TYPE_REQUEST, {"shuttle_id": sid, "current_fw_version": cur_ver})

    if ptype == TYPE_NAK:
        if len(data) < NAK_HDR_SIZE:
            return None
        _, _, sid, n_ranges = struct.unpack_from(NAK_HDR_FMT, data, 0)
        # Each range is two u16 (start, end inclusive). Reject a truncated range list.
        if len(data) < NAK_HDR_SIZE + n_ranges * 4:
            return None
        ranges: list[tuple[int, int]] = []
        for i in range(n_ranges):
            start, end = struct.unpack_from("<HH", data, NAK_HDR_SIZE + i * 4)
            ranges.append((start, end))
        return (TYPE_NAK, {"shuttle_id": sid, "ranges": ranges})

    if ptype == TYPE_ACK_COMPLETE:
        if len(data) < ACK_SIZE:
            return None
        _, _, sid, fw_ver = struct.unpack_from(ACK_FMT, data, 0)
        return (TYPE_ACK_COMPLETE, {"shuttle_id": sid, "fw_version": fw_ver})

    # Unknown / server-sent type echoed back — ignore.
    return None


# ---------------------------------------------------------------------------
# UDP protocol — OTA control + chunks on port 5685.
# ---------------------------------------------------------------------------

class OtaProtocol(asyncio.DatagramProtocol):
    """Asyncio datagram handler for the OTA firmware server on port 5685.

    Near-stateless: the STM drives the transfer (REQUEST → NAK loop → ACK_COMPLETE);
    this side just blasts the offered image and resends whatever seqs are NAKed."""

    def __init__(self, store: FirmwareStore) -> None:
        self.store = store
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        parsed = _parse_packet(data)
        if parsed is None:
            logger.debug("[OTA] dropping unparseable %d-byte packet from %s", len(data), addr)
            return
        ptype, f = parsed
        if ptype == TYPE_REQUEST:
            self._on_request(f, addr)
        elif ptype == TYPE_NAK:
            self._on_nak(f, addr)
        elif ptype == TYPE_ACK_COMPLETE:
            self._on_ack_complete(f, addr)

    # OTA_REQUEST: blast the full image if a newer one is offered, else stay silent.
    def _on_request(self, f, addr) -> None:
        sid, cur = f["shuttle_id"], f["current_fw_version"]
        img = self.store.get()
        if img is None:
            logger.debug("[OTA] REQUEST from shuttle %d but no firmware offered", sid)
            return
        if img.fw_version <= cur:
            logger.debug("[OTA] shuttle %d already at v%d (offered v%d) — no update",
                         sid, cur, img.fw_version)
            return
        logger.info("[OTA] shuttle %d at v%d → serving v%d (%d chunks) to %s",
                    sid, cur, img.fw_version, img.total_chunks, addr)
        # Full transfer = every chunk_seq; reuse the same paced sender as a NAK resend.
        seqs = list(range(img.total_chunks))
        asyncio.create_task(self._serve(sid, img, seqs, addr, begin=True))

    # OTA_NAK: resend only the chunk_seqs the STM is still missing, then END again.
    def _on_nak(self, f, addr) -> None:
        sid = f["shuttle_id"]
        img = self.store.get()
        if img is None:
            return
        seqs: list[int] = []
        for start, end in f["ranges"]:
            # Clamp to valid range so a malformed NAK can't index out of bounds.
            for s in range(max(0, start), min(end, img.total_chunks - 1) + 1):
                seqs.append(s)
        if not seqs:
            return
        logger.info("[OTA] shuttle %d NAK → resending %d chunks", sid, len(seqs))
        asyncio.create_task(self._serve(sid, img, seqs, addr, begin=False))

    # OTA_ACK_COMPLETE: the STM has the whole image and is committing to flash.
    def _on_ack_complete(self, f, addr) -> None:
        logger.info("[OTA] shuttle %d ACK_COMPLETE v%d — image received intact, committing",
                    f["shuttle_id"], f["fw_version"])

    # Send (optional BEGIN) + the listed chunks (paced) + END (x3). Runs as a task so
    # the pacing sleeps never block the event loop / the socket's other readers.
    async def _serve(self, sid: int, img: FirmwareImage, seqs, addr, begin: bool) -> None:
        if self.transport is None:
            return
        pacing = OTA_CHUNK_PACING_MS / 1000.0
        try:
            if begin:
                for _ in range(OTA_CONTROL_REPEAT):
                    self.transport.sendto(_build_begin(sid, img), addr)
            for seq in seqs:
                self.transport.sendto(_build_chunk(sid, img, seq), addr)
                if pacing > 0:
                    await asyncio.sleep(pacing)
            for _ in range(OTA_CONTROL_REPEAT):
                self.transport.sendto(_build_end(sid, img), addr)
        except Exception as exc:
            # A send failure must never crash the data-engine; the STM re-NAKs on timeout.
            logger.warning("[OTA] serve to shuttle %d failed: %s", sid, exc)

    def error_received(self, exc: Exception) -> None:
        logger.error("[OTA] UDP socket error: %s", exc)


# ---------------------------------------------------------------------------
# Entry point — called from data-engine.py main()
# ---------------------------------------------------------------------------

# Module-level store so data-engine can read offered_version() for the beacon token.
_store: FirmwareStore | None = None


# fw_version offered for the beacon `:fw=` token, or None if no firmware on disk.
def offered_version() -> int | None:
    return _store.offered_version() if _store is not None else None


# Bind the OTA UDP endpoint. Returns once the listener is up; serving is event-driven.
async def start_ota_server(firmware_dir: str | None = None) -> None:
    global _store
    _store = FirmwareStore(firmware_dir or OTA_FIRMWARE_DIR)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: OtaProtocol(_store),
        local_addr=("0.0.0.0", OTA_PORT),
    )
    img = _store.get()
    offered = f"v{img.fw_version} ({img.total_chunks} chunks)" if img else "none on disk"
    logger.info("[OTA] firmware-update server bound on port %d | offering: %s", OTA_PORT, offered)
