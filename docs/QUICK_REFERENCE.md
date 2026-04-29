# Quick Reference Card

## 1. Build & Flash (STM32)

```bash
cd STM_Shuttles/PLUDOS_Edge_Node/Debug
make clean && make -j4
```

Flash the resulting `.elf` file using STM32CubeIDE (Run → Flash) or
ST-Link Utility. See `docs/WIFI_FIX_AND_BUILD.md` for WiFi init fix history.

## 2. UART Debugging

Connect to the board's serial port at **115200 baud** (8N1, no flow control).

Expected boot sequence:
```
[NETWORK] WiFi init sequence starting...
[NETWORK] Performing WiFi module hard reset...
[NETWORK] WiFi SPI bus registered successfully
[NETWORK] Connecting to WiFi: '<YOUR_SSID>'
[NETWORK] SUCCESS! IP: <STM32_IP>
[SENSOR] State machine initialized. State: IDLE
```

## 3. Critical Firmware Configs (`main.c`)

> **Do not hardcode credentials in source.** These defines should live in
> `Core/Inc/wifi_credentials.h` (gitignored). See `docs/NETWORK_SETUP.md`.

```c
#define WIFI_SSID      "<your hotspot SSID>"
#define WIFI_PASSWORD  "<your hotspot password>"
#define JETSON_IP      "<jetson IP from 'ip -4 addr show wlan0'>"
#define JETSON_PORT    5683
```

## 4. Start Data Engine (Jetson)

```bash
cd ~/PLUDOS/client
podman-compose up --build data-engine        # foreground (first run)
podman-compose up -d data-engine             # background (subsequent)
podman logs -f pludos-data-engine            # live logs
```

## 5. Start Flower FL Round (Laptop)

```bash
# Ensure data-engine has flushed at least one .parquet file first
flwr run .
```

## 6. Start Monitoring Stack (Laptop)

```bash
cd server
podman-compose up -d
# InfluxDB: http://localhost:8086
# Grafana:  http://localhost:3000  (admin / admin on first login)
```

## 7. Test with Simulator (No Hardware)

```bash
# Terminal 1: start data-engine locally
python data-engine.py

# Terminal 2: send mock packets
python tools/mock_stm32.py
```

## 8. Common Podman Commands

```bash
podman ps                              # list running containers
podman logs pludos-data-engine         # check logs
podman-compose down                    # stop all services
podman-compose up --build data-engine  # rebuild image and start
```
