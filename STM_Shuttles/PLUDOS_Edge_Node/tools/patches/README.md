# STM32 Schema & Logic Migration Patches

## Overview

These Python scripts document **one-time code transformations** applied to `STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c`. They are **reference material** and **not runtime dependencies** — the changes they describe have already been integrated into the codebase.

---

## Files

### **1. patch_main.py** (Structural Transformation)

**What it transforms:**
- Old struct field names → new field names for chronological tracking
  - `packet_num` → `sequence_id`
  - `tick_ms` → `relative_tick_count`
  - `vib_x/y/z` → `accel_x/y/z`
  - Adds: `adc_power_mw` (power measurement in milliwatts)

- JSON payload encoding → binary struct encoding
  - Old: `snprintf()` generating JSON strings (~200 bytes)
  - New: `memcpy()` binary struct (`CriticalPayload` = 39 bytes)
  - CoAP Content-Format: 50 (JSON) → 42 (application/octet-stream)

- Adds two new payload structs:
  - `CriticalPayload`: Binary CoAP payload (39 bytes, confirmable)
  - `NonCriticalPayload`: UDP payload for non-critical telemetry (8 bytes, best-effort)

**When you'd use it:**
- If main.c was reset from STM32CubeIDE defaults and you need to reapply the old field names and binary encoding
- **Current state:** Already applied ✅

**How to run (if needed):**
```bash
cd /path/to/PLUDOS
python3 STM_Shuttles/PLUDOS_Edge_Node/tools/patches/patch_main.py
```

---

### **2. patch_main_logic.py** (Behavioral Enhancements)

**What it patches:**

1. **RFC 7252 Binary Exponential Backoff**
   - Replaces simple retry loop with spec-compliant timeout escalation
   - Timer sequence: 2s → 4s → 8s → 16s (doubles each retry, max 4 attempts)
   - Updates `NETWORK_ConfigureUdpSocket()` signature to accept dynamic timeout

2. **500ms Continuous Threshold (State Machine)**
   - Old: Single spike in acceleration triggers `STATE_MOVING`
   - New: Acceleration must stay **above threshold for 500ms continuous** before transition
   - Prevents false positives from transient shocks
   - Logic: `continuous_movement_start_tick` tracks onset; transition fires only after 500ms elapsed

3. **Memory Protection (SRAM Overflow Safeguard)**
   - Monitors SRAM buffer usage during `STATE_MOVING`
   - If buffer reaches 95%: **immediately suspend** ADC/I2C sampling
   - Preserves existing buffer (no data loss)
   - Resumes sampling only after returning to `STATE_IDLE` AND successfully flushing CoAP payload

4. **UDP Non-Critical Path**
   - Adds `UDP_SendNonCritical()` for temperature/humidity (non-critical telemetry)
   - Fires during `STATE_IDLE` only (low-priority background transmission)

**When you'd use it:**
- If state machine logic was lost and you need to restore advanced behaviors
- **Current state:** Already applied ✅

**How to run (if needed):**
```bash
cd /path/to/PLUDOS
python3 STM_Shuttles/PLUDOS_Edge_Node/tools/patches/patch_main_logic.py
```

---

## Current Integration Status

| Component | Status | Location |
|-----------|--------|----------|
| Struct redefinition (patch_main.py) | ✅ Applied | `main.c` lines ~40–60 |
| Binary payload encoding (patch_main.py) | ✅ Applied | `main.c` SENSOR_BuildBatchPayload() |
| CriticalPayload struct | ✅ Applied | `main.c` lines ~46–54 |
| NonCriticalPayload struct | ✅ Applied | `main.c` lines ~56–61 |
| RFC 7252 backoff (patch_main_logic.py) | ✅ Applied | `main.c` COAP_SendBufferedBatch(), lines ~564–603 |
| 500ms continuous threshold (patch_main_logic.py) | ✅ Applied | `main.c` main loop, lines ~876–941 |
| Memory protection suspend_sampling (patch_main_logic.py) | ✅ Applied | `main.c` main loop, lines ~877–905 |
| UDP_SendNonCritical() (patch_main_logic.py) | ✅ Applied | `main.c` lines ~621–637 |

---

## Design Philosophy

These patches enforce **PLUDOS system constraints**:

1. **Payload minimization:** 39 bytes binary vs. ~200 bytes JSON → 81% size reduction
2. **Temporal accuracy:** `sequence_id` + `relative_tick_count` enables Jetson to backdating absolute timestamps via NTP offset calculation
3. **Reliability:** RFC 7252 backoff + 500ms threshold prevent transient failures and false state transitions
4. **Memory safety:** Bare-metal STM32 has ~256KB SRAM; 95% safeguard prevents buffer overflow crashes
5. **Dual-path telemetry:** CoAP (confirmable, critical) + UDP (best-effort, non-critical) separates concerns

---

## For Developers

**Do NOT:**
- Apply these patches twice (code is already updated)
- Manually edit patch_main.py or patch_main_logic.py
- Use these as templates for arbitrary code modifications

**If main.c gets corrupted/reset:**
1. Restore from Git: `git checkout STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c`
2. Or run patches in sequence:
   ```bash
   python3 patch_main.py
   python3 patch_main_logic.py
   ```

**If you need to understand the changes:**
- Read the comments in the patch files (they explain the "why")
- Compare patch output to current main.c to see before/after

---

## Quick Reference: What These Patches Enable

| Feature | Enabled By |
|---------|-----------|
| Chronological data ordering | `sequence_id` (patch_main.py) |
| Jetson NTP offset calculation | `relative_tick_count` + absolute timestamp injection (patch_main.py) |
| 81% payload size reduction | Binary encoding (patch_main.py) |
| CoAP reliability | RFC 7252 backoff (patch_main_logic.py) |
| False-positive prevention | 500ms continuous threshold (patch_main_logic.py) |
| Memory safety | suspend_sampling at 95% (patch_main_logic.py) |
| Non-critical telemetry | UDP_SendNonCritical() (patch_main_logic.py) |
