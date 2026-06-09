# OVERVIEW — STM_Shuttles/PLUDOS_Edge_Node (STM32 firmware)

> Newcomer's map of the firmware folder. For byte layouts see
> `docs/wire_protocol.md`; for the motion FSM see `docs/state_machine.md`.

## Why this folder exists

This is the **extreme edge** of the three-tier system
(**STM32 → Jetson gateway → central server**). One of these boards rides on
each Savoye XTPS shuttle. The firmware:

1. reads the on-board IMU (and a few environmental sensors),
2. decides whether the shuttle is **IDLE** or **MOVING** with a small state
   machine,
3. packs each reading into a fixed 24-byte telemetry packet, and
4. streams those packets over 2.4 GHz Wi-Fi (raw UDP) to the Jetson gateway
   that "adopted" it via a discovery beacon.
5. during a MOVING run, additionally **captures high-rate IMU into on-board
   PSRAM** (accel 3332 Hz + gyro 416 Hz batched in the ISM330 FIFO) and
   **drains** each sealed mission, plus periodic 10-min IDLE snapshots, to the
   gateway over a separate UDP port once the run ends (ADR-020/021).

It is a CubeMX-generated **STM32CubeIDE** project for the B-U585I-IOT02A
(STM32U585, Cortex-M33, bare-metal C, HAL drivers, no RTOS). Most of the tree
is vendor/generated code; only a handful of files contain PLUDOS logic.

## The files that actually contain our logic

These are hand-written (or hand-extended) — this is where the project lives.

| File | Responsibility | Weight |
|------|----------------|--------|
| `Core/Src/main.c` | **The whole application.** ~1900 lines. Holds the IDLE/MOVING state machine, the ISM330DHCX IMU read path (accel + gyro), the 24-byte `PludosTelemetry_t` packer, the boot-time **beacon discovery** that finds the gateway's IP, the live UDP transmit loop, and sequence/timestamp bookkeeping. Also the **high-rate capture/drain path** (ADR-020/021): batches MOVING IMU into the ISM330 FIFO → PSRAM, then drains sealed missions and 10-min IDLE snapshots over UDP, stamping each `DrainBegin` with `t0_tick` and `tx_tick` so the gateway can recover capture time. A boot **reset-cause report** (`[BOOT] reset cause: …`) logs why the MCU last reset, and a **pre-TX jitter** (random 1–15 s before powering the radio for a drain) decorrelates shuttles that exit MOVING together so they don't collide on the shared 2.4 GHz channel. PLUDOS code lives inside the `USER CODE BEGIN/END` guards. | **Core / critical** — this is the firmware |
| `Core/Src/sensors.c` | Hand-written I²C drivers for **HTS221** (temperature + humidity) and **LPS22HH** (pressure). Probes the chip, reads factory calibration, returns engineering units. Called from `main.c` to fill the environmental fields of the telemetry packet. | Core (secondary sensors) |
| `Core/Inc/sensors.h` | Public prototypes for the `sensors.c` drivers. | Helper (header) |
| `Core/Src/cJSON.c`, `Core/Inc/cJSON.h` | Vendored JSON library. **Grandfathered** for dev/debug paths only (it uses `malloc`, which the project otherwise bans). Not on the hot telemetry path — that path is the binary 24-byte struct, not JSON. | Helper / legacy |

> **Worth knowing:** three sensors are live, not just the IMU — `sensors.c`
> actively drives the **HTS221** (temp + humidity) and **LPS22HH** (pressure)
> alongside the **ISM330DHCX**, and `main.c` caches their readings into the
> telemetry packet.

## Configuration headers (`Core/Inc/`)

Edit-with-care: these tune the board but are not application logic.

- `wifi_credentials.h` — **Wi-Fi SSID/password. Gitignored secret.** Copy
  `wifi_credentials.h.example` to `wifi_credentials.h` and fill it in before
  building. Without it the build fails.
- `mx_wifi_conf.h` — MXCHIP EMW3080 Wi-Fi module settings (bare-metal mode,
  no CMSIS-OS).
- `b_u585i_iot02a_conf.h`, `b_u585i_iot02a_errno.h` — BSP board config.
- `stm32u5xx_hal_conf.h` — which HAL modules are compiled in.
- `stm32_assert.h` — assert macro for the LL drivers.
- `main.h` — almost entirely CubeMX **pin definitions** (which GPIO is the red
  LED, the Wi-Fi NSS line, etc.). Generated; don't hand-edit.

## CubeMX-generated files (don't hand-edit outside the guards)

These are regenerated whenever the `.ioc` is opened in CubeMX. Touching them
directly will be overwritten on the next regeneration.

- `Core/Src/stm32u5xx_it.c` — interrupt handlers. **One PLUDOS-critical edit
  lives here** (inside a user-code guard): the EXTI callback that routes the
  Wi-Fi module's "data ready" interrupt to the SPI semaphore. Don't remove it —
  Wi-Fi stops working without it.
- `Core/Src/stm32u5xx_hal_msp.c` — HAL peripheral init/de-init (clocks, pins).
- `Core/Src/system_stm32u5xx.c` — clock-tree boot setup.
- `Core/Src/syscalls.c`, `Core/Src/sysmem.c` — newlib glue (`printf`, heap).
- `Core/Startup/` — assembly reset vector / startup code.

## Project & build files (folder root)

- `PLUDOS_Edge_Node.ioc` — **the CubeMX project.** Source of truth for pins,
  clocks, and peripherals. **Read-only by hand** — open it in STM32CubeMX to
  change hardware config, then let it regenerate the `MX_*_Init` code.
- `STM32U585AIIXQ_FLASH.ld`, `STM32U585AIIXQ_RAM.ld` — linker scripts. Define
  the 768 KB main RAM / 16 KB SRAM4 split and the 2 MB flash map.
- `PLUDOS_Edge_Node Debug.launch` — STM32CubeIDE debug/flash configuration.
- `.settings/` — IDE project metadata.

## Vendor & build trees (treat as read-only black boxes)

- `Drivers/` — ST's HAL, the B-U585I-IOT02A **BSP**, the **mx_wifi** stack, and
  **CMSIS**. Third-party. We call into it; we don't edit it.
- `Debug/` — compiler output (`.o`, `.elf`, `.map`, generated makefiles). Pure
  build artifacts; safe to delete and rebuild.

## tools/

- `tools/coap_udp_monitor.py` — a small laptop-side Python listener for
  eyeballing the UDP/CoAP packets the firmware emits, without needing the full
  Jetson stack running. Pure dev/test aid. **Weight: one-off helper.**

## How it all connects

```
sensors.c (HTS221, LPS22HH)  ─┐
ISM330 read path (in main.c) ─┼─► main.c FSM (IDLE/MOVING)
                              │      │
                              │      ├─► PludosTelemetry_t (24 B) ─► UDP :5683 ─► Jetson
                              │      │
                              └──────┴─► PSRAM capture FIFO ─► drain ─► UDP :5684 ─► Jetson
boot: BEACON_Run() listens :5000 for "PLUDOS-GW:<ip>" to learn where to send
```

The 24-byte packet layout is the contract with `client/data-engine.py` on the
gateway — both sides must agree, and `docs/wire_protocol.md` is the spec.
If you change the struct in `main.c`, you must change the `struct.unpack`
format in `data-engine.py` (and the mock in `tools/mock_stm32.py`) to match.
