# Network & Hotspot Configuration

The PLUDOS Edge Node connects to the Jetson gateway via a local WiFi hotspot
(typically a phone hotspot) using CoAP over UDP.

> **Security note:** Real SSID, password, and IP addresses are NOT committed
> here. Configure them locally in `wifi_credentials.h` (gitignored) on the
> STM32 side, and in `client/.env` on the Jetson side.

## Hotspot Requirements

The MXCHIP EMW3080 WiFi module **only supports 2.4 GHz**. It will not join a
5 GHz or auto-band network.

| Setting | Requirement |
|---|---|
| Band | **2.4 GHz only** — disable 5 GHz or use a 2.4 GHz-only hotspot |
| Security | WPA2-AES |
| SSID | Your choice — must match `WIFI_SSID` define in firmware |
| Password | Your choice — must match `WIFI_PASSWORD` define in firmware |

Configure your phone (Android or iOS) to broadcast on 2.4 GHz only.
On Android, check "Extended compatibility" or "2.4 GHz band" in hotspot settings.

## Jetson IP (Gateway)

1. Connect Jetson to the hotspot network.
2. Find its IP: `ip -4 addr show wlan0` (or `hostname -I`).
3. Update the STM32 firmware with that IP (`JETSON_IP` define in `main.c`),
   rebuild, and flash.
4. Verify CoAP listener is up: `ss -tlnup | grep 5683`.

## Ports Used

| Port | Protocol | Service |
|---|---|---|
| 5683 | UDP | CoAP (confirmable telemetry) |
| 5000 | UDP | Beacon discovery (stubbed, future zero-touch) |

Open both on the Jetson firewall:
```bash
sudo ufw allow 5683/udp
sudo ufw allow 5000/udp
```

## Troubleshooting

- **STM32 can't find WiFi:** Hotspot is broadcasting on 5 GHz — force 2.4 GHz.
- **Connection timeout:** SSID or password mismatch in firmware defines.
- **CoAP ACKs missing:** Verify Jetson IP in firmware matches `ip -4 addr show wlan0` output.
- **Firewall blocking:** `sudo ufw status` — 5683/udp must show ALLOW.
