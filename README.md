# PLUDOS: Modular Framework for Industrial Frugal Data Collection & Edge AI

**Industrial-Academic Collaboration:** [Savoye SASU](https://www.savoye.com/) & PhD Research [[theses.fr/s410359](https://theses.fr/s410359)]

PLUDOS (**P**ower-aware **L**ightweight **U**DP **D**ata **O**rchestration **S**ystem) is a modular framework designed for **frugal data collection** and **Energy-Aware Federated Learning (HE-AFL)** at the extreme edge. Developed for large-scale industrial logistics, the system optimizes the energy-accuracy trade-off in warehouse automation environments.

> ### ⚖️ Intellectual Property Notice
> **Copyright © 2026 Gentjan Gjinalaj & Savoye SASU. All Rights Reserved.**
> 
> This repository contains proprietary research and industrial code. Unauthorized copying, modification, distribution, or use is strictly prohibited. Access is provided for review and academic validation within the context of the doctoral thesis.

---

## 🎯 Core Research Objectives

* **Computational Frugality:** Minimizing the energy footprint of the monitoring system itself to ensure a "net-zero" monitoring overhead.
* **Modular Scalability:** Rapid deployment across diverse industrial hardware architectures, from ultra-low-power microcontrollers to edge AI accelerators.
* **High-Granularity Telemetry:** Real-time vibration and power analysis utilizing lightweight, event-driven protocols (CoAP/UDP).

---

## 🏗️ System Architecture

### 1. The Edge Provisioning Layer (Industrial IoT)
* **Hardware Abstraction:** Bridges ultra-low-power microcontrollers (**STM32**) with Edge AI accelerators (**NVIDIA Jetson Orin Nano**).
* **Event-Driven Telemetry:** Implements a lightweight CoAP-based UDP protocol for 3-axis vibration data and high-frequency power metrics.
* **Smart Buffering Engine:** A deterministic data engine that handles packet reordering and high-performance serialization into compressed **Apache Parquet** formats, protecting edge storage from excessive wear.

### 2. The Federated Learning Island (AI Orchestration)
* **Edge Worker (`ClientApp`):** A containerized AI client leveraging **NVIDIA CUDA** for on-device XGBoost training. It enforces strict production hardware standards for industrial reliability.
* **Central Orchestrator (`ServerApp`):** Coordinates global model updates using the **Flower framework**, utilizing the `FedAvg` strategy to aggregate insights across distributed logistics nodes.
* **Deployment Suite:** Fully containerized via **Podman** and **Docker Compose**, featuring virtual RAM disks (`tmpfs`) to ensure operational longevity in 24/7 industrial environments.

---

## 🔄 Operational Sequence

1.  **Ingestion:** Industrial sensors transmit telemetry via CoAP UDP with mission-critical status flags.
2.  **Orchestration:** The Data Engine sorts, validates, and serializes high-velocity streams into frugal, compressed data structures.
3.  **Training:** The Central Server triggers a Federated Learning round; Edge Workers train local models on real-world industrial datasets.
4.  **Aggregation:** Model weights are returned to the server to refine the global energy-efficiency model without sharing raw data.

---

## 🛠️ Technical Stack

* **Languages:** Python, C (Firmware)
* **AI/ML:** Flower (FL), XGBoost, NVIDIA CUDA
* **Protocol:** CoAP (UDP), Custom Telemetry Headers
* **Storage:** Apache Parquet, InfluxDB, Grafana
* **DevOps:** Podman, Docker Compose, Ray Engine
