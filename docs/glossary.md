# Glossary

Domain terms used throughout the PLUDOS codebase and documentation.

---

| Term | Definition |
|---|---|
| **Shuttle** | A warehouse autonomous shuttle vehicle. Each shuttle carries one STM32U585 edge node. |
| **Edge node** | The STM32U585 board (B-U585I-IOT02A) mounted on a shuttle. Samples sensors, buffers data, transmits to gateway via CoAP/UDP. |
| **Gateway** | A Jetson Orin Nano Super Developer Kit installed in a warehouse. Receives data from multiple shuttles, runs Flower FL client, forwards models to server. One gateway per warehouse; designed to handle ≥ 100 shuttles. |
| **Central server** | Laptop (development) or dedicated server (production). Runs Flower ServerApp, InfluxDB, and Grafana. Aggregates FL models from all gateways. |
| **Mission** | One shuttle movement cycle: from the moment the STM32 enters STATE_MOVING until `mission_active = 0` is transmitted. A single Parquet file is written per completed mission. |
| **STATE_IDLE** | STM32 operating mode with low sampling rate (~2 Hz). Shuttle is stationary. Triggers CoAP data flush to gateway. |
| **STATE_MOVING** | STM32 operating mode with high sampling rate (50 Hz). Shuttle is in motion. Samples are buffered locally; no transmission during motion. |
| **CriticalPayload** | The 39-byte binary struct sent via CoAP CON from STM32 to gateway. Contains vibration, accelerometer, power, and status fields. See `wire_protocol.md`. |
| **FL round** | One Flower federated learning round: server signals all clients to train locally, clients return model updates, server aggregates. PLUDOS currently runs 3 rounds. |
| **AlumetProfiler** | Python class in `client.py` that measures energy consumption during FL training. Currently a placeholder; real integration is ADR-011. |
| **Alumet** | Open-source energy measurement framework developed by UGA/LIG. Target for real energy sensing integration on the Jetson (tegrastats / INA3221). |
| **Alumet relay** | An Alumet instance running on the Jetson that forwards local power metrics to the main Alumet instance on the central server. |
| **NTP offset** | Per-shuttle correction factor: `offset_ms = receipt_time_ms - tick_ms`. Computed once on first CoAP packet. Converts STM32 relative `tick_ms` to gateway-local wall clock time. |
| **CoAP CON** | CoAP Confirmable message. Requires an ACK from the recipient. Used for critical sensor data. |
| **CoAP NON** | CoAP Non-confirmable message. No ACK required. Used for non-critical data (future). |
| **tmpfs** | RAM-backed filesystem. Used for the `shared_ram_buffer` volume on the Jetson. Fast writes, no wear, but data lost on reboot. |
| **SRAM budget** | 786 KB total on STM32U585: 768 KB main SRAM + 16 KB SRAM4 (backup domain). Application uses a static 256-entry sensor buffer. |
| **Beacon discovery** | Planned zero-touch provisioning: STM32 discovers Jetson IP by listening for UDP broadcasts on port 5000 instead of using a hardcoded IP. Currently stubbed. |
| **Tailscale** | WireGuard-based mesh VPN used for gateway ↔ server connectivity. Handles NAT traversal for mobile deployments. |
| **Flower (flwr)** | Federated learning framework. `ServerApp` runs on the central server; `ClientApp` runs on each Jetson gateway. |
| **XGBoost** | Gradient-boosted decision tree library. The current FL model type. Runs on Jetson GPU with `tree_method='hist'`. |
| **ADR** | Architecture Decision Record. Captures what was decided, why, and what the consequences are. See `decisions.md`. |
| **P0 / P1 / P2** | Priority levels for known problems. P0 = blocking, P1 = important, P2 = nice-to-have. See `current_problems.md`. |
| **CubeMX** | STM32CubeMX: the STMicroelectronics GUI tool that generates HAL driver code from the `.ioc` project file. Never edit `.ioc` directly. |
| **USER CODE guards** | `/* USER CODE BEGIN <name> */ ... /* USER CODE END <name> */` markers in CubeMX-generated files. Application code placed inside these guards survives regeneration. |
| **MXCHIP EMW3080** | The 2.4 GHz WiFi module on the B-U585I-IOT02A board, connected to the STM32 via SPI2. Uses AT-command-style IPC. |
| **ISM330DLC** | 6-axis IMU (accelerometer + gyroscope) on the B-U585I-IOT02A board. Connected via I2C2. Currently used only for accelerometer data. |
| **INA3221** | Multi-channel power monitor on the Jetson module. Target sensor for real Alumet energy measurement (ADR-011). |
| **tegrastats** | NVIDIA tool on Jetson that exposes real-time GPU/CPU power readings. One path to real energy measurement for Alumet. |
