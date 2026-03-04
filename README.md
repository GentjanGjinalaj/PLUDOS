# PLUDOS: Energy-Aware Federated Learning System

This repository contains the core software architecture for a PhD research project focusing on Energy-Aware Federated Learning (HE-AFL) at the extreme edge. 

The system bridges ultra-low-power microcontrollers (STM32) with edge AI accelerators (Jetson Orin Nano) and a central aggregation server, using CoAP (UDP) for lightweight telemetry and the Flower framework for AI orchestration.



## 🏗️ The System Architecture & Logic Flow

The PLUDOS system strictly separates testing logic from production deployment using Environment Variables (`TEST_MODE=1`). The architecture is divided into two main "Islands":

### 1. The Edge Island (Jetson Orin Nano & STM32)
* **`mock_stm32.py`**: A simulator for the physical STM32 microcontroller. It mimics "Event-Driven Telemetry" by blasting UDP packets containing 3D vibration data, power metrics, and a crucial `mission_active` status flag.
* **`data-engine.py`**: The Jetson's Gatekeeper. It listens on UDP Port 5683 and features **Smart Buffering**. It holds packets in RAM until a soft limit (e.g., 80% capacity) is reached, but *waits* for the STM32 to send a `mission_active: false` signal before flushing. It then automatically reorders the scrambled UDP packets chronologically and saves them as a highly compressed `.parquet` file.
* **`client.py`**: The AI Worker (`ClientApp`). It strictly enforces production hardware standards (demanding NVIDIA `cuda` and real `.parquet` files). It scans the RAM buffer, loads the real vibration data, and uses the Jetson's GPU to train an XGBoost model.
* **`Dockerfile` & `jetson-compose.yaml`**: The deployment blueprints. These files package `data-engine.py` and `client.py` into secure Podman containers with a virtual RAM disk (`tmpfs`) to protect the physical SD card from wear.

### 2. The Central Server Island (Your Laptop/Cloud)
* **`server.py`**: The AI Orchestrator (`ServerApp`). It coordinates Jetson clients and aggregates their learned weights using the `FedAvg` strategy.
* **`pyproject.toml`**: The modern Flower configuration file, linking Server and Client Apps for local Ray-engine simulations.
* **`compose.yaml`**: The analytics infrastructure spinning up InfluxDB and Grafana for future Alumet energy monitoring.

## 🔄 The Sequence of Operations
1. STM32 senses vibration -> Sends CoAP UDP packet with Packet Number and Power data.
2. Jetson `data-engine` buffers packets in RAM -> Sorts them -> Saves to `.parquet`.
3. Central Server starts an FL Round -> Pings Jetson `client.py`.
4. Jetson `client.py` reads the `.parquet` file -> Trains XGBoost on GPU -> Sends weights back to Server.