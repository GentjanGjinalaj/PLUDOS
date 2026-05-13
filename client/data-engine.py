"""
PLUDOS Edge Gateway: Data Engine (ADR-015 unified-stream rewrite)
-----------------------------------------------------------------
Listens on a single raw UDP socket:
  - UDP 5683: PludosTelemetry packets from each STM32 shuttle
    (47 bytes — accel xyz + temp/humidity/pressure + power_mw + state)
    See `docs/wire_protocol.md §1`.

Every incoming packet is appended to that shuttle's in-memory buffer. A
mission is delimited by the `state` field: when a shuttle has been
streaming `state == MOVING` packets and then stays in `state == IDLE` for
`MISSION_END_IDLE_S` (default 30 s), the gateway flushes that shuttle's
buffer to a single Parquet file and writes a summary row to InfluxDB
`stm_mission`.

There is no CoAP and no second port. Packet loss is tolerated — the next
20 ms / 1 s sample arrives anyway. See `docs/decisions.md` ADR-015.
"""

import asyncio
import logging
import math
import os
import socket
import struct
import threading
import time

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

TEST_MODE       = os.getenv("TEST_MODE") == "1"
TELEMETRY_PORT  = int(os.getenv("TELEMETRY_PORT", os.getenv("COAP_PORT", "5683")))

# Per-shuttle buffer limits (independent of other shuttles).
# At 50 Hz MOVING: SHUTTLE_SOFT_LIMIT=400 → proactive flush after 8 s,
# SHUTTLE_HARD_LIMIT=600 → emergency flush after 12 s.
SHUTTLE_SOFT_LIMIT = int(os.getenv("SHUTTLE_SOFT_LIMIT", "400"))
SHUTTLE_HARD_LIMIT = int(os.getenv("SHUTTLE_HARD_LIMIT", "600"))

# Emergency safety ceiling across the entire gateway (all shuttles combined).
# At 47 bytes/packet and 8 GB Jetson RAM, 50 000 packets ≈ 2.4 MB.
GATEWAY_HARD_LIMIT = int(os.getenv("GATEWAY_HARD_LIMIT", "50000"))

if SHUTTLE_HARD_LIMIT <= SHUTTLE_SOFT_LIMIT:
    raise ValueError(
        f"SHUTTLE_HARD_LIMIT ({SHUTTLE_HARD_LIMIT}) must be > "
        f"SHUTTLE_SOFT_LIMIT ({SHUTTLE_SOFT_LIMIT})"
    )

# Re-anchor NTP offset every N packets per shuttle to correct STM32 crystal drift.
NTP_REFRESH_INTERVAL = int(os.getenv("NTP_REFRESH_INTERVAL", "100"))

# Mission-end detection — after this many seconds of state==IDLE following
# any state==MOVING run, flush the shuttle's buffer as one Parquet file.
MISSION_END_IDLE_S = float(os.getenv("MISSION_END_IDLE_S", "30"))

# Per-second status log: roll up "tx rate" and last-seen sensor values rather
# than logging every packet (50 Hz × 100 shuttles would drown the terminal).
STATUS_LOG_PERIOD_S = float(os.getenv("STATUS_LOG_PERIOD_S", "1.0"))

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

# State enum mirrors the STM32 firmware.
STATE_IDLE   = 0
STATE_MOVING = 1

# ---------------------------------------------------------------------------
# Wire format — must match wire_protocol.md §1 exactly.
# ---------------------------------------------------------------------------

# Packed (47 bytes): firmware built with __attribute__((packed)) or #pragma pack(push,1).
TELEMETRY_FMT_PACKED  = "<12sHIBfffffff"
TELEMETRY_SIZE_PACKED = struct.calcsize(TELEMETRY_FMT_PACKED)
assert TELEMETRY_SIZE_PACKED == 47, f"packed fmt must be 47, got {TELEMETRY_SIZE_PACKED}"

# Natural alignment (52 bytes): ARM GCC adds 2 bytes pad before tick_ms and
# 3 bytes pad before accel_x when pragma pack is not honored by the IDE build.
TELEMETRY_FMT_NATURAL  = "<12sH2xIB3xfffffff"
TELEMETRY_SIZE_NATURAL = struct.calcsize(TELEMETRY_FMT_NATURAL)
assert TELEMETRY_SIZE_NATURAL == 52, f"natural fmt must be 52, got {TELEMETRY_SIZE_NATURAL}"

VALID_TELEMETRY_SIZES = {TELEMETRY_SIZE_PACKED, TELEMETRY_SIZE_NATURAL}
# Canonical size after firmware is reflashed with __attribute__((packed))
TELEMETRY_SIZE = TELEMETRY_SIZE_PACKED

# ---------------------------------------------------------------------------
# Mutable gateway state — per-shuttle dicts, single-threaded asyncio.
# ---------------------------------------------------------------------------

# All telemetry packets waiting for Parquet flush, keyed by shuttle_id.
_telemetry_buf: dict[str, list[dict]] = {}

# Cumulative STM32-estimated energy per shuttle in Joules, reset after each mission flush.
_mission_energy_j: dict[str, float] = {}

# Wall-clock time of the first packet of the current mission per shuttle.
_mission_start_wall: dict[str, float] = {}

# Per-shuttle NTP offset (ms). Set on first packet; refreshed every NTP_REFRESH_INTERVAL.
_ntp_offsets: dict[str, int] = {}

# Per-shuttle packet counter driving the periodic NTP offset refresh.
_packet_counts: dict[str, int] = {}

# Per-shuttle wall-clock time of the last received packet.
_last_packet_wall: dict[str, float] = {}

# Per-shuttle wall-clock time the most recent state==MOVING packet was seen.
# None until the first MOVING packet for the current mission.
_last_moving_wall: dict[str, float | None] = {}

# Per-shuttle last seen sequence_id — used to detect uint16 wrap.
_last_seq_ids: dict[str, int] = {}

# Per-shuttle wrap count: incremented each time sequence_id wraps 65535 → 0.
_seq_wrap_counts: dict[str, int] = {}

# Per-shuttle latest sensor snapshot for the per-second status log.
_last_sample: dict[str, dict] = {}

# Per-shuttle running count of packets received in the current STATUS_LOG_PERIOD_S window.
_tx_rate_window: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flush(buf: list[dict], prefix: str) -> None:
    """Sort by (shuttle_id, sequence_monotonic) and write atomic Parquet; clears buf."""
    if not buf:
        return
    df = pd.json_normalize(buf)

    sort_cols = ["header.shuttle_id", "header.sequence_monotonic"]
    if all(c in df.columns for c in sort_cols):
        df.sort_values(by=sort_cols, inplace=True)
    else:
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
    # Fire-and-forget background thread so the asyncio loop is never blocked by InfluxDB I/O.
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
            client.close()

    threading.Thread(target=_write, daemon=True).start()


def _unpack_telemetry(raw: bytes) -> dict:
    """Unpack a PludosTelemetry packet (47 or 52 bytes) into a labelled dict.
    52-byte variant: natural ARM alignment without __attribute__((packed)).
    Wire sentinels (-999.0 °C, 0.0 hPa) are converted to NaN so downstream ML treats
    them as missing rather than real readings."""
    if len(raw) == TELEMETRY_SIZE_PACKED:
        fmt = TELEMETRY_FMT_PACKED
    elif len(raw) == TELEMETRY_SIZE_NATURAL:
        fmt = TELEMETRY_FMT_NATURAL
    else:
        raise struct.error(f"unexpected size {len(raw)}, expected 47 or 52")
    sid, seq, tick, state, ax, ay, az, temp, hum, pres, pwr = struct.unpack(fmt, raw)

    # Sentinel decoding: see wire_protocol.md §1 field notes.
    temp_out = float("nan") if temp == -999.0 else temp
    hum_out  = float("nan") if temp == -999.0 else hum
    pres_out = float("nan") if pres ==    0.0 else pres

    return {
        "header.shuttle_id":  sid.decode("utf-8").rstrip("\x00"),
        "header.sequence_id": seq,
        "status.tick_ms":     tick,
        "status.state":       int(state),         # 0 = IDLE, 1 = MOVING
        "status.is_moving":   bool(state == STATE_MOVING),
        "sensors.accel_x":    ax,
        "sensors.accel_y":    ay,
        "sensors.accel_z":    az,
        "env.temp_c":         temp_out,
        "env.humidity_pct":   hum_out,
        "env.pressure_hpa":   pres_out,
        "energy.power_mw":    pwr,
    }


def _maybe_flush_mission(shuttle_id: str, now: float) -> None:
    """If the shuttle has been IDLE for >= MISSION_END_IDLE_S after any MOVING run,
    flush its buffer to one Parquet file and reset all per-shuttle state."""
    last_moving = _last_moving_wall.get(shuttle_id)
    if last_moving is None:
        # No MOVING packet seen yet in this mission window — nothing to flush.
        return

    if (now - last_moving) < MISSION_END_IDLE_S:
        return

    pkts          = _packet_counts.get(shuttle_id, 0)
    energy        = _mission_energy_j.get(shuttle_id, 0.0)
    started_wall  = _mission_start_wall.get(shuttle_id, now)
    duration_ms   = (now - started_wall) * 1000.0

    logger.info(
        "[%s] mission end (IDLE %.0fs) | energy=%.4fJ | pkts=%d | dur=%.0fms | flushing",
        shuttle_id, MISSION_END_IDLE_S, energy, pkts, duration_ms,
    )

    _flush(_telemetry_buf.pop(shuttle_id, []), "mission")
    _write_mission_summary(shuttle_id, energy, pkts, duration_ms)

    # Reset all per-shuttle state so the next MOVING burst starts a fresh mission.
    for store in (
        _mission_energy_j, _mission_start_wall, _ntp_offsets,
        _last_packet_wall, _packet_counts, _last_seq_ids, _seq_wrap_counts,
        _last_moving_wall, _last_sample, _tx_rate_window,
    ):
        store.pop(shuttle_id, None)


# ---------------------------------------------------------------------------
# UDP protocol — handles PludosTelemetry on port 5683
# ---------------------------------------------------------------------------

class TelemetryProtocol(asyncio.DatagramProtocol):
    """Single-port asyncio datagram handler for PludosTelemetry from all shuttles."""

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) not in VALID_TELEMETRY_SIZES:
            logger.debug(
                "Telemetry: bad size %d from %s (expected 47 or 52)", len(data), addr
            )
            return

        try:
            pkt = _unpack_telemetry(data)
        except struct.error as exc:
            logger.warning("Telemetry unpack error from %s: %s", addr, exc)
            return

        shuttle_id  = pkt["header.shuttle_id"]
        sequence_id = pkt["header.sequence_id"]
        tick_ms     = pkt["status.tick_ms"]
        state       = pkt["status.state"]
        power_mw    = pkt["energy.power_mw"]

        receipt_ms = int(time.time() * 1000)
        now        = time.monotonic()

        # First packet from this shuttle (since boot or since last mission flush):
        # establish the NTP offset and mark the mission start.
        if shuttle_id not in _ntp_offsets:
            _ntp_offsets[shuttle_id]        = receipt_ms - tick_ms
            _mission_start_wall[shuttle_id] = now
            logger.info(
                "[%s] NTP offset established: %d ms (state=%s)",
                shuttle_id, _ntp_offsets[shuttle_id],
                "MOVING" if state == STATE_MOVING else "IDLE",
            )

        # Drift correction: refresh the offset every NTP_REFRESH_INTERVAL packets.
        _packet_counts[shuttle_id] = _packet_counts.get(shuttle_id, 0) + 1
        count = _packet_counts[shuttle_id]
        if count % NTP_REFRESH_INTERVAL == 0:
            old_offset = _ntp_offsets[shuttle_id]
            _ntp_offsets[shuttle_id] = receipt_ms - tick_ms
            drift_ms = _ntp_offsets[shuttle_id] - old_offset
            logger.info(
                "[%s] NTP offset refreshed at pkt %d: %d ms (drift %+d ms)",
                shuttle_id, count, _ntp_offsets[shuttle_id], drift_ms,
            )

        pkt["timestamp_ms"] = tick_ms + _ntp_offsets[shuttle_id]

        # uint16 sequence wrap detection — STM32 counter rolls 65535 → 0.
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

        # Energy integration uses wall-clock elapsed since the previous packet so the
        # estimate is correct across IDLE (1 Hz) and MOVING (50 Hz) rates.
        if shuttle_id in _last_packet_wall:
            elapsed_s = now - _last_packet_wall[shuttle_id]
        else:
            elapsed_s = 0.02
        _last_packet_wall[shuttle_id] = now

        _mission_energy_j.setdefault(shuttle_id, 0.0)
        _mission_energy_j[shuttle_id] += (power_mw / 1000.0) * elapsed_s  # mW→W × s = J

        # Mission boundary tracking: any MOVING packet resets the IDLE timer.
        if state == STATE_MOVING:
            _last_moving_wall[shuttle_id] = now

        # Buffer the packet. Per-shuttle list so multi-shuttle deployments don't
        # interleave each other's missions in one Parquet file (P2-9 fix preserved).
        _telemetry_buf.setdefault(shuttle_id, []).append(pkt)
        _last_sample[shuttle_id]    = pkt
        _tx_rate_window[shuttle_id] = _tx_rate_window.get(shuttle_id, 0) + 1

        shuttle_pkts = len(_telemetry_buf[shuttle_id])
        total_pkts   = sum(len(v) for v in _telemetry_buf.values())

        # Mission-end via state transition is the normal path. We only check on
        # IDLE packets to avoid running the check 50× per second during MOVING.
        if state == STATE_IDLE:
            _maybe_flush_mission(shuttle_id, now)

        # Buffer-pressure flushes (mid-mission). These do not reset shuttle state —
        # the mission keeps accruing energy and the next batch lands in the next file.
        elif shuttle_pkts >= SHUTTLE_HARD_LIMIT:
            logger.warning(
                "[%s] per-shuttle HARD LIMIT (%d pkts) — mid-mission flush",
                shuttle_id, shuttle_pkts,
            )
            _flush(_telemetry_buf[shuttle_id], "mission")

        elif shuttle_pkts >= SHUTTLE_SOFT_LIMIT:
            logger.info(
                "[%s] per-shuttle soft limit (%d pkts) — proactive flush",
                shuttle_id, shuttle_pkts,
            )
            _flush(_telemetry_buf[shuttle_id], "mission")

        elif total_pkts >= GATEWAY_HARD_LIMIT:
            logger.error(
                "GATEWAY HARD LIMIT (%d total pkts across %d shuttles) — emergency flush all",
                total_pkts, len(_telemetry_buf),
            )
            for s_buf in list(_telemetry_buf.values()):
                _flush(s_buf, "mission")
            _telemetry_buf.clear()

    def error_received(self, exc: Exception) -> None:
        logger.error("Telemetry UDP socket error: %s", exc)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _status_log_task() -> None:
    """Once per STATUS_LOG_PERIOD_S, emit a per-shuttle summary line so operators
    can see live activity without drowning in 50 Hz per-packet logs."""
    while True:
        await asyncio.sleep(STATUS_LOG_PERIOD_S)
        for sid in list(_last_sample.keys()):
            sample = _last_sample.get(sid)
            if not sample:
                continue
            rate = _tx_rate_window.get(sid, 0) / STATUS_LOG_PERIOD_S
            _tx_rate_window[sid] = 0
            state_name = "MOVING" if sample["status.state"] == STATE_MOVING else "IDLE"

            temp = sample["env.temp_c"]
            hum  = sample["env.humidity_pct"]
            pres = sample["env.pressure_hpa"]
            energy = _mission_energy_j.get(sid, 0.0)

            logger.info(
                "[%s] %s %.1fHz seq=%d accel=(%.2f,%.2f,%.2f)g "
                "temp=%s°C hum=%s%% pres=%s hPa pwr=%.0fmW e=%.2fJ",
                sid, state_name, rate, sample["header.sequence_id"],
                sample["sensors.accel_x"], sample["sensors.accel_y"], sample["sensors.accel_z"],
                "n/a" if math.isnan(temp) else f"{temp:.1f}",
                "n/a" if math.isnan(hum)  else f"{hum:.0f}",
                "n/a" if math.isnan(pres) else f"{pres:.1f}",
                sample["energy.power_mw"], energy,
            )


async def _mission_end_watchdog() -> None:
    """Catch the case where a shuttle goes IDLE and then stops sending entirely.
    Without this loop, _maybe_flush_mission only runs on incoming IDLE packets — so
    a shuttle that powers off mid-IDLE would never flush its mission."""
    while True:
        await asyncio.sleep(5.0)
        now = time.monotonic()
        for sid in list(_last_moving_wall.keys()):
            _maybe_flush_mission(sid, now)


# ---------------------------------------------------------------------------
# Beacon broadcast — P2-1 zero-touch provisioning (unchanged from v1)
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
    # and skip the hardcoded JETSON_IP. Requires host networking on the Jetson.
    ip = GATEWAY_IP or _detect_local_ip()
    payload = f"PLUDOS-GW:{ip}".encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    loop = asyncio.get_running_loop()
    logger.info("[BEACON] announcing %s on UDP port %d every %.0f s", ip, BEACON_PORT, BEACON_INTERVAL_S)
    try:
        while True:
            try:
                await loop.run_in_executor(
                    None, sock.sendto, payload, ("255.255.255.255", BEACON_PORT)
                )
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
        "PLUDOS Data Engine (ADR-015) starting | TEST_MODE=%s | UDP=%d | "
        "shuttle soft=%d hard=%d | gateway hard=%d | mission_end_idle=%.0fs | dir=%s",
        TEST_MODE, TELEMETRY_PORT,
        SHUTTLE_SOFT_LIMIT, SHUTTLE_HARD_LIMIT, GATEWAY_HARD_LIMIT,
        MISSION_END_IDLE_S, BUFFER_DIR,
    )

    # Single UDP listener — replaces both the aiocoap /vib server and the
    # legacy NonCriticalProtocol on 5684 from earlier protocol versions.
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        TelemetryProtocol,
        local_addr=("0.0.0.0", TELEMETRY_PORT),
    )
    logger.info("Telemetry UDP listener bound on port %d", TELEMETRY_PORT)

    # Background tasks.
    asyncio.create_task(_status_log_task())
    asyncio.create_task(_mission_end_watchdog())
    asyncio.create_task(_broadcast_beacon())

    await loop.create_future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")
