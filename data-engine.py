import asyncio
import json
import logging
import os
import pandas as pd
import aiocoap.resource as resource
import aiocoap

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# This is the path where Podman will mount the RAM disk (tmpfs) on the Jetson.
# For local testing, it will just create a folder on your laptop.
BUFFER_DIR = "./ram_buffer"
os.makedirs(BUFFER_DIR, exist_ok=True)

# Our high-speed in-memory list
ram_buffer = []
BATCH_LIMIT = 50  # We will flush to disk after 50 packets to save SD card life

class TelemetryResource(resource.Resource):
    """
    This CoAP resource listens for NON-confirmable UDP packets from the STM32.
    """
    async def render_post(self, request):
        global ram_buffer
        
        try:
            # 1. Decode the UDP packet
            payload = json.loads(request.payload.decode('utf-8'))
            shuttle_id = payload.get("header", {}).get("shuttle_id", "unknown")
            power_mw = payload.get("energy", {}).get("power_mw", 0)
            
            logger.info(f"Received UDP packet from {shuttle_id} | Power: {power_mw}mW")
            
            # 2. Append to our fast RAM list
            ram_buffer.append(payload)
            
            # 3. Check if we need to execute the "Infrequent Write"
            if len(ram_buffer) >= BATCH_LIMIT:
                self.flush_to_storage()
                
            # CoAP standard response (STM32 won't wait for this due to mtype=NON)
            return aiocoap.Message(code=aiocoap.CHANGED)
            
        except Exception as e:
            logger.error(f"Failed to process packet: {e}")
            return aiocoap.Message(code=aiocoap.BAD_REQUEST)

    def flush_to_storage(self):
        """Moves data from active RAM to a permanent .parquet file on the SD card."""
        global ram_buffer
        logger.info(f"Triggering infrequent write... Saving {len(ram_buffer)} records to storage.")
        
        # Convert JSON list to a Pandas DataFrame
        df = pd.json_normalize(ram_buffer)
        
        # Save as a highly compressed Parquet file (excellent for ML training)
        file_path = os.path.join(BUFFER_DIR, f"mission_data_{int(asyncio.get_event_loop().time())}.parquet")
        df.to_parquet(file_path)
        
        # Clear the RAM buffer for the next mission
        ram_buffer.clear()
        logger.info(f"Data safely written to {file_path}. RAM buffer cleared.")

async def main():
    logger.info("Starting PLUDOS CoAP Data Engine on UDP Port 5683...")
    
    # Setup the CoAP Server routing
    root = resource.Site()
    root.add_resource(['telemetry'], TelemetryResource())
    
    # Bind to all network interfaces on the standard CoAP port
    await aiocoap.Context.create_server_context(root, bind=('0.0.0.0', 5683))
    
    # Keep the server running forever
    await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")