# PLUDOS Edge Node: WiFi Fix & Build Guide

## The WiFi Initialization Bug

The STM32U585I-IOT02A was hanging indefinitely during the MXCHIP EMW3080 WiFi initialization at `MX_WIFI_Init()`.
The root cause was a missing GPIO interrupt routing mechanism. The MXCHIP module signals the STM32 via `WRLS_NOTIFY_Pin` (EXTI14) and `WRLS_FLOW_Pin` (EXTI15) when SPI transfers complete. Without a callback, the WiFi driver (`mx_wifi_spi.c`) never received these signals and hung waiting for the SPI semaphore.

### The Fix

The fix involved adding the `HAL_GPIO_EXTI_Rising_Callback` to `Core/Src/stm32u5xx_it.c` to properly route the hardware interrupts to the WiFi driver.

```c
// Added to stm32u5xx_it.c
extern void mxchip_WIFI_ISR(uint16_t isr_source);

void HAL_GPIO_EXTI_Rising_Callback(uint16_t GPIO_Pin)
{
  if (GPIO_Pin == WRLS_NOTIFY_Pin || GPIO_Pin == WRLS_FLOW_Pin)
  {
    mxchip_WIFI_ISR(GPIO_Pin); // Signal SPI TX/RX semaphore
  }
}
```

Additionally, `mxchip_WIFI_ISR` was forward-declared in `Core/Inc/main.h`, and `main.c` was updated with detailed UART logging to track initialization timings.

## Build Instructions

### Method 1: Command Line (Linux)
```bash
cd STM_Shuttles/PLUDOS_Edge_Node/Debug
make clean
make -j4
```
Ensure you have `arm-none-eabi-gcc` installed.

### Method 2: STM32CubeIDE
Open the project in STM32CubeIDE, right-click the project, and select **Build Project**.

All linking and compilation errors have been resolved. The `.elf` file will be generated and can be flashed to the device via ST-Link.
