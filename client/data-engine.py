"""
PLUDOS Edge Gateway: Data Engine
---------------------------------
Listens on two sockets:
  - UDP 5683 (aiocoap): CoAP CON CriticalPayload from each STM32 shuttle
    (accel xyz, power_mw, ram_pct, mission_active — wire_protocol.md §1)
  - UDP 5684 (raw datagram): NonCriticalPayload environmental data
    (temp_c, humidity_pct, pressure_hpa — wire_protocol.md §2)

Each incoming CoAP packet is ACKed immediately. Packets are buffered in
process memory per shuttle, sorted by (shuttle_id, sequence_monotonic), and
flushed to Parquet on mission-end or when per-shuttle buffer limits are reached.

On mission-end a summary point is written to InfluxDB measurement
`stm_mission` capturing energy consumed by that shuttle during the mission.

Configuration entirely through environment variables — no hardcoded values.
"""

import asyncio
import logging
import os
import socket
import struct
import threading
import time

import aiocoap
import aiocoap.resource as resource
import pandas as pd
from influxdb_client import InfluxDBClient, Point, WritePrecision  # type: ignore
from influxdb_client.client.write_api import SYNCHRONOUS           # type: ignore

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

TEST_MODE   = os.getenv("TEST_MODE") == "1"
COAP_PORT   = int(os.getenv("COAP_PORT",   "5683"))
UDP_NC_PORT = int(os.getenv("UDP_NC_PORT", "5684"))

# Per-shuttle buffer limits (independent of other shuttles).
# At 50 Hz (MOVING state): SHUTTLE_SOFT_LIMIT=400 → proactive flush after 8 s,
# SHUTTLE_HARD_LIMIT=600 → emergency flush after 12 s.
# Old BUFFER_SOFT/HARD_LIMIT env vars controlled a global total — renamed to
# per-shuttle semantics. Deployments using the old names must update .env.
SHUTTLE_SOFT_LIMIT = int(os.getenv("SHUTTLE_SOFT_LIMIT", "400"))
SHUTTLE_HARD_LIMIT = int(os.getenv("SHUTTLE_HARD_LIMIT", "600"))

# Emergency safety ceiling across the entire gateway (all shuttles combined).
# At 39 bytes/packet and 8 GB Jetson RAM, 50 000 packets ≈ 2 MB — very conservative.
GATEWAY_HARD_LIMIT = int(os.getenv("GATEWAY_HARD_LIMIT", "50000"))

if SHUTTLE_HARD_LIMIT <= SHUTTLE_SOFT_LIMIT:
    raise ValueError(
        f"SHUTTLE_HARD_LIMIT ({SHUTTLE_HARD_LIMIT}) must be > "
        f"SHUTTLE_SOFT_LIMIT ({SHUTTLE_SOFT_LIMIT})"
    )

# Re-anchor NTP offset every N packets per shuttle to correct for STM32 crystal drift.
NTP_REFRESH_INTERVAL = int(os.getenv("NTP_REFRESH_INTERVAL", "100"))

# Beacon broadcast: announces the gateway IP on UDP so STM32s can auto-discover it.
BEACON_PORT       = int(os.getenv("BEACON_PORT", "5000"))
BEACON_INTERVAL_S = float(os.getenv("BEACON_INTERVAL_S", "10"))
GATEWAY_IP        = os.getenv("GATEWAY_IP", "")

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

# InfluxDB connection for mission-summary writes (stm_mission measurement).
_INFLUXDB_URL    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
_INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN",  "")
_INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG",    "pludos")
_INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

# Human-readable gateway tag written to every stm_mission InfluxDB point.
_GATEWAY_TAG = os.getenv("JETSON_HOSTNAME", socket.gethostname())

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md exactly.
# ---------------------------------------------------------------------------

# §1 CriticalPayload: 39 bytes  <12s H I B f f f f f>
CRITICAL_FMT  = "<12sHIBfffff"
CRITICAL_SIZE = struct.calcsize(CRITICAL_FMT)

# §2 NonCriticalPayload: 30 bytes  <12s H I f f f>
NC_FMT  = "<12sHIfff"
NC_SIZE = struct.calcsize(NC_FMT)

# ---------------------------------------------------------------------------
# Mutable gateway state — per-shuttle dicts (P2-9 fix).
# Single-threaded asyncio — no locking required.
# ---------------------------------------------------------------------------

# Critical CoAP packets waiting for Parquet flush, keyed by shuttle_id.
_critical_buf: dict[str, list[dict]] = {}

# Non-critical UDP packets waiting for Parquet flush, keyed by shuttle_id.
_nc_buf: dict[str, list[dict]] = {}

# Cumulative STM32-estimated energy per shuttle in Joules, reset after each mission flush.
_mission_energy_j: dict[str, float] = {}

# Wall-clock time of the first packet from each shuttle in the current mission.
_mission_start_wall: dict[str, float] = {}

# Per-shuttle NTP offset (ms). Set on first packet; refreshed every NTP_REFRESH_INTERVAL
# packets to correct for STM32 crystal drift. Reset on mission end.
_ntp_offsets: dict[str, int] = {}

# Per-shuttle packet counter driving the periodic NTP offset refresh.
_packet_counts: dict[str, int] = {}

# Per-shuttle wall-clock time of the last received CoAP packet.
_last_packet_wall: dict[str, float] = {}

# Per-shuttle last seen sequence_id — used to detect uint16 wrap (0 after 65535).
_last_seq_ids: dict[str, int] = {}

# Per-shuttle wrap count: incremented each time sequence_id wraps 65535 → 0.
# Used to construct a monotonically increasing sequence_monotonic field so
# Parquet sort order is correct even across wrap boundaries.
_seq_wrap_counts: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flush(buf: list[dict], prefix: str) -> None:
    """Sort buf by (shuttle_id, sequence_monotonic) and write atomic Parquet; clears buf."""
    if not buf:
        return
    df = pd.json_normalize(buf)

    # Sort by monotonic sequence (accounts for uint16 wrap) rather than raw sequence_id.
    # sequence_monotonic is computed per-packet in render_post using _seq_wrap_counts.
    sort_cols = ["header.shuttle_id", "header.sequence_monotonic"]
    if all(c in df.columns for c in sort_cols):
        df.sort_values(by=sort_cols, inplace=True)
    else:
        # Fallback: sort by raw sequence_id (acceptable for missions under 22 min at 50 Hz).
        df.sort_values(by=["header.shuttle_id", "header.sequence_id"], inplace=True)

    ts        = int(time.time())
    file_path = os.path.join(BUFFER_DIR, f"{prefix}_{ts}.parquet")
    tmp_path  = file_path + ".tmp"
    # PyArrow write is sync but only fires on flush — acceptable latency spike.
    df.to_parquet(tmp_path, engine="pyarrow")
    os.replace(tmp_path, file_path)  # atomic rename: crash-safe on Linux
    logger.info("Flushed %d records → %s", len(buf), file_path)
    buf.clear()


def _write_mission_summary(
    shuttle_id: str,
    energy_j: float,
    packets: int,
    duration_ms: float,
) -> None:
    # Fire-and-forget: writes stm_mission summary to InfluxDB in a background
    # thread so the asyncio event loop is never blocked.
    def _write() -> None:
        client = InfluxDBClient(url=_INFLUXDB_URL, token=_INFLUXDB_TOKEN, org=_INFLUXDB_ORG)
        try:
            point = (
                Point("stm_mission")
                .tag("shuttle_id", shuttle_id)
                .tag("gateway",    _GATEWAY_TAG)
                .field("energy_j",    energy_j)
                .field("packets",     packets)
                .field("duration_ms", duration_ms)
                .time(time.time_ns(), WritePrecision.NS)
            )
            client.write_api(write_options=SYNCHRONOUS).write(
                bucket=_INFLUXDB_BUCKET, record=point
            )
            logger.info(
                "[INFLUXDB] stm_mission shuttle=%s energy=%.4fJ pkts=%d dur=%.0fms",
                shuttle_id, energy_j, packets, duration_ms,
            )
        except Exception as exc:
            logger.warning("[INFLUXDB] stm_mission write failed (%s): %s", shuttle_id, exc)
        finally:
            # Always release the connection pool, even on write failure.
            client.close()

    threading.Thread(target=_write, daemon=True).start()


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
        # 0.0 is the LPS22HH unavailability sentinel — replace with NaN so ML
        # pipelines treat it as missing data rather than a real 0 hPa reading.
        "env.pressure_hpa":   float("nan") if pres == 0.0 else pres,
    }

# ---------------------------------------------------------------------------
# CoAP resource — handles CriticalPayload at /vib (and fallback paths)
# ---------------------------------------------------------------------------

class CriticalResource(resource.Resource):
    """CoAP POST handler for CriticalPayload packets from STM32 shuttles."""

    async def render_post(self, request):
        global _critical_buf, _mission_energy_j, _ntp_offsets, _last_packet_wall
        global _packet_counts, _last_seq_ids, _seq_wrap_counts

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

            # NTP offset: established on the first packet, refreshed every
            # NTP_REFRESH_INTERVAL packets to limit STM32 crystal-drift accumulation.
            # Note: timestamp_ms represents gateway receipt time, not STM32 emission time.
            # The systematic bias equals the one-way network latency (~sub-ms on LAN).
            receipt_ms = int(time.time() * 1000)
            _packet_counts[shuttle_id] = _packet_counts.get(shuttle_id, 0) + 1
            count = _packet_counts[shuttle_id]

            now = time.monotonic()

            if shuttle_id not in _ntp_offsets:
                _ntp_offsets[shuttle_id] = receipt_ms - tick_ms
                _mission_start_wall[shuttle_id] = now
                logger.info("[%s] NTP offset established: %d ms", shuttle_id, _ntp_offsets[shuttle_id])
            elif count % NTP_REFRESH_INTERVAL == 0:
                old_offset = _ntp_offsets[shuttle_id]
                _ntp_offsets[shuttle_id] = receipt_ms - tick_ms
                drift_ms = _ntp_offsets[shuttle_id] - old_offset
                logger.info(
                    "[%s] NTP offset refreshed at pkt %d: %d ms (drift %+d ms)",
                    shuttle_id, count, _ntp_offsets[shuttle_id], drift_ms,
                )
            pkt["timestamp_ms"] = tick_ms + _ntp_offsets[shuttle_id]

            # Detect uint16 sequence_id wrap (65535 → 0). At 50 Hz this occurs after
            # ~22 minutes of continuous MOVING. Wrap increments the shuttle's counter so
            # sequence_monotonic is always increasing — making the Parquet sort correct.
            last_seq = _last_seq_ids.get(shuttle_id, sequence_id)
            if last_seq > 60000 and sequence_id < 5000:
                _seq_wrap_counts[shuttle_id] = _seq_wrap_counts.get(shuttle_id, 0) + 1
                logger.info(
                    "[%s] sequence_id wrap #%d detected (was %d → %d)",
                    shuttle_id, _seq_wrap_counts[shuttle_id], last_seq, sequence_id,
                )
            _last_seq_ids[shuttle_id] = sequence_id
            pkt["header.sequence_monotonic"] = (
                sequence_id + _seq_wrap_counts.get(shuttle_id, 0) * 65536
            )

            # Energy integration: use actual wall-clock elapsed time so the
            # estimate is correct across both 50 Hz (MOVING) and 2 Hz (IDLE) states.
            if shuttle_id in _last_packet_wall:
                elapsed_s = now - _last_packet_wall[shuttle_id]
            else:
                elapsed_s = 0.02  # conservative fallback for the very first packet
            _last_packet_wall[shuttle_id] = now

            _mission_energy_j.setdefault(shuttle_id, 0.0)
            _mission_energy_j[shuttle_id] += (power_mw / 1000.0) * elapsed_s  # mW→W×s = J

            logger.info(
                "[%s] seq=%d mono=%d tick=%d ram=%.0f%% power=%.0fmW e=%.4fJ",
                shuttle_id, sequence_id, pkt["header.sequence_monotonic"],
                tick_ms, pkt["status.stm_ram_pct"], power_mw,
                _mission_energy_j[shuttle_id],
            )

            # Append to this shuttle's buffer only (P2-9 fix: per-shuttle dict).
            _critical_buf.setdefault(shuttle_id, []).append(pkt)

            shuttle_pkts = len(_critical_buf[shuttle_id])
            total_pkts   = sum(len(v) for v in _critical_buf.values())

            if not mission_active:
                # STM32 signalled end of mission — flush only this shuttle's data.
                mission_energy = _mission_energy_j[shuttle_id]
                mission_pkts   = _packet_counts[shuttle_id]
                duration_ms    = (now - _mission_start_wall.get(shuttle_id, now)) * 1000.0
                logger.info(
                    "[%s] mission_active=0 | energy=%.4fJ | pkts=%d | dur=%.0fms | flushing",
                    shuttle_id, mission_energy, mission_pkts, duration_ms,
                )
                _flush(_critical_buf.pop(shuttle_id, []), "mission")
                _flush(_nc_buf.pop(shuttle_id, []),       "env")
                _write_mission_summary(shuttle_id, mission_energy, mission_pkts, duration_ms)

                # Reset all per-shuttle state so the next mission starts fresh.
                for store in (
                    _mission_energy_j, _mission_start_wall, _ntp_offsets,
                    _last_packet_wall, _packet_counts, _last_seq_ids, _seq_wrap_counts,
                ):
                    store.pop(shuttle_id, None)

            elif shuttle_pkts >= SHUTTLE_HARD_LIMIT:
                # This shuttle hit its individual hard ceiling — flush it only.
                # Other shuttles' in-progress missions are not disturbed.
                logger.warning(
                    "[%s] per-shuttle HARD LIMIT (%d pkts) — mid-mission flush",
                    shuttle_id, shuttle_pkts,
                )
                _flush(_critical_buf[shuttle_id], "mission")
                # Key stays in dict (list cleared in-place by _flush) — next packets append normally.

            elif shuttle_pkts >= SHUTTLE_SOFT_LIMIT:
                # Proactive flush for this shuttle — keep gateway RAM comfortable.
                logger.info(
                    "[%s] per-shuttle soft limit (%d pkts) — proactive flush",
                    shuttle_id, shuttle_pkts,
                )
                _flush(_critical_buf[shuttle_id], "mission")

            elif total_pkts >= GATEWAY_HARD_LIMIT:
                # Emergency: combined buffer across all shuttles hit the gateway ceiling.
                # This should never happen under normal operating conditions.
                logger.error(
                    "GATEWAY HARD LIMIT (%d total pkts across %d shuttles) — emergency flush all",
                    total_pkts, len(_critical_buf),
                )
                for s_buf in list(_critical_buf.values()):
                    _flush(s_buf, "mission")
                _critical_buf.clear()

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
            logger.debug(
                "NC UDP: unexpected size %d from %s (expected %d)", len(data), addr, NC_SIZE
            )
            return
        try:
            pkt = _unpack_nc(data)
            logger.info(
                "[%s] NC seq=%d temp=%.1f°C hum=%.0f%% pres=%s hPa",
                pkt["header.shuttle_id"], pkt["header.sequence_id"],
                pkt["env.temp_c"], pkt["env.humidity_pct"],
                f"{pkt['env.pressure_hpa']:.1f}" if pkt["env.pressure_hpa"] == pkt["env.pressure_hpa"]
                else "n/a",
            )
            # Append to this shuttle's NC buffer (P2-9 fix: per-shuttle dict).
            _nc_buf.setdefault(pkt["header.shuttle_id"], []).append(pkt)
        except struct.error as exc:
            logger.warning("NC UDP unpack error from %s: %s", addr, exc)

    def error_received(self, exc: Exception) -> None:
        logger.error("NC UDP socket error: %s", exc)

# ---------------------------------------------------------------------------
# Beacon broadcast — P2-1 zero-touch provisioning
# ---------------------------------------------------------------------------

def _detect_local_ip() -> str:
    # UDP connect trick: the OS picks the outbound interface without sending any packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


async def _broadcast_beacon() -> None:
    # Sends "PLUDOS-GW:<ip>" to 255.255.255.255 so STM32s can read the source IP
    # and skip the hardcoded JETSON_IP. Requires host networking on the Jetson to
    # escape the container bridge and reach the WiFi subnet (see .env.example).
    ip = GATEWAY_IP or _detect_local_ip()
    payload = f"PLUDOS-GW:{ip}".encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    logger.info("[BEACON] announcing %s on UDP port %d every %.0f s", ip, BEACON_PORT, BEACON_INTERVAL_S)
    try:
        while True:
            try:
                await loop.sock_sendto(sock, payload, ("255.255.255.255", BEACON_PORT))
            except OSError as exc:
                logger.warning("[BEACON] broadcast failed: %s", exc)
            await asyncio.sleep(BEACON_INTERVAL_S)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info(
        "PLUDOS Data Engine starting | TEST_MODE=%s | CoAP=%d | NC-UDP=%d | "
        "shuttle soft=%d hard=%d | gateway hard=%d | dir=%s",
        TEST_MODE, COAP_PORT, UDP_NC_PORT,
        SHUTTLE_SOFT_LIMIT, SHUTTLE_HARD_LIMIT, GATEWAY_HARD_LIMIT, BUFFER_DIR,
    )

    # CoAP server — STM32 sends CriticalPayload to /vib.
    root = resource.Site()
    coap_resource = CriticalResource()
    root.add_resource([],            coap_resource)
    root.add_resource(["vib"],       coap_resource)
    root.add_resource(["telemetry"], coap_resource)
    await aiocoap.Context.create_server_context(root, bind=("0.0.0.0", COAP_PORT))
    logger.info("CoAP server listening on port %d", COAP_PORT)

    # Raw UDP listener — STM32 sends NonCriticalPayload to a separate port.
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        NonCriticalProtocol,
        local_addr=("0.0.0.0", UDP_NC_PORT),
    )
    logger.info("NC UDP listener on port %d", UDP_NC_PORT)

    # Beacon broadcast: lets STM32s discover the gateway IP over UDP 5000.
    asyncio.create_task(_broadcast_beacon())

    await loop.create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")
