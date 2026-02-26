# PLUDOS Operations Guide: How to Run the Simulation

Until the physical Jetson and STM32 hardware arrives, the entire PLUDOS system can be simulated locally on a laptop using two distinct testing phases.

## Prerequisites
1. Open a terminal and navigate to the `PLUDOS` folder.
2. Activate the Python virtual environment: `source pludos_venv/bin/activate` (Mac/Linux) or `pludos_venv\Scripts\activate` (Windows).

---

## Phase 1: Test the Data Pipeline (Sensory Island)
We need to generate real `.parquet` files for the AI to train on.

1. **Start the Data Engine:** In your active terminal, run:
   ```bash
   python data-engine.py
   (It will listen on port 5683).
2. Fire the Mock Data: Open a second terminal, activate the venv, and run:
    ```bash
    python mock_stm32.py
3. Verify: Check the ram_buffer/ directory. You should see a newly generated mission_data_XXXXX.parquet file.

## Phase 2: Test the Federated Learning (Brain Island)
Now that we have data, we can run the AI training loop.

1. Ensure the ram_buffer/ contains at least one .parquet file from Phase 1.

2. In a single terminal (with the venv active), run the modern Flower engine:
    ```bash
    flwr run .
3. Verify: You will see the Ray engine spin up, connect the Server and Client Apps, load the 50 real data samples from your buffer, execute 3 rounds of XGBoost training, and shut down cleanly.

## Phase 3: Start the Central Analytics Dashboard
To view the energy metrics (future Alumet integration):

1. Run the Podman compose file (requires rootless permissions or sudo):
    ```bash
    podman compose up -d
2. Open a browser and navigate to http://localhost:3000 to access Grafana.