"""
PLUDOS Edge Gateway: CoAP Data Engine
-------------------------------------
This script acts as the asynchronous UDP ingestion engine on the Jetson Orin Nano. 
It listens for unacknowledged CoAP packets from constrained STM32 microcontrollers.
It bases its disk-writing logic entirely on the physical RAM state reported by 
the STM32 itself, calculating cumulative energy consumption in real-time, 
and compiling chronological .parquet files for the Federated Learning worker.
"""

import asyncio
import json
import logging
import os
import pandas as pd
import aiocoap.resource as resource
import aiocoap

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 1. 12-FACTOR APP ENVIRONMENT RULES
# ==========================================
# TEST_MODE=1 maps the buffer to a local laptop folder.
# In production (Jetson), TEST_MODE defaults to False, utilizing the container's 
# /app/ram_buffer path which is mounted directly to the physical RAM (tmpfs).
TEST_MODE = os.getenv("TEST_MODE") == "1"
BUFFER_DIR = "./ram_buffer" if TEST_MODE else "/app/ram_buffer"
os.makedirs(BUFFER_DIR, exist_ok=True)

# ==========================================
# 2. DYNAMIC MEMORY CONSTRAINTS
# ==========================================
# STM_RAM_SOFT_LIMIT: The engine monitors the STM32's internal RAM. When the STM32 
# reports its memory is >= 85% full, the Jetson prepares to flush to the SD card.
STM_RAM_SOFT_LIMIT = 85.0 

# JETSON_HARD_LIMIT: A strict fallback constraint. If the STM32 crashes or network 
# packets are dropped, preventing the 'mission_active=False' signal, the Jetson will 
# forcefully flush at 500 packets to prevent its own OS from running out of memory.
JETSON_HARD_LIMIT = 500  

# The high-speed list holding telemetry data in active volatile memory
ram_buffer = []

# Dictionary tracking cumulative energy (mJ) per physical shuttle. 
# Enables multi-tenant tracking if several STM32s transmit simultaneously.
mission_energy_tracker = {}


class TelemetryResource(resource.Resource):
    """
    Asynchronous CoAP endpoint handling POST requests at '/telemetry'.
    """
    async def render_post(self, request):
        global ram_buffer
        global mission_energy_tracker
        
        try:
            # --- A. PACKET DECODING ---
            payload = json.loads(request.payload.decode('utf-8'))
            shuttle_id = payload.get("header", {}).get("shuttle_id", "unknown")
            packet_num = payload.get("header", {}).get("packet_num", 0)
            
            # Extract mission state and the STM32's current physical RAM capacity
            mission_active = payload.get("status", {}).get("mission_active", True)
            stm_ram_pct = payload.get("status", {}).get("ram_usage_pct", 0.0) 
            
            # Extract instantaneous power draw (milliWatts)
            power_mw = payload.get("energy", {}).get("power_mw", 0)
            
            # --- B. PHYSICS & ENERGY INTEGRATION ---
            # Energy = Power * Time. Assuming 50Hz sampling (0.02 seconds per packet).
            packet_energy_mj = power_mw * 0.02
            
            if shuttle_id not in mission_energy_tracker:
                mission_energy_tracker[shuttle_id] = 0.0
                
            # Keep a running total of the energy consumed by this specific shuttle
            mission_energy_tracker[shuttle_id] += packet_energy_mj
            
            logger.info(
                f"[{shuttle_id}] Pkt {packet_num} | "
                f"STM RAM: {stm_ram_pct}% | "
                f"Energy: {mission_energy_tracker[shuttle_id]:.2f} mJ"
            )
            
            # --- C. BUFFER INGESTION ---
            ram_buffer.append(payload)
            jetson_buffer_size = len(ram_buffer)
            
            # --- D. DYNAMIC STM-DRIVEN FLUSH LOGIC ---
            # Condition 1: STM32 RAM is highly utilized AND the mission has physically concluded.
            if stm_ram_pct >= STM_RAM_SOFT_LIMIT and not mission_active:
                grand_total = mission_energy_tracker[shuttle_id]
                logger.info(f"🏁 MISSION COMPLETE! STM RAM at {stm_ram_pct}%. Total Energy: {grand_total:.2f} mJ. Flushing buffer.")
                
                self.flush_to_storage()
                # Reset the tracker for the next mission cycle
                mission_energy_tracker[shuttle_id] = 0.0
                
            # Condition 2: Failsafe triggered to protect Jetson resources.
            elif jetson_buffer_size >= JETSON_HARD_LIMIT:
                logger.warning(f"CRITICAL: Jetson RAM buffer hit HARD LIMIT ({jetson_buffer_size} pkts). Forcing emergency flush!")
                self.flush_to_storage()
                mission_energy_tracker[shuttle_id] = 0.0
                
            return aiocoap.Message(code=aiocoap.CHANGED)
            
        except Exception as e:
            logger.error(f"Failed to process UDP packet: {e}")
            return aiocoap.Message(code=aiocoap.BAD_REQUEST)

    def flush_to_storage(self):
        """
        Executes the infrequent write operation. Reorders UDP packets chronologically 
        and compresses them into a Parquet file for the AI model to read.
        """
        global ram_buffer
        if not ram_buffer:
            return
            
        # Flatten JSON array into a Pandas DataFrame
        df = pd.json_normalize(ram_buffer)
        
        # RESOLVE NETWORK SCRAMBLING:
        # Mathematically sort the DataFrame by the physical shuttle ID, 
        # then by the STM32's assigned packet number to restore chronological order.
        df = df.sort_values(by=['header.shuttle_id', 'header.packet_num'])
        
        # Generate unique timestamped filename
        timestamp = int(asyncio.get_event_loop().time())
        file_path = os.path.join(BUFFER_DIR, f"mission_data_{timestamp}.parquet")
        
        # Save to disk using PyArrow engine for maximum compression
        df.to_parquet(file_path, engine='pyarrow')
        
        # Clear volatile RAM
        ram_buffer.clear()
        logger.info(f"SUCCESS: Reordered timeline saved to {file_path}. RAM buffer cleared.")

async def main():
    """Sets up the asynchronous CoAP server and binds the network ports."""
    logger.info(f"Starting PLUDOS Data Engine (Test Mode: {TEST_MODE}) on UDP Port 5683...")
    root = resource.Site()
    root.add_resource(['telemetry'], TelemetryResource())
    await aiocoap.Context.create_server_context(root, bind=('0.0.0.0', 5683))
    await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Data Engine shutting down.")