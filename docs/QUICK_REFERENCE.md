# Quick Reference Card

## 1. Build & Flash
```bash
cd STM_Shuttles/PLUDOS_Edge_Node/Debug
make clean && make -j4
```
Flash the resulting `.elf` file using STM32CubeIDE or ST-Link utility.

## 2. UART Debugging
Connect to the board's serial port at `115200` baud.

**Expected Boot Sequence:**
```text
[NETWORK] WiFi init sequence starting...
[NETWORK] Performing WiFi module hard reset...
[NETWORK] Registering WiFi SPI bus...
[NETWORK] WiFi SPI bus registered successfully (took 234 ms)
[NETWORK] Initializing MXCHIP (SPI Handshake)...
[NETWORK] MX_WIFI_Init returned: 0x00 after 1892 ms
[NETWORK] SPI Link OK! Connecting to WiFi: 'Galaxy S24 Ultra'
[NETWORK] MX_WIFI_Connect returned: 0x00 after 3456 ms
[NETWORK] SUCCESS! IP: 192.168.55.150
[NETWORK] UDP Socket created (ID: 0)
[UDP] Blasted 87 bytes → Jetson
```

## 3. Critical Configs (`main.c`)
```c
#define WIFI_SSID      "Galaxy S24 Ultra"
#define WIFI_PASSWORD  "12345666"
#define JETSON_IP      "10.17.194.48"
#define JETSON_PORT    5683
```
