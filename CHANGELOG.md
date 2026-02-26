# PLUDOS Project History & Changelog

## [0.1.0] - Local Prototyping Phase 1
**Goal:** Establish the fundamental plumbing between the sensing devices, the edge node, and the central server before hardware deployment.

### Added
* **Git Version Control:** Initialized clean repository ignoring the massive Python virtual environment.
* **Central Server Stack (`compose.yaml`):** Configured Podman to run InfluxDB and Grafana locally for future Alumet energy monitoring.
* **Modern Flower Architecture:** * Created `server.py` (`ServerApp`) with FedAvg strategy.
  * Created `client.py` (`ClientApp`) with XGBoost tabular data training.
  * Added `pyproject.toml` to orchestrate local simulation via the Ray distributed engine.
* **CoAP Ingestion Pipeline:**
  * Created `data-engine.py` using `aiocoap` to catch UDP packets.
  * Implemented RAM buffering logic to batch 50 packets into highly compressed `.parquet` files via `pyarrow`.
  * Created `mock_stm32.py` to simulate high-frequency sensor blasts.
* **Jetson Deployment Blueprint:** Drafted `Dockerfile` and `jetson-compose.yaml` utilizing `tmpfs` (RAM disk) and NVIDIA GPU pass-through to prepare for physical hardware arrival.

### Fixed
* Resolved Linux rootless permission mapping issues for Podman database deployments.
* Handled GitHub 100MB file limits by strictly managing `.gitignore` for `libnccl.so`.
* Migrated legacy Flower `start_client()` methods to the new App framework to remove deprecation warnings.