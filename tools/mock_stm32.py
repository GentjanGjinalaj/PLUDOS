"""
PLUDOS Hardware Simulator (STM32 — ADR-015 v2)
----------------------------------------------
Spawns N parallel shuttles, each cycling IDLE → MOVING → long IDLE forever,
streaming 28-byte PludosTelemetry packets over raw UDP to one Jetson
gateway. Matches `docs/wire_protocol.md §1` and `client/data-engine.py`.

Typical uses:
  # Single-shuttle smoke test against a local data-engine.
  python tools/mock_stm32.py

  # 6-shuttle stress test (one process emits all six).
  MOCK_SHUTTLES=6 python tools/mock_stm32.py

  # Point at a remote Jetson rather than localhost.
  TELEMETRY_HOST=192.168.1.50 MOCK_SHUTTLES=2 python tools/mock_stm32.py

Environment variables:
  TELEMETRY_HOST    — gateway IP                            (default: 127.0.0.1)
  TELEMETRY_PORT    — gateway UDP port                      (default: 5683)
  MOCK_SHUTTLES     — number of parallel shuttles           (default: 1)
  FIRST_SHUTTLE_ID  — starting shuttle ID (1-based)         (default: 1)
  MISSION_S         — MOVING phase duration in seconds      (default: 30)
  IDLE_S            — short IDLE phase before each MOVING   (default: 5)
  POST_MISSION_IDLE_S — long IDLE after MOVING (>= 30 triggers
                      gateway mission-end flush)            (default: 35)
"""

import asyncio
import logging
import os
import random
import socket
import struct
import time

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

# Wire format — must match wire_protocol.md §1 and data-engine.py exactly.
TELEMETRY_FMT  = "<BHIBfffff"
TELEMETRY_SIZE = struct.calcsize(TELEMETRY_FMT)
assert TELEMETRY_SIZE == 28, f"PludosTelemetry must be 28 B (got {TELEMETRY_SIZE})"

STATE_IDLE   = 0
STATE_MOVING = 1

# Internal sample cadence per state (mirrors STM32 firmware).
TX_PERIOD_IDLE_S   = 1.0    # 1 Hz in IDLE
TX_PERIOD_MOVING_S = 0.02   # 50 Hz in MOVING


# ---------------------------------------------------------------------------
# Packet builder
# ---------------------------------------------------------------------------

def _pack(shuttle_id: int, seq: int, tick_ms: int, state: int,
          ax: float, ay: float, az: float, temp: float, hum: float) -> bytes:
    # 28-byte little-endian PludosTelemetry matching `<BHIBfffff`.
    return struct.pack(
        TELEMETRY_FMT,
        shuttle_id & 0xFF,
        seq & 0xFFFF,
        tick_ms & 0xFFFFFFFF,
        state & 0xFF,
        ax, ay, az, temp, hum,
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

        ax = round(random.uniform(*ax_range), 4)
        ay = round(random.uniform(*ax_range), 4)
        az = round(az_dc + random.uniform(-az_jitter, az_jitter), 4)
        # Slow-changing env, refreshed each sample — matches HTS221 1 Hz ODR scale.
        temp = round(random.uniform(20.0, 25.0), 1)
        hum  = round(random.uniform(40.0, 60.0), 1)

        sock.sendto(
            _pack(shuttle_id, seq_ref[0], tick_ms, state, ax, ay, az, temp, hum),
            (TELEMETRY_HOST, TELEMETRY_PORT),
        )
        await asyncio.sleep(period)


async def simulate_shuttle(shuttle_id: int) -> None:
    # One shuttle loops: short IDLE → MOVING (mission) → long IDLE (flush trigger).
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq_ref = [0]
    boot_ms = int(time.monotonic() * 1000)

    logger.info("[shuttle-%d] starting → %s:%d", shuttle_id, TELEMETRY_HOST, TELEMETRY_PORT)

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

            logger.info("[shuttle-%d] cycle %d: post-IDLE %.0fs (triggers mission flush)",
                        shuttle_id, cycle, POST_MISSION_IDLE_S)
            await _send_phase(sock, shuttle_id, STATE_IDLE, POST_MISSION_IDLE_S,
                              seq_ref, boot_ms,
                              ax_range=(-0.01, 0.01), az_dc=1.0, az_jitter=0.01)
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info(
        "Mock STM32 (ADR-015 v2) | target=%s:%d | %d shuttle(s) starting at ID %d "
        "| mission=%.0fs idle=%.0fs post-idle=%.0fs",
        TELEMETRY_HOST, TELEMETRY_PORT, MOCK_SHUTTLES, FIRST_SHUTTLE_ID,
        MISSION_S, IDLE_S, POST_MISSION_IDLE_S,
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
