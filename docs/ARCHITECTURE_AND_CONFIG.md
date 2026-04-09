# PLUDOS: Architecture and Configuration Guide

This document explains the purpose of the various configuration files (`.yaml`, `.toml`, `Containerfile`) in the PLUDOS project, and how they relate to one another.

---

## 1. Why do we have both `Containerfile` and `compose.yaml`?

A common point of confusion is why we need multiple files for containers. Think of it like a restaurant:
*   **The `Containerfile` (formerly Dockerfile) is the Recipe.** It tells the system *how to cook a specific dish*. In our project, `client/Containerfile` takes a bare NVIDIA Linux image, installs Python, copies our `requirements.txt`, runs `pip install`, and sets up our directories. It builds a static, immutable **Image**.
*   **The `compose.yaml` is the Restaurant Manager.** It tells the system *how to run the restaurant*. It takes the Images built by the `Containerfile` and spins them up into live **Containers**. Crucially, it manages the infrastructure around them:
    *   It maps ports (e.g., exposing UDP 5683 to the outside world).
    *   It mounts volumes (e.g., creating the `tmpfs` RAM-disk so we don't destroy the Jetson's SD card).
    *   It defines startup order (e.g., ensuring `ai-worker` waits for `data-engine`).
    *   It handles auto-restarts if a script crashes.

**In the `client/` folder:** The `compose.yaml` uses the `Containerfile` to build the custom AI environment and runs our two Python scripts as separate, coordinated services.
**In the `server/` folder:** The `compose.yaml` doesn't need a `Containerfile` because we are using pre-built, official images for InfluxDB and Grafana straight from the internet.

---

## 2. What is `pyproject.toml`?

The `pyproject.toml` file is the modern standard for configuring Python projects. TOML (Tom's Obvious, Minimal Language) is designed to be easily readable by humans and machines. 

### How it relates to our system:
1.  **Dependencies:** Instead of just having a `requirements.txt`, the `[project]` section in `pyproject.toml` explicitly lists the high-level dependencies needed to run the Python code (`flwr`, `xgboost`, `numpy`).
2.  **Flower Configuration:** The `[tool.flwr.app]` section is strictly for the Flower AI framework. It tells the framework exactly where the entry points to our code are:
    *   `serverapp = "server:app"` (Look inside `server.py` for the variable named `app`)
    *   `clientapp = "client:app"` (Look inside `client.py` for the variable named `app`)

When you run `flwr run .` on your laptop, the Flower engine reads the `pyproject.toml` to understand how to glue your Server and Client code together into a local simulation.

---

## 3. The Big Picture: How They All Relate

1.  **The Code (`.py`)**: Contains the actual logic (Federated Learning, UDP servers, STM32 simulation).
2.  **The Python Config (`pyproject.toml`)**: Tells the Python ecosystem and the Flower framework how to read and execute that code.
3.  **The Environment Builder (`Containerfile`)**: Packages the Code and the Python Environment into an isolated, portable Linux box.
4.  **The Orchestrator (`compose.yaml`)**: Takes those isolated boxes, turns them on, wires them to the network, and attaches physical storage/RAM to them.
