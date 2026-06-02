#ifndef PSRAM_H
#define PSRAM_H

#include "stm32u5xx_hal.h"

/* 8 MB AP-Memory APS6408 Octal PSRAM on OCTOSPI1, memory-mapped at 0x90000000.
 * Used by the ADR-020 capture buffer: one mission is staged here, then drained
 * to the gateway during IDLE. CubeMX brings up hospi1 (clock/GPIO/DLYB); this
 * module finishes the device-side register config and enters memory-mapped mode
 * so the region is addressable as ordinary RAM. */

/* Base address of the memory-mapped PSRAM window (OCTOSPI1 AHB region). */
#define PSRAM_BASE_ADDR   0x90000000UL

/* Usable PSRAM size in bytes (APS6408 = 64 Mbit = 8 MB). */
#define PSRAM_SIZE_BYTES  0x00800000UL

/* Configure APS6408 mode registers and switch OCTOSPI1 to memory-mapped mode.
 * Must be called after MX_OCTOSPI1_Init(). Returns 0 on success, -1 on error. */
int8_t PSRAM_Init(void);

/* Write/read-back self-test of the memory-mapped region. Validates data
 * integrity (dense block) and address wiring (per-page stride). Destroys any
 * existing PSRAM contents. Returns 0 if all checks pass, -1 on first mismatch. */
int8_t PSRAM_SelfTest(void);

#endif /* PSRAM_H */
