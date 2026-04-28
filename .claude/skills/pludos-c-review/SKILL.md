---
name: pludos-c-review
description: Reviews STM32 firmware C code for the PLUDOS project against project-specific conventions. Use whenever the user asks to review, audit, or check a .c or .h file from STM_Shuttles/PLUDOS_Edge_Node. Also triggers automatically on C code review requests mentioning the STM32, firmware, HAL, or sensor sampling code.
---

# PLUDOS STM32 C Code Review Skill

Reviews firmware code against PLUDOS conventions and embedded safety rules.

## Review checklist

Run through these in order. Flag any violation with severity (CRITICAL / WARNING / NOTE).

### 1. Dynamic memory (CRITICAL if present)
- Any `malloc`, `calloc`, `realloc`, `free` in application code → CRITICAL.
- Exception: vendored `cJSON` in dev/test paths only.
- Fix: replace with static arrays or static pool allocators.

### 2. CubeMX boundary (CRITICAL if violated)
- Code edited outside `USER CODE BEGIN/END` guards in generated files → CRITICAL.
- `MX_*_Init`, `SystemClock_Config`, `HAL_*_MspInit/DeInit` called or modified directly → CRITICAL.
- Route all peripheral/pin/clock changes to CubeMX. Invoke `pludos-stm32-cubemx` skill.

### 3. HAL/LL mixing (WARNING)
- New code using `LL_*` calls while the rest of the module uses `HAL_*` → WARNING.
- Exception: CubeMX-generated UCPD/USB init using `LL_*` is grandfathered.

### 4. State machine thresholds
- STATE_MOVE trigger: accelerometer threshold exceeded CONTINUOUSLY for 500 ms → verify.
- STATE_IDLE trigger: 10 s continuous zero-movement → verify.
- SRAM suspend at 95% fill, flush trigger at 70% → verify constants match `#define`s.

### 5. Buffer safety
- `sensor_buffer` bounds: never write past `SENSOR_BUFFER_SIZE` (256 entries).
- Flush triggered at 70% (179 entries), suspend at 95% (243 entries).
- No off-by-one on `buffer_head` / `buffer_tail` indices.

### 6. CoAP payload
- `CriticalPayload` struct: 39 bytes, packed, includes `sequence_id`, `shuttle_id`,
  `relative_tick_count`, `mission_active` flag. Verify any change keeps this layout.
- Manual application-layer retry (2s/4s/8s/16s, max 4 attempts) — note in review
  that this contradicts the "rely on RFC 7252 backoff" design intent (tracked as
  known divergence in `architecture.md`).

### 7. Code style
- All magic numbers replaced with `#define` constants.
- Log tags follow `[MODULE]` pattern: `[NETWORK]`, `[SENSOR]`, `[BUFFER]`, etc.
- Comments explain WHY, not WHAT.
- No commented-out dead code blocks.
- Function names: `VerbNoun` or `verb_noun` consistent within the file.

### 8. WiFi / EXTI ISR
- `HAL_GPIO_EXTI_Rising_Callback` in `stm32u5xx_it.c`: must include the SPI
  semaphore signal for MXCHIP. Do NOT remove — see `docs/WIFI_FIX_AND_BUILD.md`.

### 9. Credential hygiene
- `WIFI_SSID` / `WIFI_PASSWORD` committed in `main.c` → WARNING. Should move to
  an `#include "wifi_credentials.h"` with that header gitignored.

## Output format

```
## C Review: <filename>

### CRITICAL
- [line X] <issue> — <fix>

### WARNING
- [line X] <issue> — <fix>

### NOTE
- [line X] <observation>

### Summary
<1–3 sentences on overall quality and most important action>
```

If no issues found in a category, omit that section.
