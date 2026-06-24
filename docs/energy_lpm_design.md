# Shuttle Low-Power Design — Idle Stop2 + ISM330 Wake-on-Motion

**Status:** design spec only. Blocked on (a) a bench current measurement and (b) a
CubeMX peripheral/clock change. No firmware is written yet. Closes the *design* half
of DESIGN_COUNCIL item 1 (and the power half of item 6); the *measurement* and the
*implementation* are follow-ups the project owner must unblock.

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
  dropped, resume the FSM. The first post-wake samples must respect the existing
  `ACCEL_SETTLE_MS` LPF2-settle guard.

### Energy intuition (qualitative — not a measured claim)

Eliminating 160 MHz polling across ~98 % of wall-clock should dominate the idle
budget; whether the IMU rail or the I2C bus dominates the residual is exactly what
the §2 measurement must settle (this is the open question parked in DESIGN_COUNCIL
§4.4 — decide the idle snapshot rate *after* the number exists, not before).

---

## 4. Required CubeMX changes — OWNER ACTION (do not edit `.ioc` from code)

Per the project hard rule, the following must be made in STM32CubeMX/CubeIDE by the
owner, then regenerated; firmware logic resumes inside `USER CODE` guards afterward:

1. **ISM330 INT1 pin → EXTI**, configured as a **Stop2 wake source** (rising edge).
   Confirm which MCU pin the board routes ISM330 INT1 to (UM2839 schematic).
2. **Low-power mode** entry/exit support for **Stop2** (PWR config); verify the clock
   tree restores on exit.
3. **RTC + RTC wake-up timer** for the 10-min snapshot cadence under Stop2 —
   contingent on a usable RTC clock source (see no-crystal caveat above).
4. Keep the existing **MXCHIP EXTI routing fix** (`stm32u5xx_it.c`) intact — adding
   a second EXTI source must not disturb the WiFi SPI semaphore path.

Once (1)–(3) are regenerated, the sleep/wake state logic (Stop2 entry on confirmed
idle, INT1/RTC wake handling, ISM330 wake-up register config) can be written in
`USER CODE` in a follow-up — no further `.ioc` change needed for the register writes
if the INT1 EXTI line is wired.

---

## 5. Blocked-on summary

| Sub-task | Who | Blocker |
|----------|-----|---------|
| Idle-current baseline | owner (bench) | UM2839 IDD point / external ammeter |
| Stop2 + INT1 EXTI + RTC wake config | owner (CubeMX) | `.ioc` regeneration |
| Sleep/wake firmware logic | agent (follow-up) | depends on the CubeMX regen |
| Idle snapshot rate decision (§4.4) | owner | depends on the measured number |

References: RM0456 (low-power modes, RTC), UM2839 (board schematic + current
measurement), ISM330DHCX datasheet (wake-up/activity interrupt registers).
