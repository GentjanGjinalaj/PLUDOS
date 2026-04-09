# Network & Hotspot Configuration

The PLUDOS Edge Node relies on a local WiFi hotspot (provided by a phone) to connect to the Jetson Nano gateway via CoAP over UDP.

## Hotspot Requirements (Phone)
The MXCHIP EMW3080 WiFi module **only supports 2.4GHz**. It does not support 5GHz.

- **SSID:** `Galaxy S24 Ultra` (Exact match, case-sensitive)
- **Password:** `12345666` (Exactly 8 characters)
- **Band:** `2.4 GHz` (Critical: Ensure 5GHz is disabled on the hotspot)
- **Security:** `WPA2-AES`

## Jetson Configuration (Gateway)
The Jetson acts as the CoAP server. The STM32 sends UDP packets to the Jetson's IP address.

1. Find the Jetson's IP address by running `hostname -I` on the Jetson while it is connected to the phone hotspot.
2. If the Jetson's IP is different from `10.17.194.48`, update `Core/Src/main.c` in the STM32 project:
   ```c
   #define JETSON_IP "YOUR_JETSON_IP"
   ```
3. Rebuild the STM32 firmware and flash it.
4. Ensure the CoAP server is listening on the Jetson: `ss -tlnup | grep 5683`.

## Troubleshooting
- **Cannot find WiFi:** Check that the phone is broadcasting in 2.4GHz.
- **Connection Timeout:** Verify the exact SSID and password.
- **Data Not Reaching Jetson:** Check if the Jetson's IP has changed or if the Ubuntu firewall (`ufw`) is blocking port `5683 UDP`.
