---
name: pludos-stm32-cubemx
description: Routes STM32 hardware-configuration changes back to STM32CubeMX/CubeIDE GUI rather than editing .ioc files or auto-generated init code. Use whenever the user wants to change pin assignments, peripheral configuration, clock tree, interrupt priorities, or any setting that lives in PLUDOS_Edge_Node.ioc. Also use when an error or new feature requires a new peripheral (ADC channel, additional I2C device, TIM, DMA, EXTI, etc.) on the B-U585I-IOT02A. Do NOT use this skill for application logic that lives inside USER CODE BEGIN/END guards.
---

# PLUDOS STM32CubeMX Boundary Skill

The PLUDOS firmware is generated from `STM_Shuttles/PLUDOS_Edge_Node/PLUDOS_Edge_Node.ioc`
by STM32CubeMX (or its embedded CubeIDE equivalent). The IDE regenerates
`.c`/`.h` files from the `.ioc` whenever the user clicks "Generate Code"
or saves the `.ioc`. Anything you put outside the `USER CODE BEGIN/END`
guards in those files is OVERWRITTEN on next regeneration.

This skill keeps you on the safe side of that boundary.

## What you must NOT touch directly

These are CubeMX territory. If a change is needed here, route to the user.

- `*.ioc` files — XML-like project state, do not edit by hand
- `Core/Src/main.c::SystemClock_Config()` — clock tree
- `Core/Src/main.c::SystemPower_Config()` — power supply config
- `Core/Src/main.c::MX_*_Init()` — peripheral init (I2C, SPI, UART, ADC,
  TIM, OCTOSPI, USB, etc.)
- `Core/Src/main.c::MX_GPIO_Init()` — pin modes outside USER CODE blocks
- `Core/Src/stm32u5xx_hal_msp.c` — peripheral MSP init/deinit (clocks,
  GPIO mode, NVIC priorities)
- `Core/Src/stm32u5xx_it.c` outside USER CODE blocks — interrupt vectors
- Linker script `STM32U585AIIXQ_FLASH.ld` — generated, but may be edited
  by the user manually for memory tuning. Don't edit unless asked.
- `Core/Inc/stm32u5xx_hal_conf.h` — HAL module enables; CubeMX manages
- Pin defines in `Core/Inc/main.h` (the long block of `#define X_Pin`
  and `X_GPIO_Port` lines) — generated from CubeMX pinout

## What you CAN edit freely

- Inside `USER CODE BEGIN/END` guards anywhere in the generated files
- Any new `.c`/`.h` file you or the user creates outside that pattern
- Application-level logic in `main.c` — the FSM, sensor sampling loop,
  CoAP packet building, buffer management
- New header includes, new function definitions, new typedefs (place
  them inside USER CODE blocks)

## Symptoms that mean "STOP, this needs CubeMX"

When you see any of these, do not start editing C code. Stop and route.

- "I need to add a new I2C/SPI/UART/ADC/TIM peripheral"
- "I need to change the clock frequency / oscillator source"
- "I need to enable an interrupt I haven't enabled before"
- "I need to change a pin's mode (input/output/AF/analog)"
- "I need to add DMA to an existing peripheral"
- "I need to change the I2C clock speed"
- "I need to enable a HAL module that's currently `#define`d off"
- "I need to add a new GPIO output for a sensor"

## Routing template

When you detect a CubeMX-side change, output something like:

```
This change needs to happen in STM32CubeMX, not in source code.
Editing the generated `.c` files directly will work until you next
regenerate from the .ioc, then your edits will be wiped.

Please:
1. Open `STM_Shuttles/PLUDOS_Edge_Node/PLUDOS_Edge_Node.ioc` in
   STM32CubeIDE (or standalone CubeMX).
2. <specific instructions for the change>
3. Save (`Ctrl+S`) — confirm "Generate Code" when prompted.
4. Build the project.
5. Tell me when done; I'll write the application logic that uses
   the new peripheral.
```

Replace `<specific instructions>` with concrete CubeMX UI steps. Use
the user's CubeMX vocabulary (Pinout & Configuration view, Categories,
the peripheral name in the left tree, the parameter name in the right
panel). Don't paraphrase; cite exact menu paths.

## Examples

### Example 1 — adding ADC for power measurement

User: "We need to actually read the ADC for power instead of the
150 mW placeholder."

Wrong response: editing `main.c` to call `HAL_ADC_Start()` and
`HAL_ADC_GetValue()` directly. The ADC peripheral is not configured
in the current `.ioc` (check `MX_ADC*_Init` — there isn't one). You'd
fail at compile or at runtime, depending on which symbol you reach
for first.

Right response:

```
The ADC isn't configured in PLUDOS_Edge_Node.ioc yet. Before I can
write the read-and-convert logic, you need to enable it in CubeMX.

In STM32CubeIDE:
1. Open PLUDOS_Edge_Node.ioc.
2. Pinout & Configuration → Analog → ADC1 (or ADC4 for low-power).
3. Enable IN<n> Single-ended on the pin connected to your power
   sensor — confirm pin assignment against your schematic.
4. In Parameter Settings, set:
   - Resolution: 12 bits
   - Continuous Conversion Mode: Disabled (we trigger per sample)
   - End of Conversion Selection: EOC flag at the end of single channel
5. Save (Ctrl+S), confirm Generate Code.
6. Build to verify MX_ADC1_Init() now exists in main.c.

Tell me when done. I'll then write the application-level
`ADC_ReadPowerMilliwatts()` function and wire it into the sensor
sampling loop.
```

### Example 2 — changing CoAP retry timeout

User: "Bump the CoAP timeout from 2 seconds to 5."

This is application logic. It lives in `COAP_ACK_TIMEOUT_MS` in
`main.c`. Edit freely. Do NOT route to CubeMX.

### Example 3 — adding the LIS2MDL magnetometer

User: "I want to start using the magnetometer too."

The magnetometer is on I2C2 (already configured), so the bus exists,
but the driver, the sensor's I2C address, and any INT pin handling
need to be added. Mixed:

- I2C2 itself: already in `.ioc`, no change needed.
- INT pin (if used): if it's not already enabled as an EXTI input
  in `.ioc`, that's a CubeMX change.
- Application driver code: brand new file, edit freely.

If unsure whether a given pin is already configured, ask the user to
check the `.ioc` Pinout view, or grep `Core/Inc/main.h` for the pin's
defines.

## Anti-pattern

Do not "be helpful" by editing `MX_I2C2_Init()` to add a delay or
change clock timing. The user would fix that for one build, then lose
it on the next CubeMX regen, and waste an evening figuring out why
their I2C broke again. Routing through CubeMX is the cheap path.
