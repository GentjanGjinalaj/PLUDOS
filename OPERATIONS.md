# PLUDOS Operations Cheatsheet

Quick reference for day-to-day work on the physical hardware.

---

## 1. SSH into the Jetson

```bash
ssh warehouse1@192.168.0.100
# password: Warehouse1savoye!
```

---

## 2. Check what's running on the Jetson

```bash
podman ps
podman logs pludos-data-engine        # CoAP/UDP listener
podman logs pludos-alumet-relay       # Alumet energy monitor
```

---

## 3. Start / restart services on the Jetson

```bash
cd ~/PLUDOS/client

# Start CoAP data engine (STM32 comms) — always needed
~/.local/bin/podman-compose up -d data-engine

# Start Alumet energy monitor (local mode, no Tailscale needed)
~/.local/bin/podman-compose up -d alumet-relay

# Restart a specific service after code changes
podman stop pludos-data-engine && podman rm pludos-data-engine
~/.local/bin/podman-compose up -d --build data-engine
```

---

## 4. Pull latest code and rebuild on the Jetson

```bash
cd ~/PLUDOS
git pull origin main
cd client
~/.local/bin/podman-compose up -d --build data-engine
```

---

## 5. Check Parquet files (shuttle data arriving)

```bash
ls -lh ~/PLUDOS/client/ram_buffer/
```

Files appear here after a shuttle mission ends (or buffer fills).
Empty = no data received yet.

---

## 6. Monitor the STM32 serial output (from laptop)

```bash
# Check which port is the STM32 STLINK
ls /dev/ttyACM*

# Read serial output (you are in dialout group — no sudo needed)
stty -F /dev/ttyACM1 115200 raw -echo && cat /dev/ttyACM1
# Ctrl+C to stop
```

> `/dev/ttyACM0` = Jetson USB gadget, `/dev/ttyACM1` = STM32 STLINK-V3

---

## 7. Key IPs and ports

| What | Value |
|------|-------|
| Jetson WiFi IP | `192.168.0.100` |
| Laptop IP | `192.168.0.101` |
| CoAP (critical) | Jetson UDP 5683 |
| NC-UDP (non-critical) | Jetson UDP 5684 |
| Beacon | Jetson UDP 5000 |
| WiFi SSID | `Pludos_2.4_5` |
| InfluxDB | `http://192.168.0.101:8086` |
| Grafana | `http://192.168.0.101:3000` |

---

## 8. After reflashing STM32 — confirm it's talking to the Jetson

On the laptop, watch data-engine receive packets:

```bash
ssh warehouse1@192.168.0.100 'podman logs -f pludos-data-engine'
```

You should see `[COAP]` or `[BUFFER]` lines appear within seconds of the STM32 booting.

---

## 9. Alumet relay build (first time only, ~25 min — Rust compile)

```bash
ssh warehouse1@192.168.0.100
cd ~/PLUDOS/client
podman build -f alumet-relay/Containerfile alumet-relay/ -t pludos-alumet-relay
# then start it:
~/.local/bin/podman-compose up -d alumet-relay
```

---

## 10. InfluxDB / Grafana credentials

| What | Value |
|------|-------|
| Grafana URL | `http://192.168.0.101:3000` |
| Grafana login | `admin` / `changeme` |
| InfluxDB URL | `http://192.168.0.101:8086` |
| InfluxDB token | `changeme-pludos-token` |
| InfluxDB org | `pludos` |
| InfluxDB bucket | `alumet_energy` |

---

## 11. What to ignore

- `Error validating CNI config file ... plugin firewall` — harmless Podman warning, appears on every command
- `CoAP RST` in data-engine logs from your own laptop test packets — normal
- `LPS22HH not found on I2C2` in STM32 serial — non-fatal, pressure field stays 0.0
