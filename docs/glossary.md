# Glossary

Domain terms used throughout the PLUDOS codebase and documentation.

---

| Term | Definition |
|---|---|
| **Shuttle** | A warehouse autonomous shuttle vehicle. Each shuttle carries one STM32U585 edge node. |
| **Edge node** | The STM32U585 board (B-U585I-IOT02A) mounted on a shuttle. Samples the ISM330DHCX IMU (104 Hz ODR, polled 50 Hz in MOVING / 10 Hz in IDLE), runs the IDLE/MOVING state machine, and streams 24-byte `PludosTelemetry` UDP packets to the gateway at 50 Hz (MOVING) or 0.1 Hz (IDLE). |
| **Gateway** | A Jetson Orin Nano Super Developer Kit installed in a warehouse. Receives telemetry from multiple shuttles, buffers per-shuttle, flushes to Parquet on mission end, runs Flower FL client. One gateway per warehouse; designed to handle ≥ 100 shuttles. |
| **Central server** | Laptop (development) or dedicated server (production). Runs Flower ServerApp, InfluxDB, and Grafana. Aggregates FL models from all gateways. |
| **Mission** | One shuttle movement cycle: from the first STATE_MOVING packet until the gateway detects ≥ 30 s of STATE_IDLE after any MOVING run (`MISSION_END_IDLE_S`). A single Parquet file is written per completed mission. |
| **STATE_IDLE** | STM32 operating mode. Internal sampling: 10 Hz (for FSM responsiveness). Telemetry TX rate: **0.1 Hz** (one packet every 10 s). Shuttle is stationary. |
| **STATE_MOVING** | STM32 operating mode. Internal sampling and TX rate: **10 Hz** (every sample sent immediately). Shuttle is in motion. |
| **PludosTelemetry** | The 24-byte packed binary struct sent as raw UDP from STM32 to gateway (port 5683). Contains shuttle_id, sequence_id, tick_ms, state, accel xyz, gyro xyz, temp_c, humidity_pct. See `wire_protocol.md §1`. Protocol version v3 (ADR-016). |
| **FL round** | One Flower federated learning round: server signals all clients to train locally, clients return model updates, server aggregates. PLUDOS currently runs 3 rounds. |
| **AlumetProfiler** | Python class in `client.py` that measures energy consumption during FL training. Phase 1 done: reads `tegrastats` for real Jetson power rails. Phase 2 scaffolded: INA3221 via Alumet relay sidecar. See ADR-011. |
| **Alumet** | Open-source energy measurement framework developed by UGA/LIG. Target for real energy sensing integration on the Jetson (tegrastats / INA3221). |
| **Alumet relay** | An Alumet instance running on the Jetson that forwards local power metrics to the main Alumet instance on the central server. |
| **NTP offset** | Per-shuttle correction factor: `offset_ms = receipt_time_ms - tick_ms`. Established on the first received packet, refreshed every `NTP_REFRESH_INTERVAL` packets (default 100) to compensate STM32 crystal drift. Converts relative `tick_ms` to gateway wall-clock time. |
| **tmpfs** | RAM-backed filesystem. Used for the `ram_buffer` bind-mount on the Jetson. Fast writes, no wear, but data lost on reboot. |
| **SRAM budget** | 786 KB total on STM32U585: 768 KB main SRAM + 16 KB SRAM4 (backup domain). No application-level sensor buffer (ADR-015 removed it); each sample is sent directly over UDP. |
| **Beacon discovery** | Zero-touch provisioning: data-engine broadcasts `PLUDOS-GW:<ip>[:csv-ids]` to `255.255.255.255:5000` every 10 s. STM32 listens at boot (30 s probe), on WiFi reconnect, and periodically every 30 s while IDLE. Implemented end-to-end (P2-1). |
| **Tailscale** | WireGuard-based mesh VPN used for gateway ↔ server connectivity. Handles NAT traversal for mobile deployments. |
| **Flower (flwr)** | Federated learning framework. `ServerApp` runs on the central server; `ClientApp` runs on each Jetson gateway. |
| **XGBoost** | Gradient-boosted decision tree library. The current FL model type. Runs on Jetson GPU with `tree_method='hist'`. |
| **ADR** | Architecture Decision Record. Captures what was decided, why, and what the consequences are. See `decisions.md`. |
| **P0 / P1 / P2** | Priority levels for known problems. P0 = blocking, P1 = important, P2 = nice-to-have. See `current_problems.md`. |
| **CubeMX** | STM32CubeMX: the STMicroelectronics GUI tool that generates HAL driver code from the `.ioc` project file. Never edit `.ioc` directly. |
| **USER CODE guards** | `/* USER CODE BEGIN <name> */ ... /* USER CODE END <name> */` markers in CubeMX-generated files. Application code placed inside these guards survives regeneration. |
| **MXCHIP EMW3080** | The 2.4 GHz WiFi module on the B-U585I-IOT02A board, connected to the STM32 via SPI2. Uses AT-command-style IPC. |
| **ISM330DHCX** | 6-axis IMU (accelerometer + gyroscope) on the B-U585I-IOT02A board. Connected via I2C2 at address 0x6B (SA0=VDD). Provides accel_xyz (±2 g) and gyro_xyz (±250 dps) at 10 Hz ODR. |
| **INA3221** | Multi-channel power monitor on the Jetson module. Target sensor for real Alumet energy measurement (ADR-011). |
| **tegrastats** | NVIDIA tool on Jetson that exposes real-time GPU/CPU power readings. Used by AlumetProfiler Phase 1. |
| **ZUPT** | Zero-velocity UPdate. Integration technique used in `data-engine.py` to estimate shuttle speed and displacement from accelerometer data. Velocity is reset to zero at each IDLE→MOVING transition to bound drift. |
| **sequence_monotonic** | Per-shuttle monotonically increasing packet counter maintained by the gateway. The wire `sequence_id` is uint16 (wraps at 65 535); the gateway unwraps it into a globally unique sort key. Always use this for Parquet ordering, not `timestamp_ms`. |
