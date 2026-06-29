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

/* ADR-020 crash-recovery: reserve the top of PSRAM for a CRC-validated capture
 * index that survives an MCU reset (IWDG/brownout). PSRAM keeps its contents
 * across a core reset (the chip is externally powered), so a post-reset boot can
 * rediscover sealed-but-undrained captures instead of losing them with the
 * volatile SRAM bookkeeping. The capture data ring uses only PSRAM_USABLE_BYTES
 * and never writes into the reserved region. */
#define PSRAM_PERSIST_BYTES  0x00004000UL                              /* 16 KB reserved index region */

/* ADR-019 OTA staging: carve a fixed window below the persist index where a
 * downloaded firmware image is assembled and CRC-gated before any flash is
 * touched. OTA runs only in IDLE (ring quiescent), but the carve-out is explicit
 * so the capture ring write path can never wrap into it. 512 KB >> current .bin
 * (~100 KB); shrinks the usable ring from 8 MB to ~7.5 MB. */
#define PSRAM_OTA_STAGE_BYTES 0x00080000UL                             /* 512 KB OTA image staging */

#define PSRAM_USABLE_BYTES   (PSRAM_SIZE_BYTES - PSRAM_PERSIST_BYTES - PSRAM_OTA_STAGE_BYTES)  /* capture ring size */
#define PSRAM_OTA_STAGE_ADDR (PSRAM_BASE_ADDR + PSRAM_USABLE_BYTES)    /* OTA staging base, just below persist index */
#define PSRAM_PERSIST_ADDR   (PSRAM_OTA_STAGE_ADDR + PSRAM_OTA_STAGE_BYTES) /* index region base address (top of PSRAM) */

/* Configure APS6408 mode registers and switch OCTOSPI1 to memory-mapped mode.
 * Must be called after MX_OCTOSPI1_Init(). Returns 0 on success, -1 on error. */
int8_t PSRAM_Init(void);

/* Write/read-back self-test of the memory-mapped region. Validates data
 * integrity (dense block) and address wiring (per-page stride). Destroys any
 * existing PSRAM contents. Returns 0 if all checks pass, -1 on first mismatch. */
int8_t PSRAM_SelfTest(void);

#endif /* PSRAM_H */
