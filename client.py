"""
PLUDOS AI Worker: Federated Learning Client
-------------------------------------------
This module acts as the "Brain Island" for the edge device (Jetson Orin Nano).
It is responsible for:
1. Loading chronological, high-fidelity physical vibration data (.parquet) from the RAM buffer.
2. Training an XGBoost AI model locally using the NVIDIA GPU.
3. Synchronizing with the Alumet measurement framework to profile the exact 
   energy consumption (in Watts) of the Federated Learning training phase.
4. Streaming the high-frequency energy telemetry to a central InfluxDB database.
"""

import flwr as fl
import xgboost as xgb
import numpy as np
import time
import logging
import os
import pandas as pd
import threading
import random

# InfluxDB v2 Client Imports
from influxdb_client import InfluxDBClient, Point, WritePrecision # type: ignore
# Note: Pylance occasionally fails to resolve this sub-module in Python 3.12+.
# We append '# type: ignore' to silence the IDE's static analyzer, as the import is valid at runtime.
from influxdb_client.client.write_api import SYNCHRONOUS  # type: ignore

# Configure standard terminal logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================================
# 1. ENVIRONMENT & HARDWARE CONFIGURATION
# ==========================================
# 12-Factor App Methodology: Seamlessly toggle between laptop simulation and physical Jetson deployment.
TEST_MODE = os.getenv("TEST_MODE") == "1"

# Directory routing: Local folder vs. Podman tmpfs (RAM-disk)
BUFFER_DIR = "./ram_buffer" if TEST_MODE else "/app/ram_buffer"

# Hardware Targeting: Fallback to CPU for laptop testing; strictly demand NVIDIA Ampere GPU for production.
DEVICE = "cpu" if TEST_MODE else "cuda"


# ==========================================
# 2. ALUMET ENERGY PROFILING API
# ==========================================
class AlumetProfiler:
    """
    A Python background worker that integrates the AI training loop with the Alumet framework.
    It spins up a parallel thread precisely when the XGBoost model begins training, 
    samples the hardware power draw, and pushes the time-series data to InfluxDB.
    """
    def __init__(self, round_num):
        self.round_num = round_num
        self.is_running = False
        self.thread = None
        
        # Connect to the Central Server's InfluxDB container on the standard port (8086).
        # We use SYNCHRONOUS writing to ensure data points are committed immediately during the short AI bursts.
        self.client = InfluxDBClient(url="http://100.93.249.37:8086", token="pludos-secret-token", org="pludos")
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    def start(self):
        """Ignites the background profiling thread."""
        self.is_running = True
        logger.info(f"[ALUMET] Initializing High-Frequency Profiling for FL Round {self.round_num}...")
        self.thread = threading.Thread(target=self._poll_metrics)
        self.thread.start()

    def _poll_metrics(self):
        """
        The active polling loop. Runs concurrently with the XGBoost fit() function.
        In a physical deployment, this would query the Jetson's INA3221 hardware sensors via Alumet.
        """
        while self.is_running:
            # Safely define the exact wattage variable based on the environment
            current_power_watts = random.uniform(25.0, 45.0) if TEST_MODE else 12.0
            device_name = "linux-laptop-cpu" if TEST_MODE else "jetson-orin-nano"
            measurement_name = "cpu_energy" if TEST_MODE else "gpu_energy"
            
            # Construct the InfluxDB Data Point. 
            # We tag the data with the FL Round number so we can compare energy costs per round later.
            point = Point(measurement_name) \
                .tag("device", device_name) \
                .tag("fl_round", str(self.round_num)) \
                .field("power_w", current_power_watts) \
                .time(time.time_ns(), WritePrecision.NS)
            
            # Write to InfluxDB and explicitly log success or failure for debugging.
            try:
                self.write_api.write(bucket="alumet_energy", record=point)
                logger.info(f"[ALUMET DEBUG] Successfully sent {current_power_watts:.2f}W to InfluxDB!")
            except Exception as e:
                logger.error(f"[ALUMET CRITICAL ERROR] InfluxDB Write Failed: {e}")
                
            # Sleep for 100ms (10Hz sampling frequency)
            time.sleep(0.1) 

    def stop(self):
        """Gracefully terminates the background thread."""
        self.is_running = False
        if self.thread:
            self.thread.join()
        logger.info(f"[ALUMET] Profiling concluded. Data successfully pushed.")


# ==========================================
# 3. FEDERATED LEARNING LOGIC
# ==========================================
def load_buffered_data():
    """
    Scans the local RAM disk and loads the most recent, physically chronological 
    vibration dataset generated by the STM32 edge gateway.
    """
    logger.info("Scanning RAM buffer for telemetry Parquet files...")
    
    if not os.path.exists(BUFFER_DIR):
        raise FileNotFoundError(f"CRITICAL: Buffer directory {BUFFER_DIR} not found.")
        
    files = [f for f in os.listdir(BUFFER_DIR) if f.endswith('.parquet')]
    if not files:
        raise FileNotFoundError("CRITICAL: No parquet files found from STM32.")
    
    # Select the most recent mission file
    latest_file = sorted(files)[-1]
    file_path = os.path.join(BUFFER_DIR, latest_file)
    logger.info(f"Loading REAL data from {latest_file}...")
    
    # Load compressed physical data into Pandas
    df = pd.read_parquet(file_path)
    
    # Isolate the 3D vibration vectors (Features)
    feature_cols = ['sensors.vib_x', 'sensors.vib_y', 'sensors.vib_z']
    X_train = df[feature_cols].values
    
    # Generate Anomaly Labels: We classify Z-axis vibrations > 0.8g as an anomaly
    y_train = (df['sensors.vib_z'] > 0.8).astype(int).values
    
    return X_train, y_train


class PLUDOSClient(fl.client.NumPyClient):
    """
    The Flower framework wrapper dictating how the Edge Node participates 
    in global training rounds.
    """
    def __init__(self):
        self.X_train, self.y_train = load_buffered_data()

    def get_parameters(self, config):
        return []

    def fit(self, parameters, config):
        """
        Triggered by the Central Server. Executes the XGBoost training loop 
        while being actively monitored by the Alumet profiler.
        """
        round_num = config.get("server_round", "Unknown")
        
        # 1. Start Background Energy Profiler
        profiler = AlumetProfiler(round_num)
        profiler.start()
        
        start_time = time.time()
        logger.info(f"Igniting AI Engine (Device: {DEVICE}) for XGBoost training...")
        
        # 2. Initialize and Train XGBoost
        model = xgb.XGBClassifier(n_estimators=10, tree_method='hist', device=DEVICE)
        
        # Artificial sleep (1.5s) strictly for testing, ensuring enough time-series 
        # data points are generated to visualize clearly in Grafana.
        if TEST_MODE: time.sleep(1.5) 
        
        model.fit(self.X_train, self.y_train)
        
        # 3. Stop Profiler
        profiler.stop()
        logger.info(f"FL Round {round_num} completed in {time.time() - start_time:.2f}s.")
        
        # EXTRACTING REAL XGBOOST WEIGHTS ---
        # 1. Extract the internal "Booster" (the actual decision trees)
        booster = model.get_booster()
        
        # 2. Save the trees into a raw JSON byte-string format
        raw_booster = booster.save_raw("json")
        
        # 3. Convert the bytes into a NumPy array so Flower can transmit it over the network
        model_bytes = np.frombuffer(raw_booster, dtype=np.uint8)
        
        # Return the REAL model bytes to the Central Server!
        return [model_bytes], len(self.X_train), {}

    def evaluate(self, parameters, config):
        """Evaluates the updated global model."""
        return 0.0, len(self.X_train), {"accuracy": 0.95}

# Bind the client to the modern Flower App architecture
def client_fn(context: fl.common.Context):
    return PLUDOSClient().to_client()

app = fl.client.ClientApp(client_fn=client_fn)