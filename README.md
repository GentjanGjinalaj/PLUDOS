# PLUDOS: Energy-Aware Federated Learning System

This repository contains the core software architecture for a PhD research project focusing on Energy-Aware Federated Learning (HE-AFL) at the extreme edge. 

The system is designed to bridge ultra-low-power microcontrollers (STM32) with edge AI accelerators (Jetson Orin Nano) and a central aggregation server, using CoAP (UDP) for lightweight telemetry and the Flower framework for AI orchestration.



## 🏗️ The System Architecture & Logic Flow

The PLUDOS system is divided into two main "Islands" that communicate with each other:

### 1. The Edge Island (Jetson Orin Nano & STM32)
This is where data is generated, collected, and learned from.
* **`mock_stm32.py`**: A simulator for the physical STM32 microcontroller. It wakes up, blasts 55 UDP packets containing 3D vibration data and power metrics, and goes back to sleep.
* **`data-engine.py`**: The Jetson's Gatekeeper. It constantly listens on UDP Port 5683. When the STM32 fires data, this script catches it, holds it in the Jetson's RAM, and saves it as a highly compressed `.parquet` file after reaching 50 packets. This protects the Jetson's SD card from degradation.
* **`client.py`**: The AI Worker (`ClientApp`). When instructed by the central server, this script scans the RAM buffer, loads the real `.parquet` vibration data, and uses the Jetson's NVIDIA GPU to train an XGBoost model.
* **`Dockerfile` & `jetson-compose.yaml`**: The deployment blueprints. These files tell Podman how to package `data-engine.py` and `client.py` into secure containers that start automatically on the Jetson, complete with a virtual RAM disk (`tmpfs`) for the buffer.

### 2. The Central Server Island (Your Laptop/Cloud)
This is the brain that coordinates the AI and monitors the energy.
* **`server.py`**: The AI Orchestrator (`ServerApp`). It waits for Jetson clients to connect, tells them which round of training to execute, and aggregates their learned weights using the `FedAvg` strategy.
* **`pyproject.toml`**: The modern Flower configuration file. It links `server.py` and `client.py` together so we can test the entire distributed network locally on one machine using the Ray engine.
* **`compose.yaml`**: The analytics infrastructure. This Podman file spins up InfluxDB (a time-series database) and Grafana (a visualization dashboard) to eventually monitor the energy data captured by Alumet.

## 🔄 The Sequence of Operations
1. STM32 senses vibration -> Sends CoAP UDP packet to Jetson.
2. Jetson `data-engine` buffers 50 packets -> Saves to `.parquet` in RAM.
3. Central Server starts an FL Round -> Pings Jetson `client.py`.
4. Jetson `client.py` reads the `.parquet` file -> Trains XGBoost on GPU -> Sends weights back to Server.