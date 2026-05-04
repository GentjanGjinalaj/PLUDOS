"""
PLUDOS Edge Gateway: Data Engine
---------------------------------
Listens on two sockets:
  - UDP 5683 (aiocoap): CoAP CON CriticalPayload from each STM32 shuttle
    (accel xyz, power_mw, ram_pct, mission_active — wire_protocol.md §1)
  - UDP 5684 (raw datagram): NonCriticalPayload environmental data
    (temp_c, humidity_pct, pressure_hpa — wire_protocol.md §2)

Each incoming CoAP packet is ACKed immediately. Packets are buffered in
process memory, sorted by (shuttle_id, sequence_id), and flushed to Parquet
on mission-end or when buffer limits are reached.

Configuration entirely through environment variables — no hardcoded values.
"""

import asyncio
import logging
import os
import struct
import time

import aiocoap
import aiocoap.resource as resource
import pandas as pd

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("data-engine")

# ---------------------------------------------------------------------------
# Configuration — all values come from environment variables with safe defaults
# ---------------------------------------------------------------------------

TEST_MODE         = os.getenv("TEST_MODE") == "1"
COAP_PORT         = int(os.getenv("COAP_PORT", "5683"))
UDP_NC_PORT       = int(os.getenv("UDP_NC_PORT", "5684"))

# Buffer limits: soft triggers a proactive flush; hard is the emergency ceiling.
# Hard must be > soft. Both can be tuned without rebuilding the container.
BUFFER_SOFT_LIMIT = int(os.getenv("BUFFER_SOFT_LIMIT", "400"))
BUFFER_HARD_LIMIT = int(os.getenv("BUFFER_HARD_LIMIT", "500"))

if BUFFER_HARD_LIMIT <= BUFFER_SOFT_LIMIT:
    raise ValueError(
        f"BUFFER_HARD_LIMIT ({BUFFER_HARD_LIMIT}) must be greater than "
        f"BUFFER_SOFT_LIMIT ({BUFFER_SOFT_LIMIT})"
    )

# Parquet output directory: use tmpfs mount when inside container, local dir in test.
_DEFAULT_BUFFER_DIR   = "./ram_buffer"
_CONTAINER_BUFFER_DIR = "/app/ram_buffer"
BUFFER_DIR = (
    _DEFAULT_BUFFER_DIR
    if TEST_MODE or not os.path.isdir("/app")
    else _CONTAINER_BUFFER_DIR
)
try:
    os.makedirs(BUFFER_DIR, exist_ok=True)
except PermissionError:
    logger.warning("Cannot write to %s, falling back to %s", BUFFER_DIR, _DEFAULT_BUFFER_DIR)
    BUFFER_DIR = _DEFAULT_BUFFER_DIR
    os.makedirs(BUFFER_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md exactly.
# Update both here and the STM32 structs if the layout changes.
# ---------------------------------------------------------------------------

# §1 CriticalPayload: 39 bytes  <12s H I B f f f f f>
#   shuttle_id[12], sequence_id(u16), tick_ms(u32), mission_active(u8),
#   ram_usage_pct(f), accel_x(f), accel_y(f), accel_z(f), power_mw(f)
CRITICAL_FMT  = "<12sHIBfffff"
CRITICAL_SIZE = struct.calcsize(CRITICAL_FMT)

# §2 NonCriticalPayload: 30 bytes  <12s H I f f f>
#   shuttle_id[12], sequence_id(u16), tick_ms(u32),
#   temp_c(f), humidity_pct(f), pressure_hpa(f)
NC_FMT  = "<12sHIfff"
NC_SIZE = struct.calcsize(NC_FMT)

# ---------------------------------------------------------------------------
# Mutable gateway state
# Single-threaded asyncio — no locking required.
# ---------------------------------------------------------------------------

# Critical CoAP packets waiting for Parquet flush.
_critical_buf: list[dict] = []

# Non-critical UDP packets waiting for Parquet flush.
_nc_buf: list[dict] = []

# Cumulative energy per shuttle in Joules, reset after each mission flush.
_mission_energy_j: dict[str, float] = {}

# Per-shuttle NTP offset (ms). Set once on the first packet of each mission;
# anchors the STM32 relative tick_ms to the gateway's NTP wall clock.
# Intentionally not refreshed mid-mission (see P1-4 in current_problems.md).
_ntp_offsets: dict[str, int] = {}

# Per-shuttle wall-clock time of the last received CoAP packet.
# Used to compute real elapsed time for energy integration.
_last_packet_wall: dict[str, float] = {}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flush(buf: list[dict], prefix: str) -> None:
    """Sort buf by (shuttle_id, sequence_id) and write atomic Parquet; clears buf."""
    if not buf:
        return
    df = pd.json_normalize(buf)
    df.sort_values(by=["header.shuttle_id", "header.sequence_id"], inplace=True)
    ts        = int(time.time())
    file_path = os.path.join(BUFFER_DIR, f"{prefix}_{ts}.parquet")
    tmp_path  = file_path + ".tmp"
    # PyArrow write is sync but only fires on flush — acceptable latency spike.
    df.to_parquet(tmp_path, engine="pyarrow")
    os.replace(tmp_path, file_path)  # atomic rename: crash-safe on Linux
    logger.info("Flushed %d records → %s", len(buf), file_path)
    buf.clear()


def _unpack_critical(raw: bytes) -> dict:
    """Unpack CriticalPayload bytes into a labelled dict; raises struct.error on bad data."""
    sid, seq, tick, active, ram_pct, ax, ay, az, pwr = struct.unpack(CRITICAL_FMT, raw)
    return {
        "header.shuttle_id":     sid.decode("utf-8").rstrip("\x00"),
        "header.sequence_id":    seq,
        "status.tick_ms":        tick,
        "status.mission_active": bool(active),
        "status.stm_ram_pct":    ram_pct,
        "sensors.accel_x":       ax,
        "sensors.accel_y":       ay,
        "sensors.accel_z":       az,
        "energy.power_mw":       pwr,
    }


def _unpack_nc(raw: bytes) -> dict:
    """Unpack NonCriticalPayload bytes into a labelled dict; raises struct.error on bad data."""
    sid, seq, tick, temp, hum, pres = struct.unpack(NC_FMT, raw)
    return {
        "header.shuttle_id":  sid.decode("utf-8").rstrip("\x00"),
        "header.sequence_id": seq,
        "status.tick_ms":     tick,
        "env.temp_c":         temp,
        "env.humidity_pct":   hum,
        "env.pressure_hpa":   pres,
    }

# ---------------------------------------------------------------------------
# CoAP resource — handles CriticalPayload at /vib (and fallback paths)
# ---------------------------------------------------------------------------

class CriticalResource(resource.Resource):
    """CoAP POST handler for CriticalPayload packets from STM32 shuttles."""

    async def render_post(self, request):
        global _critical_buf, _mission_energy_j, _ntp_offsets, _last_packet_wall

        try:
            if len(request.payload) != CRITICAL_SIZE:
                logger.warning(
                    "CoAP: bad payload size %d (expected %d)", len(request.payload), CRITICAL_SIZE
                )
                return aiocoap.Message(code=aiocoap.BAD_REQUEST)

            pkt = _unpack_critical(request.payload)
            shuttle_id     = pkt["header.shuttle_id"]
            sequence_id    = pkt["header.sequence_id"]
            tick_ms        = pkt["status.tick_ms"]
            mission_active = pkt["status.mission_active"]
            power_mw       = pkt["energy.power_mw"]

            # Per-shuttle NTP offset: set once per mission on the first packet.
            receipt_ms = int(time.time() * 1000)
            if shuttle_id not in _ntp_offsets:
                _ntp_offsets[shuttle_id] = receipt_ms - tick_ms
                logger.info("[%s] NTP offset = %d ms", shuttle_id, _ntp_offsets[shuttle_id])
            pkt["timestamp_ms"] = tick_ms + _ntp_offsets[shuttle_id]

            # Energy integration: use actual wall-clock elapsed time so the
            # estimate is correct across both 50 Hz (MOVING) and 2 Hz (IDLE) states.
            now = time.monotonic()
            if shuttle_id in _last_packet_wall:
                elapsed_s = now - _last_packet_wall[shuttle_id]
            else:
                elapsed_s = 0.02  # conservative fallback for the very first packet
            _last_packet_wall[shuttle_id] = now

            _mission_energy_j.setdefault(shuttle_id, 0.0)
            _mission_energy_j[shuttle_id] += (power_mw / 1000.0) * elapsed_s  # mW→W * s = J

            logger.info(
                "[%s] seq=%d tick=%d ram=%.0f%% power=%.0fmW e=%.4fJ",
                shuttle_id, sequence_id, tick_ms,
                pkt["status.stm_ram_pct"], power_mw,
                _mission_energy_j[shuttle_id],
            )

            _critical_buf.append(pkt)
            buf_len = len(_critical_buf)

            if not mission_active:
                # STM32 signalled end of mission — flush everything for this shuttle.
                logger.info(
                    "[%s] mission_active=0 | total energy=%.4f J | flushing",
                    shuttle_id, _mission_energy_j[shuttle_id],
                )
                _flush(_critical_buf, "mission")
                _flush(_nc_buf,       "env")
                _mission_energy_j.pop(shuttle_id, None)
                # Reset NTP offset so the next mission re-anchors cleanly.
                _ntp_offsets.pop(shuttle_id, None)
                _last_packet_wall.pop(shuttle_id, None)

            elif buf_len >= BUFFER_HARD_LIMIT:
                # Emergency: buffer is at the ceiling — flush now regardless of mission state.
                logger.warning("HARD LIMIT (%d pkts) — emergency flush", buf_len)
                _flush(_critical_buf, "mission")

            elif buf_len >= BUFFER_SOFT_LIMIT:
                # Proactive flush to keep gateway RAM comfortable.
                logger.info("Soft limit (%d pkts) — proactive flush", buf_len)
                _flush(_critical_buf, "mission")

            return aiocoap.Message(code=aiocoap.CHANGED)

        except struct.error as exc:
            logger.error("CoAP unpack error: %s", exc)
            return aiocoap.Message(code=aiocoap.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error processing CoAP packet: %s", exc)
            return aiocoap.Message(code=aiocoap.INTERNAL_SERVER_ERROR)

# ---------------------------------------------------------------------------
# Raw UDP protocol — handles NonCriticalPayload on port 5684
# ---------------------------------------------------------------------------

class NonCriticalProtocol(asyncio.DatagramProtocol):
    """Asyncio datagram handler for raw UDP NonCriticalPayload from STM32 shuttles."""

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) != NC_SIZE:
            # Silently ignore wrong-size datagrams (e.g. stale 26-byte packets
            # from a shuttle that hasn't been reflashed yet).
            logger.debug(
                "NC UDP: unexpected size %d from %s (expected %d)", len(data), addr, NC_SIZE
            )
            return
        try:
            pkt = _unpack_nc(data)
            logger.info(
                "[%s] NC seq=%d temp=%.1f°C hum=%.0f%% pres=%.1f hPa",
                pkt["header.shuttle_id"], pkt["header.sequence_id"],
                pkt["env.temp_c"], pkt["env.humidity_pct"], pkt["env.pressure_hpa"],
            )
            _nc_buf.append(pkt)
        except struct.error as exc:
            logger.warning("NC UDP unpack error from %s: %s", addr, exc)

    def error_received(self, exc: Exception) -> None:
        logger.error("NC UDP socket error: %s", exc)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info(
        "PLUDOS Data Engine starting | TEST_MODE=%s | CoAP=%d | NC-UDP=%d | "
        "buf soft=%d hard=%d | dir=%s",
        TEST_MODE, COAP_PORT, UDP_NC_PORT,
        BUFFER_SOFT_LIMIT, BUFFER_HARD_LIMIT, BUFFER_DIR,
    )

    # CoAP server — STM32 sends CriticalPayload to /vib.
    root = resource.Site()
    coap_resource = CriticalResource()
    root.add_resource([],            coap_resource)
    root.add_resource(["vib"],       coap_resource)
    root.add_resource(["telemetry"], coap_resource)
    await aiocoap.Context.create_server_context(root, bind=("0.0.0.0", COAP_PORT))
    logger.info("CoAP server listening on port %d", COAP_PORT)

    # Raw UDP listener — STM32 sends NonCriticalPayload to a separate port to
    # avoid collision with aiocoap's socket on 5683.
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        NonCriticalProtocol,
        local_addr=("0.0.0.0", UDP_NC_PORT),
    )
    logger.info("NC UDP listener on port %d", UDP_NC_PORT)

    # Run until interrupted.
    await loop.create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")
