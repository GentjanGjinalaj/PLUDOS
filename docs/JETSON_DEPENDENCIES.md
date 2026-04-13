# Jetson: venv & Dependencies Strategy

**TL;DR:**
- ❌ **NO venv on Jetson host**
- ❌ **NO pip install on Jetson host**
- ✅ **Container handles all Python dependencies automatically**

---

## Why No venv on Jetson?

**Your deployment model:** `Podman Container` running `data-engine.py`

**Container = isolated Python environment**
- Dockerfile specifies base image: `nvcr.io/nvidia/l4t-pytorch:r35.2.1` (includes Python 3.10)
- `requirements.txt` is installed **inside** the container during build
- Jetson host doesn't need Python packages at all
- Container is "shipped" with all dependencies baked in

---

## Jetson Host Only Needs

```bash
# Container runtime (just the tool, not Python packages)
sudo apt install podman podman-compose

# Git (to clone repo)
sudo apt install git

# That's it!
```

**No Python, no pip, no venv needed on the host.**

---

## Where dependencies Actually Live

```
Laptop (development):
├── python3 (system)
├── pludos_venv/ (your dev venv)
├── requirements.txt (defines dependencies)
└── data-engine.py (source code)

↓ Git push

Jetson (production):
├── podman (container runtime)
├── client/
│   ├── compose.yaml (orchestration config)
│   ├── Containerfile (build recipe)
│   ├── requirements.txt (copied into container)
│   └── data-engine.py (copied into container)
│
└── Running Container (isolated):
    ├── Python 3.10 (from base image)
    ├── aiocoap, pandas, PyArrow (installed during build)
    └── data-engine.py (running inside)
```

---

## What If You Need to Run Directly on Jetson (Not in Container)?

**Status:** Not recommended for production. Only for debugging.

If you must run directly on Jetson host:

```bash
# Create venv (one-time)
python3 -m venv ~/pludos_venv

# Activate
source ~/pludos_venv/bin/activate

# Install requirements
pip install -r requirements.txt

# Run
python3 client/data-engine.py
```

But **this is not the deployment approach** — you want containerized because:
- Jetson can be wiped/reimaged → code still runs (reproducible)
- Multiple containers can coexist peacefully
- Systemd integration is cleaner

---

## Testing vs Production

| Scenario | Command | venv? | Container? |
|----------|---------|-------|-----------|
| Laptop dev (just testing CoAP) | `python3 client/data-engine.py` | ✅ (pludos_venv) | ❌ |
| Laptop mock test | `python3 mock_stm32.py` | ✅ (pludos_venv) | ❌ |
| Jetson production | `podman-compose up data-engine` | ❌ | ✅ |
| Jetson debug (rare) | `python3 client/data-engine.py` | ✅ (if needed) | ❌ |

---

## Jetson First-Time Setup (Copy-Paste)

```bash
# SSH into Jetson
ssh warehouse1@10.187.8.100

# Install container tools only
sudo apt update
sudo apt install -y podman podman-compose git

# Clone repo
git clone https://github.com/<your-username>/PLUDOS.git
cd PLUDOS

# Get your IP (for STM32 firmware)
ip -4 addr show wlan0
# Note the IP address here ← share with laptop

# Open firewall
sudo ufw allow 5683/udp
sudo ufw allow 5000/udp

# Deploy container
cd client
podman-compose up --build data-engine

# ✅ Done! Container is running with zero host dependencies.
```

---

## What If requirements.txt Changes?

### On Laptop:
1. Update `requirements.txt` (add new package)
2. Test locally in pludos_venv: `pip install -r requirements.txt`
3. Commit and push to Git

### On Jetson:
1. Pull from Git: `git pull origin main`
2. Rebuild container: `podman-compose up --build data-engine` (or `--no-cache` for clean build)
3. Container automatically installs new dependencies during build

**Old host dependencies are NOT carried over** (clean slate each time).

---

## Podman Compose vs Docker Compose

Both work identically for our purposes:

```bash
# These are the same:
podman-compose up --build data-engine
docker-compose up --build data-engine    # (if docker installed)
```

Use `podman-compose` on Jetson (per project constraints: Podman-only, no Docker).

---

## Summary

**Jetson Host:**
- Is a "container runtime" host, not a dev environment
- Needs: `podman`, `podman-compose`, `git` (3 tools)
- Does NOT need: Python, pip, venv, requirements.txt (all live in container)

**Container Inside:**
- Is a complete, isolated Python environment
- Has: Python 3.10, all packages from requirements.txt, source code
- Runs: `data-engine.py` (listens on 0.0.0.0:5683)

**When deployed on Jetson:**
- Push button: `podman-compose up --build data-engine`
- Out comes: Running service, zero setup drama, reproducible every time

This is the power of containerization. Welcome to 2026! 🚀
