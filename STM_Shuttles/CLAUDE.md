# CLAUDE.md — STM_Shuttles/PLUDOS_Edge_Node

This is the STM32U585 firmware tree. The root `CLAUDE.md` applies; this
file adds firmware-specific rules.

## What this tree is

CubeMX-generated STM32CubeIDE project for the B-U585I-IOT02A. The
microcontroller acts as the "extreme edge" PLUDOS device on each shuttle.
Bare-metal C, HAL drivers, no RTOS. Built via `make` in `Debug/` or via
the IDE.

## CubeMX boundaries

`PLUDOS_Edge_Node.ioc` is the CubeMX project file. Treat it as read-only.
If a peripheral, clock, pin, or interrupt needs changing:

1. STOP. Don't edit `.ioc`, don't edit clock-config code, don't edit
   `MX_*_Init` peripheral functions, don't edit `stm32u5xx_hal_msp.c`.
2. Tell me what needs to change and why.
3. I open the `.ioc` in STM32CubeIDE, make the change, regenerate, build.
4. You resume in the regenerated source, applying logic inside the
   `USER CODE BEGIN/END` guards.

`MX_*_Init` functions, `SystemClock_Config`, `HAL_*_MspInit/DeInit` are
all CubeMX territory. Application logic lives in user-code guards or in
new files outside `Core/`.

## Hot zones (most-edited application code)

- `Core/Src/main.c` user-code sections — sensor loop, FSM, CoAP path
- `Core/Src/stm32u5xx_it.c` user-code sections — EXTI callbacks for
  the MXCHIP WiFi ISR routing
- New `.c`/`.h` files added under `Core/Src/` and `Core/Inc/` — these
  are not CubeMX-generated, edit freely

## Sensor inventory on the IOT02A board

The board has more sensors than the firmware currently uses. Reference
when planning extensions:

- ISM330 (I2C2, addr 0x6A << 1) — 6-axis IMU, **currently used**
- LIS2MDL — 3-axis magnetometer (I2C2)
- LPS22HH — pressure (I2C2, INT pin PG2)
- HTS221 / SHT41 — temperature + humidity (I2C2)
- VL53L5CX — time-of-flight + gesture (I2C, XSHUT on PH1)
- 2x digital MEMS microphones (MDF1 SDI0/SDI1)
- Ambient light sensor

Currently-unused sensors are not connected in firmware. Adding any of
them needs a CubeMX-side I2C/peripheral configuration first.

## Network module

MXCHIP EMW3080 over SPI2. 2.4 GHz Wi-Fi only (don't propose 5 GHz).
Uses ST `mx_wifi` BSP, AT-command-style IPC, bare-metal mode
(`MX_WIFI_USE_CMSIS_OS = 0`). The EXTI ISR routing fix in
`stm32u5xx_it.c::HAL_GPIO_EXTI_Rising_Callback` is required for the
SPI semaphore signalling — don't remove it.

## SRAM and flash budget

- 786 KB total SRAM. Linker allocates 768 KB to main RAM, 16 KB to
  SRAM4 (backup domain). See `STM32U585AIIXQ_FLASH.ld`.
- 2 MB Flash. Currently using a small fraction.
- No dynamic allocation in application code (rule from root CLAUDE.md).
- `_Min_Heap_Size = 0x1000` (4 KB), `_Min_Stack_Size = 0x400` (1 KB) in
  the linker script. Stack is for ISRs and brief function calls; the
  heap is reserved for newlib internals (e.g. `printf` formatting),
  not for application use.

## Build

- IDE: STM32CubeIDE, right-click project → Build.
- CLI: `cd Debug && make clean && make -j4`.
- Toolchain: `arm-none-eabi-gcc`. Flash to board via ST-Link.
- See `docs/QUICK_REFERENCE.md` and `docs/WIFI_FIX_AND_BUILD.md` for the
  WiFi-init bug history.

## Testing

`tools/coap_udp_monitor.py` is a minimal Python listener for verifying
CoAP packets the firmware emits. Run it on a laptop on the same network
as the shuttle to inspect traffic without needing the full Jetson stack.
The wire protocol it expects is in `@docs/wire_protocol.md`.

## C commenting rules

Use `/* ... */` for function headers and block comments, `//` for
inline end-of-line notes. One line per function describing what it does
or the constraint it respects. Examples:

```c
/* Sample accelerometer at 50 Hz; returns 0 on success, -1 on I2C error. */
int sensor_sample_accel(SensorSample_t *out) { ... }

buffer_head = (buffer_head + 1) % SENSOR_BUFFER_SIZE; /* ring wrap */
HAL_Delay(2); /* EMW3080 SPI de-assert hold time per datasheet §4.3 */
```

Mark CubeMX-generated regions clearly:
```c
/* USER CODE BEGIN 0 */
/* your code here */
/* USER CODE END 0 */
```
Never add comments outside these guards in generated files — they will
be wiped on the next CubeMX regeneration.

## Skill triggers

When you see firmware C code, the `pludos-c-review` skill applies.
When you're about to suggest editing pins, peripherals, clocks, or
interrupt vectors, the `pludos-stm32-cubemx` skill applies.
