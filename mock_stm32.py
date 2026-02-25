import asyncio
import json
import logging
import random
import aiocoap
from aiocoap import *

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def simulate_mission():
    logger.info("STM32 Simulator Waking Up... Starting LPBAM Mission.")
    
    # Create the CoAP client context
    protocol = await Context.create_client_context()

    # We send 55 packets to purposefully trigger the 50-packet flush limit in the data-engine
    for i in range(1, 56):
        # 1. Create fake 3D vibration and internal energy estimate data
        payload_dict = {
            "header": {"shuttle_id": "STM32-Alpha", "packet_num": i},
            "energy": {"power_mw": round(random.uniform(145.0, 155.0), 2)},
            "sensors": {
                "vib_x": random.random(),
                "vib_y": random.random(),
                "vib_z": random.random()
            }
        }
        
        payload_bytes = json.dumps(payload_dict).encode('utf-8')
        
        # 2. Create the CoAP POST request (Fire and Forget)
        request = Message(
            code=POST, 
            payload=payload_bytes, 
            uri="coap://127.0.0.1/telemetry",
            transport_tuning=aiocoap.Unreliable  # Modern syntax for "No ACK required"
        )
        
        try:
            # 3. Blast it over the network
            protocol.request(request)
            logger.info(f"Fired packet {i}/55 via UDP.")
        except Exception as e:
            logger.error(f"Failed to send: {e}")
            
        # Sleep for 20ms to simulate a 50Hz sensor sampling rate
        await asyncio.sleep(0.02)
        
    logger.info("Mission Complete. STM32 going back to deep sleep.")

if __name__ == "__main__":
    try:
        asyncio.run(simulate_mission())
    except KeyboardInterrupt:
        logger.info("Simulation aborted.")