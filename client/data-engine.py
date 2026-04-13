"""
PLUDOS Edge Gateway: CoAP Data Engine
-------------------------------------
This script acts as the asynchronous CoAP ingestion engine. It listens for
confirmable STM32 CoAP packets, orders them chronologically, and writes
.parquet files when mission state or buffer limits demand it.
"""

import asyncio
import logging
import os
import socket
import struct
import time

import aiocoap
import aiocoap.resource as resource
import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEST_MODE = os.getenv("TEST_MODE") == "1"
DEFAULT_BUFFER_DIR = "./ram_buffer"
CONTAINER_BUFFER_DIR = "/app/ram_buffer"
BUFFER_DIR = DEFAULT_BUFFER_DIR if TEST_MODE or not os.path.isdir("/app") else CONTAINER_BUFFER_DIR
try:
    os.makedirs(BUFFER_DIR, exist_ok=True)
except PermissionError:
    logger.warning(
        "No permission for %s, falling back to %s",
        BUFFER_DIR,
        DEFAULT_BUFFER_DIR,
    )
    BUFFER_DIR = DEFAULT_BUFFER_DIR
    os.makedirs(BUFFER_DIR, exist_ok=True)

JETSON_HARD_LIMIT = 500
JETSON_SOFT_LIMIT_PCT = 0.8

ram_buffer = []
mission_energy_tracker = {}
mission_ntp_offsets = {}

PACKET_INTERVAL_S = 0.02
PAYLOAD_FORMAT = '<12sHIBfffff'
EXPECTED_PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FORMAT)


class TelemetryResource(resource.Resource):
    """Asynchronous CoAP endpoint handling POST requests at '/telemetry'."""

    async def render_post(self, request):
        global ram_buffer
        global mission_energy_tracker
        global mission_ntp_offsets

        try:
            path = '/'.join(request.opt.uri_path) if hasattr(request, 'opt') else 'unknown'
            logger.info(f"Received CoAP path: {path}, payload size: {len(request.payload)}")

            if len(request.payload) != EXPECTED_PAYLOAD_SIZE:
                logger.error(
                    f"Invalid payload size: {len(request.payload)} bytes (expected {EXPECTED_PAYLOAD_SIZE})"
                )
                return aiocoap.Message(code=aiocoap.BAD_REQUEST)

            unpacked = struct.unpack(PAYLOAD_FORMAT, request.payload)
            shuttle_id = unpacked[0].decode('utf-8').rstrip('\x00')
            sequence_id = unpacked[1]
            tick_ms = unpacked[2]
            mission_active = bool(unpacked[3])
            stm_ram_pct = unpacked[4]
            accel_x = unpacked[5]
            accel_y = unpacked[6]
            accel_z = unpacked[7]
            power_mw = unpacked[8]

            logger.info(f"Parsed shuttle_id={shuttle_id}, sequence_id={sequence_id}")

            receipt_time_ms = int(time.time() * 1000)
            if shuttle_id not in mission_ntp_offsets:
                mission_ntp_offsets[shuttle_id] = receipt_time_ms - tick_ms
                logger.info(
                    f"NTP offset established for {shuttle_id}: {mission_ntp_offsets[shuttle_id]} ms"
                )
            absolute_timestamp_ms = tick_ms + mission_ntp_offsets[shuttle_id]

            payload = {
                "header.shuttle_id": shuttle_id,
                "header.sequence_id": sequence_id,
                "status.mission_active": mission_active,
                "status.ram_usage_pct": stm_ram_pct,
                "energy.power_mw": power_mw,
                "sensors.accel_x": accel_x,
                "sensors.accel_y": accel_y,
                "sensors.accel_z": accel_z,
                "timestamp_ms": absolute_timestamp_ms,
            }

            packet_energy_mj = power_mw * PACKET_INTERVAL_S
            mission_energy_tracker.setdefault(shuttle_id, 0.0)
            mission_energy_tracker[shuttle_id] += packet_energy_mj

            logger.info(
                f"[{shuttle_id}] Pkt {sequence_id} | STM RAM: {stm_ram_pct}% | "
                f"Energy: {mission_energy_tracker[shuttle_id]:.2f} mJ"
            )

            ram_buffer.append(payload)
            jetson_buffer_size = len(ram_buffer)

            if not mission_active:
                grand_total = mission_energy_tracker.get(shuttle_id, 0.0)
                logger.info(
                    f"🏁 MISSION COMPLETE (STM32 signaled). Total Energy: {grand_total:.2f} mJ. Flushing buffer."
                )
                self.flush_to_storage()
                mission_energy_tracker[shuttle_id] = 0.0

            elif jetson_buffer_size >= int(JETSON_HARD_LIMIT * JETSON_SOFT_LIMIT_PCT):
                logger.warning(
                    f"WARN: Jetson RAM buffer reached soft limit ({jetson_buffer_size} pkts). Flushing to protect performance."
                )
                self.flush_to_storage()
                mission_energy_tracker[shuttle_id] = 0.0

            elif jetson_buffer_size >= JETSON_HARD_LIMIT:
                logger.warning(
                    f"CRITICAL: Jetson RAM buffer hit HARD LIMIT ({jetson_buffer_size} pkts). Forcing emergency flush!"
                )
                self.flush_to_storage()
                mission_energy_tracker[shuttle_id] = 0.0

            return aiocoap.Message(code=aiocoap.CHANGED)

        except Exception as e:
            logger.error(f"Failed to process CoAP packet: {e}")
            return aiocoap.Message(code=aiocoap.BAD_REQUEST)

    def flush_to_storage(self):
        if not ram_buffer:
            return

        df = pd.json_normalize(ram_buffer)
        df = df.sort_values(by=["header.shuttle_id", "header.sequence_id"])
        timestamp = int(time.time())
        file_path = os.path.join(BUFFER_DIR, f"mission_data_{timestamp}.parquet")
        temp_path = file_path + ".tmp"
        df.to_parquet(temp_path, engine="pyarrow")
        os.replace(temp_path, file_path)
        ram_buffer.clear()
        logger.info(f"SUCCESS: Reordered timeline saved to {file_path}. RAM buffer cleared.")


async def broadcast_beacon():
    """Beacon disabled for simplified testing."""
    while True:
        await asyncio.sleep(60)


async def main():
    logger.info(f"Starting PLUDOS Data Engine (Test Mode: {TEST_MODE}) on UDP Port 5683...")
    asyncio.create_task(broadcast_beacon())

    root = resource.Site()
    root.add_resource([], TelemetryResource())  # Catch-all for any path
    root.add_resource(["telemetry"], TelemetryResource())
    root.add_resource(["vib"], TelemetryResource())
    root.add_resource(["vibration"], TelemetryResource())

    await aiocoap.Context.create_server_context(root, bind=("0.0.0.0", 5683))
    await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")
