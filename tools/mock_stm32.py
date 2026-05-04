"""
PLUDOS Hardware Simulator (STM32)
---------------------------------
Simulates one shuttle executing a vibration mission over CoAP CON and
sending NonCritical environmental data over raw UDP.

Wire format: wire_protocol.md §1 (CriticalPayload) and §2 (NonCriticalPayload).
CoAP transport uses aiocoap; NC uses a plain UDP socket.

Environment variables:
  COAP_SERVER  — gateway IP         (default: 127.0.0.1)
  COAP_PORT    — gateway CoAP port  (default: 5683)
  NC_PORT      — gateway NC port    (default: 5684)
  SHUTTLE_ID   — shuttle identifier (default: STM32-Alpha)
"""

import asyncio
import logging
import math
import os
import random
import socket
import struct
import time

import aiocoap
from aiocoap import Message, POST, Context

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mock-stm32")

COAP_SERVER = os.getenv("COAP_SERVER", "127.0.0.1")
COAP_PORT   = int(os.getenv("COAP_PORT",  "5683"))
NC_PORT     = int(os.getenv("NC_PORT",    "5684"))
SHUTTLE_ID  = os.getenv("SHUTTLE_ID", "STM32-Alpha").encode("utf-8")[:11].ljust(12, b"\x00")

# Wire format §1: <12s H I B f f f f f>  (39 bytes)
# Fields: shuttle_id, sequence_id, tick_ms, mission_active,
#         ram_usage_pct, accel_x, accel_y, accel_z, power_mw
CRITICAL_FMT  = "<12sHIBfffff"

# Wire format §2: <12s H I f f f>  (30 bytes)
# Fields: shuttle_id, sequence_id, tick_ms, temp_c, humidity_pct, pressure_hpa
NC_FMT = "<12sHIfff"


def _critical_payload(seq: int, tick_ms: int, mission_active: bool,
                       ram_pct: float, ax: float, ay: float, az: float,
                       power_mw: float) -> bytes:
    """Pack a 39-byte CriticalPayload matching wire_protocol.md §1."""
    return struct.pack(
        CRITICAL_FMT,
        SHUTTLE_ID,
        seq,
        tick_ms & 0xFFFFFFFF,
        int(mission_active),
        ram_pct,
        ax,
        ay,
        az,
        power_mw,
    )


def _nc_payload(seq: int, tick_ms: int,
                temp_c: float, hum_pct: float, pres_hpa: float) -> bytes:
    """Pack a 30-byte NonCriticalPayload matching wire_protocol.md §2."""
    return struct.pack(
        NC_FMT,
        SHUTTLE_ID,
        seq,
        tick_ms & 0xFFFFFFFF,
        temp_c,
        hum_pct,
        pres_hpa,
    )


def _send_nc(seq: int, tick_ms: int) -> None:
    """Fire-and-forget UDP NonCritical packet on JETSON_NC_PORT."""
    temp_c     = round(random.uniform(18.0, 30.0), 1)
    hum_pct    = round(random.uniform(35.0, 65.0), 1)
    pres_hpa   = round(random.uniform(1005.0, 1025.0), 1)
    data       = _nc_payload(seq, tick_ms, temp_c, hum_pct, pres_hpa)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(data, (COAP_SERVER, NC_PORT))
    logger.info("[NC] seq=%d temp=%.1f°C hum=%.0f%% pres=%.1f hPa",
                seq, temp_c, hum_pct, pres_hpa)


async def simulate_mission() -> None:
    logger.info("Mock STM32 starting | server=%s CoAP=%d NC=%d",
                COAP_SERVER, COAP_PORT, NC_PORT)

    protocol    = await Context.create_client_context()
    total_pkts  = 60
    start_boot  = int(time.time() * 1000)

    for i in range(1, total_pkts + 1):
        mission_active = i < total_pkts
        tick_ms        = int(time.time() * 1000) - start_boot
        ram_pct        = min((i / total_pkts) * 100.0, 100.0)

        # Simulate realistic accelerometer values: ~1g gravity + small vibration
        ax = round(0.0  + random.uniform(-0.15, 0.15), 4)
        ay = round(0.0  + random.uniform(-0.15, 0.15), 4)
        az = round(1.0  + random.uniform(-0.05, 0.05), 4)

        # State-based power estimate matching POWER_EstimateMilliwatts() in firmware:
        # MCU(15) + sensors(2) + WiFi TX(200) mA × 3.3V
        power_mw = (15.0 + 2.0 + 200.0) * 3.3

        payload_bytes = _critical_payload(i, tick_ms, mission_active,
                                          ram_pct, ax, ay, az, power_mw)

        uri     = f"coap://{COAP_SERVER}:{COAP_PORT}/vib"
        request = Message(code=POST, payload=payload_bytes, uri=uri)

        sent = False
        for attempt in range(1, 5):
            try:
                response = await protocol.request(request).response
                logger.info("[CoAP] seq=%d ACK=%s ram=%.0f%% pow=%.0fmW (attempt %d)",
                            i, response.code, ram_pct, power_mw, attempt)
                sent = True
                break
            except Exception as exc:
                logger.warning("[CoAP] seq=%d attempt %d/4 failed: %s", i, attempt, exc)
                await asyncio.sleep(0.1 * (2 ** (attempt - 1)))

        if not sent:
            logger.error("[CoAP] seq=%d dropped after 4 attempts", i)

        if not mission_active:
            logger.info("[MISSION] Final packet sent — mission_active=0. Entering IDLE.")
            _send_nc(i + 1, tick_ms)

        await asyncio.sleep(0.02)  # 50 Hz sensor rate

    logger.info("Simulation complete.")


if __name__ == "__main__":
    try:
        asyncio.run(simulate_mission())
    except KeyboardInterrupt:
        logger.info("Simulation aborted.")
