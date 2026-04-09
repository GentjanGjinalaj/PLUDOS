"""
PLUDOS Hardware Simulator (STM32)
---------------------------------
This script simulates the physical STM32 microcontroller executing a vibration 
analysis mission using LPBAM (Low Power Background Autonomous Mode).
It dynamically calculates its own internal SRAM usage and transmits this 
percentage alongside the telemetry, shifting the memory-management logic 
away from the Jetson and onto the physical edge device.
"""

import asyncio
import json
import logging
import os
import random
import aiocoap
from aiocoap import *

# Configure standard terminal logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COAP_SERVER = os.getenv('COAP_SERVER', '127.0.0.1')
COAP_PORT = int(os.getenv('COAP_PORT', '5683'))
COAP_PATH = os.getenv('COAP_PATH', 'telemetry')

async def simulate_mission():
    logger.info("STM32 Simulator Waking Up... Starting LPBAM Mission.")
    
    # Establish the CoAP client network protocol
    protocol = await Context.create_client_context()

    # Define a mission length (e.g., 46 sensor readings)
    total_packets = 46
    
    for i in range(1, total_packets + 1):
        # The mission remains active until the very last packet
        is_active = True if i < total_packets else False
        
        # --- PHYSICAL RAM SIMULATION ---
        # We simulate the STM32's internal SRAM buffer slowly filling up as it 
        # collects high-frequency vibration data. It scales from ~2% up to 100%.
        stm_ram_pct = min((i / total_packets) * 100.0, 100.0)
        
        # --- PAYLOAD CONSTRUCTION ---
        payload_dict = {
            "header": {
                "shuttle_id": "STM32-Alpha", 
                "packet_num": i          # Essential for Jetson to chronologically reorder UDP packets
            },
            "status": {
                "mission_active": is_active,
                "ram_usage_pct": round(stm_ram_pct, 1)  # The STM32 dictates its own memory state
            },
            "energy": {
                "power_mw": round(random.uniform(145.0, 155.0), 2) # Instantaneous power draw
            },
            "sensors": {
                "vib_x": random.random(),
                "vib_y": random.random(),
                "vib_z": random.random()
            }
        }
        
        # Encode the dictionary into a UTF-8 JSON byte string
        payload_bytes = json.dumps(payload_dict).encode('utf-8')
        
        # --- NETWORK TRANSPORT ---
        uri = f"coap://{COAP_SERVER}:{COAP_PORT}/{COAP_PATH}"
        request = Message(
            code=POST,
            payload=payload_bytes,
            uri=uri
        )

        # Critical: vibration + accelerometer + energy must be confirmed and retried.
        send_success = False
        for attempt in range(1, 5):
            try:
                response = await protocol.request(request).response
                logger.info(f"Packet {i} ACKed with code {response.code} (attempt {attempt})")
                send_success = True
                break
            except Exception as e:
                logger.warning(f"CoAP send retry {attempt}/4 for packet {i} failed: {e}")
                await asyncio.sleep(0.1)

        if not send_success:
            logger.error(f"Failed to send packet {i} after 4 attempts. Dropping packet.")

        if not is_active and send_success:
            logger.info(f"Sent final packet {i}. STM32 RAM at {stm_ram_pct:.1f}%. Mission Stopped.")
            
        # Sleep for 20ms to mimic a 50Hz hardware sensor sampling rate
        await asyncio.sleep(0.02)
        
    logger.info("STM32 Mission Complete. Microcontroller returning to deep sleep.")

if __name__ == "__main__":
    try:
        asyncio.run(simulate_mission())
    except KeyboardInterrupt:
        logger.info("Simulation forcefully aborted.")