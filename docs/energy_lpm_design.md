# Shuttle Low-Power Design — Idle Stop2 + ISM330 Wake-on-Motion

**Status:** CubeMX change applied (C1–C3, confirmed in regenerated source) and the
Stop2 + wake-on-motion firmware is **implemented** behind the `STOP2_IDLE_ENABLE`
compile gate (`main.c`, `stm32u5xx_it.c`; see CHANGELOG "Phase 3"), and is safe to flash
today: the device wakes every 14 s to kick the IWDG, so the `IWDG_STOP` option byte
(Step C) is now only a power optimization, not a prerequisite. Remaining follow-ups:
(a) a bench current measurement to quantify the saving, and (b) optionally Step C to let
the wake period grow. B1 (free-running RTC time base) was found unnecessary — see §3
note. Closes the *design* and *implementation* halves of DESIGN_COUNCIL item 1; the
*measurement* remains a follow-up.

This is the firmware counterpart to the gateway energy work (Alumet/INA3221 on the
Jetson, ADR-011). The Jetson INA3221 measures the **gateway**; it says nothing about
the **shuttle**. The shuttle has no on-board power telemetry exposed to firmware, so
shuttle energy must be measured externally.

---

## 1. Why this matters

The MCU runs flat-out at 160 MHz and **never sleeps**. The main loop polls the
ISM330 over I2C with `HAL_Delay` busy-waits through the entire idle period — and the
idle period is large: an idle snapshot fires only every `CAP_IDLE_SNAP_PERIOD_MS`
(600 000 ms = 10 min, `main.c`), so the device spends ~9 min 50 s out of every
10 min doing nothing but polling at full clock.

For an *energy-aware* thesis this is internally inconsistent. The in-code idle-power
assumption implies ~27 days of battery; observed battery life is **1–2 days**
(project memory) — an order-of-magnitude gap. **Every shuttle energy/battery number
is unmeasured today.** Per the project rule "don't invent numbers," the figure is
`unknown` until the bench measurement below exists.

---

## 2. Measurement procedure (REAL numbers — blocked on bench)

Goal: a measured idle-current baseline (polling, today) and a re-measure after Stop2
lands, on the actual B-U585I-IOT02A.

1. **Find the current-measurement point.** The B-U585I-IOT02A provides an on-board
   IDD / supply-current measurement provision. Look up the exact jumper/header
   designator and the measurement procedure in **UM2839 (B-U585I-IOT02A User
   Manual), "current consumption measurement" section** — do not assume a
   designator. If the board variant doesn't expose a usable point, fall back to an
   external ammeter / INA on the 3.3 V supply rail.
2. **Baseline (today's firmware):** power the board, let it settle in IDLE between
   snapshots, record mean current and supply voltage → idle power. Note ambient and
   that WiFi is idle (radio off outside drain windows, ADR-021).
3. **Record the snapshot/drain transients** separately (10 s snapshot every 10 min;
   the periodic drain burst) so the idle baseline isn't polluted by active windows.
4. **Re-measure after Stop2 lands** (§3) to quantify the saving.

Report every figure with the NVPModel-equivalent context (here: supply voltage,
ambient, firmware commit). Mark anything not yet measured as `unknown`.

---

## 3. Target architecture — Stop2 with ISM330 wake-on-motion

Replace the busy-poll idle with a sleep that the IMU itself wakes from:

- **Idle gap → Stop2.** Between snapshots, with no motion, enter **Stop2** (deepest
  mode that retains SRAM + allows fast wake; see RM0456 low-power modes). PSRAM
  contents are externally powered and already survive resets, so a Stop2 that
  retains the capture bookkeeping (or restores it from the CRC-validated PSRAM index,
  ADR-021) is safe.
- **Wake source 1 — motion:** the ISM330DHCX **wake-up / activity interrupt** on
  **INT1**, routed to an EXTI line, wakes the MCU the instant the shuttle starts
  moving. This removes idle polling entirely: the IMU watches for motion in hardware
  at µA-level, the M33 sleeps. (See ISM330DHCX datasheet — activity/inactivity and
  wake-up interrupt registers; threshold + duration are register-configurable and
  should be set conservatively below `MOVEMENT_THRESHOLD_G2`'s motion floor so the
  FSM still does the authoritative IDLE→MOVING decision after wake.)
- **Wake source 2 — snapshot cadence:** an **RTC wake-up timer** fires every
  `CAP_IDLE_SNAP_PERIOD_MS` so the periodic idle snapshot still happens under Stop2.
  Note `hardware_refs.md`: the board has **no RTC crystal/battery by default** — the
  LSI/LSE situation for an RTC wake-up timer must be checked as part of the CubeMX
  change; if no suitable RTC clock is available, the snapshot cadence is the gating
  constraint on how deep idle sleep can go.
- **On wake:** restore clocks (`SystemClock_Config` path), re-init only what Stop2
  dropped, resume the FSM. The accel ODR is left unchanged across sleep, so in the
  implemented path the LPF2 does not reset and no `ACCEL_SETTLE_MS` blank is needed on
  wake (the guard still applies on the ODR changes at snapshot entry/exit).

> **B1 resolved — no free-running RTC time base needed.** The original concern was that
> `HAL_GetTick` freezes in Stop2 and would corrupt capture timestamps. It does not: the
> gateway reconstructs wall-clock from the *intra-capture* delta `tx_tick − t0_tick`, and
> every capture runs fully awake (Stop2 only sleeps in the gap *between* captures, where
> both ticks are re-stamped after wake). The RTC is therefore needed only as the
> snapshot-cadence **wake source**, not as a clock.

### Energy intuition (qualitative — not a measured claim)

Eliminating 160 MHz polling across ~98 % of wall-clock should dominate the idle
budget; whether the IMU rail or the I2C bus dominates the residual is exactly what
the §2 measurement must settle (this is the open question parked in DESIGN_COUNCIL
§4.4 — decide the idle snapshot rate *after* the number exists, not before).

---

## 4. Required CubeMX changes — OWNER ACTION (do not edit `.ioc` from code)

Per the project hard rule, the following must be made in STM32CubeMX/CubeIDE by the
owner, then regenerated; firmware logic resumes inside `USER CODE` guards afterward.

Pins/lines below are **confirmed from the current `PLUDOS_Edge_Node.ioc`**, not assumed:
ISM330 INT1 is on **PE11** (`PE11.GPIO_Label=Mems.ISM330DLC_INT1`, currently
`GPIO_Input`, `Locked=true`); the MXCHIP WiFi uses **EXTI14 (PD14)** and **EXTI15
(PG15)**, so PE11→**EXTI11** does not collide. **LSE** is wired (PC14/PC15 =
`LSE-External-Oscillator`) and **LSI** is enabled (32 kHz); **no RTC is configured yet**
(0 references in the `.ioc`).

### C1 — ISM330 INT1 (PE11) → EXTI11, rising edge

1. Pinout: set **PE11** mode `GPIO_Input` → **`GPIO_EXTI11`** (keep the `Locked` pin and
   the `Mems.ISM330DLC_INT1` label).
2. System Core → GPIO → PE11: **External Interrupt Mode with Rising edge trigger**;
   **Pull-down** (ISM330 INT1 is active-high push-pull → idles low, clean rising edge).
3. System Core → NVIC: enable **`EXTI Line11 interrupt`** (`EXTI11_IRQn`), priority **5**
   to match the MXCHIP EXTI14/15 lines. EXTI11 has its own IRQ on U5 → it does **not**
   touch the WiFi SPI-semaphore path. Leave EXTI14/EXTI15 exactly as-is.

EXTI lines 0–15 are valid Stop2 wake sources on STM32U5 — routing INT1→EXTI11 is enough,
no extra "wake" checkbox.

### C2 — Stop2 / PWR

**No new `.ioc` change.** `MX_PWR_Init` is already in the init list, and Stop2 entry/exit
is a runtime HAL call (`HAL_PWREx_EnterSTOP2Mode`) written in `USER CODE` — not a CubeMX
checkbox. Just keep PWR enabled. Clock restore on wake = re-call the regenerated
`SystemClock_Config()` in code.

### C3 — RTC + wake-up timer (10-min snapshot cadence)

**Clock-source decision: LSE (decided).** LSE is already enabled in the running firmware
(PC14/PC15 = `LSE-External-Oscillator`) and the board boots past `SystemClock_Config`
without hitting `Error_Handler`, so the crystal is present and locking. LSE gives an
accurate time base while `HAL_GetTick` is frozen in Stop2 (solves blocker B1). After
regeneration, confirm telemetry still streams (sanity that LSE still locks). LSI (32 kHz,
±~5%) is the fallback only if LSE ever fails to lock.

**Steps:**
1. Timers → RTC → Mode: check **`Activate Clock Source`** + **`Activate Calendar`**.
2. Clock Configuration tab: set **RTC/wakeup clock mux → LSE** (or LSI per the decision).
3. RTC → NVIC Settings: enable **`RTC wake-up interrupt through EXTI line`** (`RTC_IRQn`),
   priority 5. The wake-up *period* is **not** a CubeMX field — it is started in `USER
   CODE` via `HAL_RTCEx_SetWakeUpTimer_IT(...)` for `CAP_IDLE_SNAP_PERIOD_MS`.

### C4 — Keep the MXCHIP EXTI routing fix intact

Adding EXTI11 must not disturb the `stm32u5xx_it.c::HAL_GPIO_EXTI_Rising_Callback` path
for EXTI14/15 (WiFi SPI semaphore). Different IRQ lines, so safe — but re-check after
regeneration.

Once C1–C3 are regenerated, the sleep/wake state logic (Stop2 entry on confirmed idle,
EXTI11/RTC wake handling, ISM330 wake-up register config — `INT1_CTRL`, `WAKE_UP_THS`,
`WAKE_UP_DUR`, `MD1_CFG`, all pure I2C) is written in `USER CODE` in a follow-up — no
further `.ioc` change needed if the INT1 EXTI line is wired. The ISM330 wake threshold is
set below `MOVEMENT_THRESHOLD_G2` so the FSM still makes the authoritative IDLE→MOVING
decision after wake.

---

## 5. Blocked-on summary

| Sub-task | Who | Blocker |
|----------|-----|---------|
| Idle-current baseline | owner (bench) | UM2839 IDD point / external ammeter |
| C1 PE11→EXTI11 + C3 RTC/wake config (RTC mux = **LSE**) | owner (CubeMX) | **DONE** — confirmed in `.ioc` + regenerated source |
| `IWDG_STOP` option byte = Freeze in Stop (decision D2=a) | owner (CubeProgrammer) | **optional power win** — firmware wakes every 14 s to kick the dog, so safe without it; setting Freeze lets the wake period grow to ~10 min (Option Bytes → User Config → IWDG_STOP → Freeze) |
| Sleep/wake firmware logic | agent | **DONE** — behind `STOP2_IDLE_ENABLE` (CHANGELOG Phase 3) |
| Idle snapshot rate decision (§4.4) | owner | kept at 10 s / 10 min (D3); revisit after bench number |

References: RM0456 (low-power modes, RTC), UM2839 (board schematic + current
measurement), ISM330DHCX datasheet (wake-up/activity interrupt registers).
