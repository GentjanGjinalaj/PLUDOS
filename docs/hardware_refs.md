# Hardware References

Primary hardware in the PLUDOS system with key specifications and references
to external documentation. All numbers here are from datasheets — if a number
in the code differs, investigate before assuming the code is wrong.

---

## STM32U585AII6Q (Extreme Edge MCU)

**Board:** B-U585I-IOT02A (STMicroelectronics Discovery kit)

| Spec | Value |
|---|---|
| Core | ARM Cortex-M33 @ up to 160 MHz |
| Security | TrustZone (PLUDOS uses non-secure only) |
| Flash | 2 MB |
| SRAM total | **786 KB** (768 KB main + 16 KB SRAM4 backup domain) |
| WiFi | MXCHIP EMW3080 (SPI2), 2.4 GHz only |
| IMU | ISM330DHCX (accel + gyro), I2C2, address 0x6B (SA0=VDD, left-shifted 0xD6 in firmware). MOVING capture: accel ODR 3332 Hz / gyro ODR 416 Hz; IDLE snapshot: 12.5 Hz |
| Other sensors | LIS2MDL, LPS22HH, HTS221, VL53L5CX, 2× MEMS mics — HTS221 (temp/humidity) and LPS22HH (pressure) are read for env stamps; the rest unused |

**Linker script:** `STM32U585AIIXQ_FLASH.ld`
- Main SRAM mapped to 768 KB
- SRAM4 (16 KB) not mapped by default; available for backup domain use
- Stack: 1 KB (`_Min_Stack_Size = 0x400`)
- Heap: 16 KB (`_Min_Heap_Size = 0x4000`; shared by newlib `printf` and the MXCHIP
  WiFi BSP, which `malloc`s 2.5 KB net buffers — raised from 0x1000 after a NULL-alloc
  spin caused IWDG resets during drain, see CHANGELOG Phase 3)

**Key datasheets and references:**
- STM32U585 Reference Manual (RM0456) — peripheral registers, clock tree
- B-U585I-IOT02A User Manual (UM2839) — board schematic, pin assignments
- ISM330DHCX datasheet — accelerometer/gyro register map, ODR settings
- MXCHIP EMW3080 AT command guide — WiFi module SPI protocol

**Known errata / issues:**
- EXTI interrupt routing for MXCHIP SPI requires manual `HAL_GPIO_EXTI_Rising_Callback`
  in `stm32u5xx_it.c`. Without this, the SPI semaphore is never signalled and
  WiFi hangs. See `WIFI_FIX_AND_BUILD.md`.
- No RTC battery (VBAT) by default → no persistent real-time clock across power loss.
  An LSE crystal (PC14/PC15) *is* present and locking; Phase 3 configures the RTC
  peripheral + wake-up timer off LSE, but only as a **Stop2 wake source**, not a wall
  clock. The NTP offset workaround (relative-tick anchoring) is still the time base,
  documented in `state_machine.md` and `wire_protocol.md`.

---

## Jetson Orin Nano Super Developer Kit (Gateway)

| Spec | Value |
|---|---|
| Module | Jetson Orin Nano 8 GB |
| CPU | 6-core ARM Cortex-A78AE |
| GPU | NVIDIA Ampere, 1024 CUDA cores + 32 Tensor cores |
| AI performance | 67 TOPS |
| RAM | 8 GB LPDDR5 |
| Power envelope | 7–25 W (NVPModel modes: 7 W / 15 W / 25 W MAXN_SUPER) |
| BSP | JetPack r35.x (Ubuntu 22.04) |
| Power monitor | INA3221 multi-channel (accessible via tegrastats or sysfs) |

**NVPModel power modes:**
```bash
sudo nvpmodel -m 0   # MAXN_SUPER — 25 W full power
sudo nvpmodel -m 1   # 15 W
sudo nvpmodel -m 2   # 7 W low-power
sudo jetson_clocks   # lock clocks to max (deterministic performance)
```

Always note which NVPModel is active when reporting energy benchmarks.

**Container base image:** `nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3`
The L4T version in the image tag must match the JetPack BSP version on the
host. Mismatches cause `nvidia-container-cli` errors.

**Key references:**
- Jetson Orin Nano Developer Kit Carrier Board Specification Sheet
- NVIDIA Jetson Orin Nano System-on-Module Data Sheet
- tegrastats documentation: `man tegrastats` on the Jetson
- INA3221 sysfs path: `/sys/bus/i2c/drivers/ina3221/*/iio:device*/in_power*_input`

---

## Central Server (Development Laptop)

Currently a personal laptop running Ubuntu. Runs:
- Flower `ServerApp` (direct Python process)
- InfluxDB 2.7 + Grafana (Podman containers)

No specific hardware requirements beyond what Flower and InfluxDB need.
Migration to a dedicated server is listed as a future enhancement.

---

## Network

| Component | Protocol | Port |
|---|---|---|
| STM32 → Jetson (live telemetry) | Raw UDP (24-byte `PludosTelemetry`, ADR-016 v3) | 5683 |
| STM32 → Jetson (high-rate drain) | Raw UDP (`PLDR` drain frames, ADR-020/021) | 5684 |
| Jetson → STM32 (beacon) | UDP broadcast (`PLUDOS-GW:<ip>[:csv-ids]`) | 5000 |
| Jetson → Server (FL) | gRPC over Tailscale | 9091 |
| Jetson → InfluxDB | HTTP over Tailscale | 8086 |
| Gateway VPN | WireGuard (Tailscale) | 41641 UDP |

There is no CoAP anywhere in the current code — ADR-015 removed it. The live
`PludosTelemetry` path on 5683 (ADR-016 v3) is dormant in practice: under
ADR-020/021 the radio is off outside a drain window, so the signal PLUDOS keeps
arrives as `PLDR` drain frames on 5684, reassembled into the `cap_*` Parquet
files (see `wire_protocol.md §2`).

**WiFi constraint:** The MXCHIP EMW3080 on the STM32 board only supports
2.4 GHz. Hotspots must be configured to broadcast on 2.4 GHz only.
