import flwr as fl
import xgboost as xgb
import numpy as np
import time
import logging
import os
import pandas as pd

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Function for real data logic and not test data
BUFFER_DIR = "./ram_buffer"

def load_buffered_data():
    logger.info("Scanning RAM buffer for telemetry Parquet files...")
    
    # 1. Find all parquet files in the buffer directory
    if not os.path.exists(BUFFER_DIR):
        logger.warning("No buffer directory found! Generating dummy data for failsafe.")
        return np.random.rand(100, 3), np.random.randint(0, 2, 100)
        
    files = [f for f in os.listdir(BUFFER_DIR) if f.endswith('.parquet')]
    
    if not files:
        logger.warning("No parquet files found in buffer! Generating dummy data for failsafe.")
        return np.random.rand(100, 3), np.random.randint(0, 2, 100)
    
    # 2. Load the most recent mission data
    # (In a real scenario, you might concatenate multiple files)
    latest_file = sorted(files)[-1]
    file_path = os.path.join(BUFFER_DIR, latest_file)
    logger.info(f"Loading data from {latest_file}...")
    
    df = pd.read_parquet(file_path)
    
    # 3. Prepare the Features (X) and Labels (y)
    # Our mock_stm32 creates columns like sensors.vib_x, sensors.vib_y, sensors.vib_z
    feature_cols = ['sensors.vib_x', 'sensors.vib_y', 'sensors.vib_z']
    X_train = df[feature_cols].values
    
    # Since this is an unsupervised/mock setup, we will create dummy labels 
    # based on a simple threshold (e.g., if vibration Z is high, mark as anomaly '1')
    y_train = (df['sensors.vib_z'] > 0.8).astype(int).values
    
    logger.info(f"Successfully loaded {len(X_train)} real samples from buffer.")
    return X_train, y_train
    
    '''

# ==========================================
# 1. THE DATA INGESTION (From RAM Buffer)
# ==========================================
def load_buffered_data():
    """
    In production, this reads the preprocessed .parquet files 
    saved by the data-engine in the Jetson's RAM (/app/buffer).
    """
    logger.info("Loading telemetry data from RAM buffer...")
    
    # For now, we generate synthetic tabular data to ensure the network plumbing works
    # 1000 samples, 10 features (representing FFT vibration data, temp, etc.)
    X_train = np.random.rand(1000, 10) 
    y_train = np.random.randint(0, 2, 1000) # 0 = Normal, 1 = Anomaly
    
    return X_train, y_train
'''

# ==========================================
# 2. THE FLOWER CLIENT (The AI Worker)
# ==========================================
class PLUDOSClient(fl.client.NumPyClient):
    def __init__(self):
        self.X_train, self.y_train = load_buffered_data()

    def get_parameters(self, config):
        # Return empty parameters for initialization
        return []

    def fit(self, parameters, config):
        # The central server sends the round number
        round_num = config.get("server_round", "Unknown")
        logger.info(f"--- STARTING FL ROUND {round_num} ---")
        
        # ---------------------------------------------------------
        # [ALUMET MARKER: START] 
        # In production, we send an API call/log to Alumet here
        # to start recording the 7W-25W power draw.
        # ---------------------------------------------------------
        start_time = time.time()

        logger.info("Igniting Jetson GPU for XGBoost training...")
        
        # Define XGBoost model. 
        # CRITICAL: device='cuda' tells it to use the Jetson's 1024 CUDA cores!
        # NOTE: device='cpu' for testing on laptop. Change to 'cuda' on Jetson!
        model = xgb.XGBClassifier(
            n_estimators=10, 
            tree_method='hist', 
            device='cpu' # This will fail on your laptop if you don't have an NVIDIA GPU, but it's perfect for the Jetson. 
                          # (If testing on a Mac/standard laptop now, change 'cuda' to 'cpu').
        )
        
        # Train the model on the local warehouse data
        model.fit(self.X_train, self.y_train)

        # ---------------------------------------------------------
        # [ALUMET MARKER: STOP] 
        # Stop recording. Alumet calculates: Watts * Time = Joules
        # ---------------------------------------------------------
        end_time = time.time()
        logger.info(f"FL Round {round_num} completed in {end_time - start_time:.2f} seconds.")

        # For this structural test, we return a dummy weight array to the server's FedAvg.
        # (XGBoost tree aggregation requires a specific byte-string setup we will add later).
        dummy_weights = [np.array([1.0])]
        
        return dummy_weights, len(self.X_train), {}

    def evaluate(self, parameters, config):
        # Evaluate local accuracy
        logger.info("Evaluating local model accuracy...")
        return 0.0, len(self.X_train), {"accuracy": 0.95}

# ==========================================
# 3. START THE CONNECTION
# ==========================================
def client_fn(context: fl.common.Context):
    """This function builds the ClientApp."""
    return PLUDOSClient().to_client()

# Define the App (Flower's CLI will hook into this variable)
app = fl.client.ClientApp(client_fn=client_fn)