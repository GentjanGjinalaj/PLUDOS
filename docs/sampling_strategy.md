# Sampling Strategy — Why These Rates

Why each PLUDOS sensor channel is sampled at the rate it is. This is the
rationale companion to:

- `state_machine.md` — the IDLE/MOVING FSM that gates the *live* stream rates
  (ADR-015 v2, currently flashed).
- `decisions.md` ADR-020 — the *planned* high-rate capture + PSRAM buffer +
  burst-drain architecture that the kHz accelerometer rate is for.

Two regimes coexist, and they sample for **different reasons**:

| Regime | Purpose | Where defined |
|---|---|---|
| **Live stream** (ADR-015, now) | Real-time health + FSM motion gating | `state_machine.md` |
| **High-rate capture** (ADR-020, planned) | Offline vibration / predictive-maintenance dataset | this doc + ADR-020 |

---

## 1. The constraints that bound every rate

A sampling rate is never chosen in isolation. Four limits frame the whole table:

1. **Nyquist.** To resolve a feature at frequency *f* without aliasing, the
   sample rate must be ≥ 2 *f*. Vibration analysers conventionally use **2.56 ×**
   the top frequency of interest (gives an FFT span clear of the anti-alias
   filter roll-off). So a rate *R* yields a *usable* analysis band of roughly
   *R* / 2.56.

2. **Anti-aliasing is mandatory, and it fights the rate.** Any spectral content
   above Nyquist folds back into the band as false low-frequency energy that
   cannot be removed after sampling. Every channel must be analog/digital
   low-pass filtered below its Nyquist *before* the ADC. On the ISM330DHCX this
   is the on-chip LPF2 (accel) / LPF1 (gyro) — see §5, which is where the
   high-rate plan has a real trap.

3. **The radio throughput ceiling.** Measured end-to-end on the bench
   (self-reset + Jetson `tcpdump`, two independent runs within 1 %):

   | UDP datagram | EMW3080 throughput (gateway-received) |
   |---|---|
   | 24 B   | 0.24 Mbps |
   | 256 B  | 1.79 Mbps |
   | 512 B  | 2.82 Mbps |
   | 1024 B | 3.95 Mbps |
   | 1472 B | **4.49 Mbps** |

   Throughput is **packet-rate-bound**, not bandwidth-bound: small datagrams
   waste the link on per-packet overhead. The drain must use ~1472 B datagrams
   (max non-fragmented = 1500 MTU − 28 B IP/UDP). This ceiling is why we
   **buffer and drain** instead of streaming kHz live — 600 KB/mission at
   20 KB/s of raw accel cannot stream in real time over 4.49 Mbps *while also*
   leaving headroom, but it drains in ~1 s during IDLE (§4).

   **Air loss was ~0 % in this test — but that is a best case, not the
   deployment case.** Sent-vs-received counts matched exactly at ≥256 B (24 B
   lost ~5 % because of its high packet rate). That measurement was taken with
   the shuttle board and the AP sitting on a desk, ~1 m apart, single device,
   no contention. **Real deployment is the opposite:** shuttles move along the
   rail (varying range, multipath, metal racking), the AP is metres away, and
   up to 6 shuttles share one 2.4 GHz channel and may drain near-simultaneously.
   Expect real loss in the **single-digit to tens of percent**, bursty, and
   correlated with motion. The architecture must therefore treat loss as the
   norm, not the exception — see §9 (reliable-drain protocol). The 4.49 Mbps
   figure is also a *single-shuttle* ceiling; under N-way contention the
   per-shuttle share drops roughly ∝ 1/N, lengthening the drain (§13).

4. **Buffer and power budget.** 8 MB Octo-SPI PSRAM on the B-U585I-IOT02A
   (UM2839 / DB4410) holds the per-mission buffer; 768 KB internal SRAM is DMA
   staging + DSP scratch. Higher ODR ⇒ more bytes/mission ⇒ more PSRAM traffic
   and more sensor + bus + radio energy. The rate is pushed as high as the
   *signal physics justify*, then capped by these budgets — not maximised for
   its own sake.

---

## 2. The IMU bandwidth caveat (read before trusting any ODR)

The ISM330DHCX accelerometer ODR ladder goes to **6.66 kHz**, but **ODR is not
usable bandwidth.** It is a general-purpose 6-axis IMU, not a dedicated
wideband vibration sensor (contrast ST's IIS3DWB, ~6.3 kHz flat response
designed for condition monitoring). The ISM330's usable flat band is set by:

- its mechanical/electrical response and noise floor (accel noise density
  ~70 µg/√Hz per datasheet — rising noise eats the high-frequency SNR), and
- the digital filter chain we configure.

**We do not yet have a measured flat-bandwidth figure for this part in this
mechanical mount.** The exact filter corner tables are in the ISM330DHCX
datasheet (digital filtering section) and ST AN5398 — cite those, do not invent
a "flat to X kHz" number. The honest position: pick an ODR that *can* span the
fault frequencies of interest, set the anti-alias filter deliberately (§5), and
**validate the achieved SNR/bandwidth against logged data offline** before
committing to it as the production rate. See Open Items.

---

## 3. What we are trying to capture

The shuttle is a Savoye XTPS rail-running ASRS unit. Predictive-maintenance
features live in the **mechanical vibration** of its rotating/rolling parts:
drive motor, wheel bearings, belt/drive train, guide rollers. Classic
bearing-fault signatures (BPFO/BPFI/BSF/FTF), gear-mesh and their harmonics
span from a few Hz up into the **low kHz**, the exact values depending on shaft
RPM and bearing geometry.

**We do not have the shuttle's shaft RPM or bearing part numbers yet**, so the
characteristic fault frequencies are unknown (Open Items). The strategy is
therefore *capture-wide-then-analyse*: sample the accelerometer broadly enough
to contain the plausible fault band, keep the **raw** stream for now so the
offline pipeline can compute any derived feature (FFT, envelope, RMS, kurtosis,
crest factor), and retune once real fault data exists.

---

## 4. Per-channel rates and rationale

### Accelerometer — high-rate capture: **3333 Hz** (start point)

- **Why kHz at all:** bearing/gear fault features and their early-wear
  harmonics extend into the low kHz. A 50 Hz stream (the live-mode rate)
  resolves only bulk motion (0–~20 Hz), which is useless for incipient-fault
  detection. Vibration condition monitoring needs the high band.
- **Why 3333 and not 6667:** the *clean* analysis band is set by the on-chip
  LPF2, whose widest cutoff is **ODR/4** (§11) — so at 3333 Hz the digitally
  flat band is **~833 Hz** (Nyquist is 1666 Hz). Running LPF2 *off* and trusting
  the analog anti-alias filter pushes that toward ~1.3–1.5 kHz but with softer
  anti-aliasing near Nyquist. Either way, 3333 Hz spans the plausible
  bearing/gear fundamentals and low harmonics. Doubling to 6667 Hz (LPF2 band
  → ~1666 Hz) doubles bytes/mission (~1.2 MB accel), bus load, and energy, and
  **requires I²C Fast-mode-Plus** (§10) — for a band the IMU's rising noise
  floor may not deliver cleanly (§2). So: **start at 3333 Hz**, step to 6667 Hz
  only if offline analysis shows real content clipped above ~800 Hz.
- **Cost:** 3333 Hz × 6 B (3×int16) = **20 KB/s** → 30 s worst-case mission =
  **600 KB**. Fits PSRAM with room to spare.

### Gyroscope — high-rate capture: **416 Hz**, duty-cycled

- **Why much lower than accel:** angular content (shaft rotation, wheel slip,
  yaw/pitch during cornering and arm extension) is inherently
  lower-frequency than structure-borne bearing vibration. 416 Hz → usable
  ~160 Hz band, ample for rotational dynamics.
- **Why duty-cycle:** the gyro is the dominant power draw of the IMU
  (datasheet: gyro high-performance mode ≫ accel-only current). It does not
  need to run continuously at 416 Hz across a whole mission for low-frequency
  context — candidate for on-demand / lower-duty operation. (Exact current
  figures: ISM330DHCX datasheet electrical-characteristics — measure on this
  board; do not assume.)
- **Cost:** 416 Hz × 6 B = **2.5 KB/s** → 30 s = **75 KB**.

### Temperature + humidity (HTS221) — **2 Hz**

- **Why so low:** thermal time constants of the shuttle and its environment
  are seconds-to-minutes. Nyquist for a 0.1 Hz physical process is 0.2 Hz;
  2 Hz is already 10× over-sampled. Going faster captures nothing real and
  costs I²C bus time (HTS221 read latency 5–10 ms — sampling it at the accel
  rate would consume an entire high-rate sample period).
- **Implementation:** value is **cached** at 2 Hz (`ENV_READ_PERIOD_MS=500`)
  and stamped into every outgoing record, so consumers see env data at the
  full record rate even though the sensor is physically read at 2 Hz.
- Could drop to 0.1–1 Hz with no information loss; 2 Hz kept for FSM/logging
  simplicity and a small margin.

### Live-stream rates (ADR-015) — removed by ADR-021 Phase 1

Historically the firmware transmitted a continuous UDP stream on :5683 — 50 Hz
while MOVING, a 0.1 Hz heartbeat while IDLE — purely to gate state and prove the
link alive (never to capture vibration; that is the buffered high-rate path).

**As of ADR-021 Phase 1 (firmware 2026-06-03) this live stream is gone.** The
EMW3080 radio is the dominant power draw, so it is now held in hardware reset
(off) during MOVING and IDLE, and powered on *only* to drain a finished mission:

| State | Sensor | Radio | What the gateway sees |
|---|---|---|---|
| IDLE | accel polled for the FSM (decimated read) + 12.5 Hz snapshot 10 s every 5 min | **off** | nothing until the next drain |
| MOVING | high-rate FIFO capture (accel 3332 Hz / gyro 416 Hz) → PSRAM (no TX) | **off** | nothing — data is buffered, not streamed |
| MOVING→IDLE | mission sealed | **on ~5 s** to drain, then off | one wake drains the mission **plus** queued idle snapshots on :5684 → one Parquet per stream |

The FSM still runs every loop on a decimated accelerometer read (I²C, radio-
independent), so movement detection is unaffected by the radio being off.

**ADR-021 §1 (firmware 2026-06-03): unified IDLE capture is now built.** Every
5 minutes the IDLE node takes a 10 s snapshot with the *same* ISM330DHCX
accel+gyro at the lowest clean ODR (**12.5 Hz**, 1:1) — directly comparable to
the MOVING capture in the shared sub-6 Hz band. Each snapshot is stamped with the
cached HTS221 temperature and LPS22HH pressure (so Grafana still sees the idle
environment despite the live stream being off) and queued in the PSRAM ring; it
drains for free on the next MOVING→IDLE wake. A cross-mission 75 % ring watermark
forces a standalone safety drain if a node sits idle long enough to fill the ring
(overnight park).

---

## 5. The anti-alias trap at high ODR (must fix before kHz capture)

The currently-flashed firmware sets the accel LPF2 cutoff to **ODR/10**
(`CTRL8_XL HPCF_XL=001`) — at ODR 104 Hz that is ~10.4 Hz, correct for the
50 Hz live stream. **Carried over to a 3333 Hz capture unchanged, the same
ODR/10 cutoff = ~333 Hz, which would throw away the entire kHz band we raised
the ODR to get.**

So the high-rate mode must **reconfigure the filter**, not just the ODR:

- Set the LPF2/LPF1 cutoff to sit **above the signal band but below Nyquist**
  (Nyquist at 3333 Hz = 1666 Hz). Pick the ISM330DHCX cutoff option closest to
  ~1.3–1.5 kHz from the datasheet filter table.
- Verify the chosen cutoff actually attenuates ≥ Nyquist content enough to
  prevent aliasing (check the filter's roll-off, not just its −3 dB point).
- The gyro LPF1 (FTYPE) corner likewise scales with ODR and must be re-picked
  at 416 Hz.

This is a per-mode register reconfiguration, gated behind the CubeMX
prerequisites in ADR-020 (I²C2 Fast-mode-Plus for the FIFO drain, FIFO
watermark → EXTI, I²C2 DMA). **No high-rate capture is valid until the filter
corners are re-derived for the new ODRs** — otherwise the dataset is aliased
and the ML features are garbage.

---

## 6. Throughput / buffer feasibility check

Worst-case 30 s mission, raw int16:

| Channel | Rate | Bytes/s | 30 s buffer |
|---|---|---|---|
| Accel (3 axis) | 3333 Hz | 20 KB/s | 600 KB |
| Gyro (3 axis) | 416 Hz | 2.5 KB/s | 75 KB |
| Temp + hum | 2 Hz | negligible | < 1 KB |
| **Total** | | | **~675 KB** |

- **Buffer:** 675 KB ≪ 8 MB PSRAM. A double-buffer (capture N, drain N−1) still
  fits trivially.
- **Drain time:** 675 KB × 8 ÷ 4.49 Mbps ≈ **1.2 s** — far inside the
  inter-mission IDLE gap. (Add zstd/int16 packing → less.)

The rates are chosen so a whole worst-case mission **buffers in PSRAM and
drains in ~1 second**. That is the load-bearing feasibility result behind
ADR-020.

---

## 7. Open items (measure before locking rates)

- **Shuttle shaft RPM + bearing geometry** → real fault frequencies. Until
  known, 3333 Hz / 416 Hz are *physics-plausible defaults*, not validated.
- **ISM330DHCX achieved flat bandwidth + noise floor in this mount** — measure
  with a known vibration source; confirms whether 3333 Hz delivers real content
  to ~1.3 kHz or the noise floor caps it lower (would justify dropping the rate
  and saving power/bytes).
- **IMU current draw** (accel-only vs +gyro, per ODR) on this board — sets the
  gyro duty-cycle policy. Use datasheet figures as a starting estimate, then
  measure (ties into ADR-011 Alumet).
- **Anti-alias filter corners re-derived** for 3333 Hz / 416 Hz (§5) before any
  capture run is trusted.

---

## 8. How the throughput numbers were measured (reproduce this)

The 4.49 Mbps ceiling and the loss numbers were **measured on the real radio**,
not modelled. Everything needed to reproduce it lives in the repo:

- **Firmware benchmark:** `STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c` —
  function `TELEMETRY_BenchThroughput()`, guarded by `#define BENCH_THROUGHPUT`
  (set to `1` to run, `0` for normal operation — **currently `0`**). On boot,
  after the UDP socket is armed, it sends a 3 s burst at each datagram size
  {24, 256, 512, 1024, 1472} B as fast as `MX_WIFI_Socket_sendto` allows, then
  resumes normal telemetry. It prints per size over UART (`[BENCH] size=… ok=…
  fail=… pkt/s … Mbps`). **The sender-side `pkt/s` is the true radio ceiling**
  because `sendto` *backpressures* (blocks until the module drains) — it does
  not silently drop, so `fail≈0` and the emit rate equals the link rate.
- **Capture method (no extra files committed):**
  - *Sender side:* read the board's ST-Link VCP at 115200 8N1 (`/dev/ttyACM0`
    on the dev laptop) to collect the `[BENCH]` lines.
  - *Receiver side:* on the Jetson, `tcpdump -i wlP1p1s0 -n -w cap.pcap 'udp
    port 5683'`, then histogram UDP payload lengths and divide each size's
    packet count by its 3 s window. Air loss = 1 − (received / sender-ok) per
    size. (The board was reset over SWD with the bundled
    `STM32_Programmer_CLI -c port=SWD mode=HOTPLUG -rst` to trigger the boot
    sweep.)
- **Result (paired run, 2026-06-01):** sender = receiver exactly at ≥256 B;
  24 B lost ~5 %. Three independent runs agreed within ~1 %. See §1 for the
  table and the deployment caveat.

No throughput test harness is kept in the repo as a standalone tool — the test
*is* the `BENCH_THROUGHPUT` block in firmware plus stock `tcpdump`. To re-run:
flip the define to `1`, flash, reset, capture UART + pcap, flip back to `0`.

---

## 9. Reliable drain protocol (because real air loss is not zero)

Since §1 establishes deployment loss will be bursty and significant, the drain
**must** recover lost data — a raw UDP blast is not acceptable for a research
dataset that needs completeness. The whole mission is already buffered in PSRAM,
which makes recovery cheap: we can retransmit any chunk at any time. The chosen
scheme is **NAK-based selective-repeat ARQ over the buffered mission** — *not*
per-packet ACK (which would halve throughput with round-trips):

1. **Frame the mission.** On mission-end, partition the PSRAM buffer into chunks
   of ≤ ~1400 B payload. Each chunk carries a small header:
   `magic, shuttle_id, mission_id (u16), chunk_seq (u16), total_chunks (u16),
   payload_len, crc32`. ~675 KB ⇒ ~480 chunks.
2. **DRAIN_BEGIN.** STM sends a control packet (`mission_id, total_chunks,
   sample_count, odr_accel, odr_gyro, t0_ms, layout`) — repeated a few times
   until the gateway acknowledges, so the metadata can't be lost.
3. **Blast.** STM sends all chunks back-to-back at the 1472 B rate (~480 chunks
   ≈ 1.2 s on a clean link).
4. **DRAIN_END.** STM sends an end marker (repeated).
5. **NAK.** Gateway keeps a received-chunk bitmap per `(shuttle_id, mission_id)`.
   On DRAIN_END (or a quiet timeout) it replies with **one** packet: either
   `ACK_COMPLETE`, or a compact list of missing `chunk_seq` ranges (run-length).
6. **Retransmit.** STM re-sends only the missing chunks from PSRAM, then
   DRAIN_END again. Repeat from step 5.
7. **Terminate.** Loop until `ACK_COMPLETE`, or a `MAX_ROUNDS` (e.g. 5) / total
   drain-deadline cap is hit. If capped, the gateway writes the mission with
   `complete=false` and records the gap ranges, and the STM gives up rather than
   stalling the FSM.

**Why this is robust and simple:**
- Nominal link (~0 % loss) ⇒ usually **one pass, zero retransmits**.
- Lossy link ⇒ a single compact NAK recovers many gaps per round; bounded rounds.
- **Idempotent (within a boot session):** `mission_id` lets the gateway group a
  drain's packets and ignore duplicate/late chunks of a still-fresh drain. Note the
  firmware `mission_id` **resets to 0 on every STM32 reset** (incl. the IWDG
  watchdog), so it is unique only within one boot session — the gateway dedups over a
  short TTL window, not permanently, and a re-used id after a reset is treated as a
  new drain. Parquet filenames/columns use a gateway-assigned unix-ms id, never the
  firmware `mission_id` (see `decisions.md` ADR-021 implementation notes).
- **Control-packet loss tolerated:** if STM hears no NAK/ACK within a timeout, it
  re-sends DRAIN_END to re-prompt the gateway. All waits are bounded.
- **CRC32 per chunk** catches corruption (Wi-Fi has its own FCS, but app-level
  CRC is cheap insurance against truncation/misframing).
- No `malloc`: the bitmap is a fixed `total_chunks/8`-byte static array sized for
  the worst-case mission; chunks are read straight from the memory-mapped PSRAM.

**FEC is deliberately not used.** Forward error correction (e.g. Reed-Solomon /
fountain codes) adds constant overhead on every transfer and MCU compute, to
avoid round-trips. But the drain runs during IDLE where latency is free and a
couple of NAK round-trips cost milliseconds — ARQ is strictly cheaper here. Keep
FEC in reserve only if measured loss is so high that ARQ rounds explode.

---

## 10. Exact CubeMX / `.ioc` changes

Per the hard rules, `.ioc` edits are done by you in CubeMX, not by me. Here is
exactly what to change and what can instead be done in user code.

### Change 1 — I²C2 bus speed *(REQUIRED, `.ioc`)*
The bus is currently **101 kHz (Standard mode)** — usable I²C payload ~11 KB/s,
which **cannot** even carry accel-only 3333 Hz (needs 20 KB/s). Raise it:

- CubeMX → *Pinout & Configuration* → *Connectivity* → **I2C2** →
  *Parameter Settings* → **Timing configuration**:
  - **I2C Speed Mode = Fast Mode** (400 kHz) — *sufficient*: ~43 KB/s usable,
    covers accel 3333 Hz + gyro 416 Hz (~22.5 KB/s) with margin. **Recommended
    starting point** — safest on the shared multi-sensor bus.
  - *Or* **Fast Mode Plus** (1 MHz, ~109 KB/s) only if you later need 6667 Hz or
    more sensors. FM+ stresses bus capacitance/pull-ups on the shared I²C2 —
    validate signal integrity (scope SCL) before trusting it.
- Save → regenerate. Confirm `I2C2.Timing` in the `.ioc` is no longer
  `0x30909DEC`. CubeMX configures the U5 FM/FM+ electricals automatically.
- **Verify after regen:** all *other* I²C2 sensors (HTS221, LPS22HH, LIS2MDL)
  still init OK at the new speed — they are FM/FM+ capable but the bus must
  tolerate it physically.

### Change 2 — ISM330 INT1 → EXTI *(OPTIONAL, `.ioc`)*
For a FIFO-watermark interrupt instead of polling. Needs the MCU pin wired to
ISM330DHCX INT1 on the IOT02A — **confirm the pin in UM2839 schematic first** (I
don't have it verified). Then: set that pin to `GPIO_EXTI`, trigger *rising*,
enable its NVIC line. **Skippable:** the firmware can poll `FIFO_STATUS1/2` over
I²C every few ms in the capture loop — no `.ioc` change, slightly more CPU.

### Change 3 — I²C2 RX DMA *(OPTIONAL, `.ioc`)*
To offload FIFO burst reads. CubeMX → I2C2 → *DMA Settings* → Add → **I2C2_RX**
(via GPDMA1), Normal mode, byte/byte; enable NVIC. **Skippable:** a blocking
burst read of ~200 B at 400 kHz–1 MHz takes ~2–4 ms per ~10 ms window — tolerable
for a dedicated capture mode.

### NOT a CubeMX change — PSRAM memory-mapping *(user code — DONE)*
`MX_OCTOSPI1_Init()` brings up the 8 MB AP-Memory PSRAM (OCTOSPI1,
`MemoryType=APMEMORY`, `DeviceSize=23` = 8 MB, `Refresh=100`) at the peripheral
level but does not memory-map it. The device-side bring-up lives in a new
`Core/Src/psram.c` (+ `Core/Inc/psram.h`): it tunes the delay block, programs
the APS6408 mode registers (MR0=0x24, MR8=0x0B) and calls `HAL_OSPI_MemoryMapped()`,
reusing the CubeMX `hospi1` handle. All opcodes, register values and dummy-cycle
counts are copied verbatim from the STM32CubeU5 v1.8.0 BSP for this board
(`aps6408.c`, `aps6408_conf.h`, `b_u585i_iot02a_ospi.c`) — nothing invented.
`PSRAM_Init()` + `PSRAM_SelfTest()` are called from `USER CODE BEGIN 2` in
`main()` (not the `OCTOSPI1_Init 2` guard — they need `huart1` for logging and
belong in the application module). After init the PSRAM is addressable as a
normal array at **0x90000000** (region 0x90000000–0x90800000). **No `.ioc`
change needed for the buffer RAM.**

**Verified on hardware (2026-06-01):** boot log reports
`[PSRAM] APS6408 memory-mapped at 0x90000000 (8192 KB)` then `[PSRAM] self-test PASS`.
The self-test writes/reads a dense 128 KB block (stuck-bit check) and one unique
word per 4 KB page across the full 8 MB (address-wiring check); both passed.

**Summary: only Change 1 (I²C2 speed) is a required `.ioc` edit.** Changes 2–3
are optional optimisations; PSRAM mapping and all capture/drain logic are
user-code.

---

## 11. Concrete sensor register config per mode

Two distinct ISM330DHCX configurations now coexist. The capture-mode filter
corners **must** be re-derived from the new ODR — copying the live-mode ODR/10
cutoff to 3333 Hz would band-limit to ~333 Hz and waste the high ODR.

> Divider ladder below is the LSM6/ISM330-family LPF2 set — **verify exact codes
> against the ISM330DHCX datasheet (CTRL8_XL HPCF_XL table) before flashing.**

### Live / context mode (ADR-015, currently flashed) — unchanged
| Reg | Value | Meaning |
|---|---|---|
| CTRL1_XL | `0x42` | accel ODR=104 Hz, FS=±2 g, LPF2_XL_EN=1 |
| CTRL8_XL | `0x20` | LPF2 cutoff = ODR/10 ≈ 10.4 Hz (low-pass path) |
| CTRL2_G  | `0x40` | gyro ODR=104 Hz, FS=±250 dps |
| CTRL4_C / CTRL6_C | `0x02` / `0x07` | gyro LPF1 on, FTYPE=111 → ~11.5 Hz |

Anti-aliased for the 50 Hz read (Nyquist 25 Hz). Correct as-is.

### Capture mode (ADR-020, planned) — to flash for high-rate runs
| Reg | Value | Meaning |
|---|---|---|
| CTRL1_XL | `0x9?` | accel **ODR=3333 Hz** (ODR_XL=`1001`), FS=±4 g (FS_XL=`10`) for shock headroom, LPF2_XL_EN=1 → `0x9A` (verify FS bits) |
| CTRL8_XL | LPF2 cutoff = **ODR/4 ≈ 833 Hz** (HPCF_XL=`000`, low-pass path) — widest clean band below the 1666 Hz Nyquist |
| CTRL2_G  | gyro **ODR=416 Hz** (ODR_G=`0110`), FS as needed |
| FIFO_CTRL1..4 | enable FIFO, set BDR (batch data rate) = accel/gyro ODR, watermark = a block that fits one I²C burst (e.g. 32–64 samples), continuous/stream mode |

Design rule for the cutoff: **signal band < LPF2 cutoff < Nyquist (ODR/2)**.
- 3333 Hz: clean band ≤ 833 Hz (ODR/4). Want more? Set LPF2 OFF → ~ODR/2 with the
  analog AAF (softer rolloff), or step to 6667 Hz.
- 6667 Hz: ODR/4 = 1666 Hz clean band, Nyquist 3333 Hz — needs FM+ I²C (§10).
- Gyro 416 Hz: re-pick LPF1 FTYPE for a corner below 208 Hz Nyquist.

FS choice (±2 g vs ±4 g) trades resolution for clip headroom — start ±4 g to
avoid clipping rail-joint shocks, revisit once peak amplitudes are measured.

---

## 12. Jetson-side changes

The gateway currently only handles the 24 B live `PludosTelemetry` on UDP 5683
(`client/data-engine.py`). The drain needs a second receive path **and** a
back-channel to the shuttle (for NAK/ACK). Keep the two concerns separate:

- **Dedicated drain port.** Receive drain control + chunks on a new UDP port —
  **5684** is free (retired NC-UDP port, ADR-015). The 5683 live path is
  untouched. (Alternative: a type/magic byte on 5683 — rejected; a second port
  keeps the hot 24 B path simple.)
- **Reassembler.** Per `(shuttle_id, mission_id)`: a chunk bitmap, CRC check per
  chunk, dedup of late/duplicate chunks, NAK-range generation, and `ACK_COMPLETE`
  — all sent back to the shuttle's source address from `recvfrom`.
- **Parquet writer (high-rate schema).** One file per completed mission, keyed by
  `(shuttle_id, mission_id)`: `sample_index, ax, ay, az` (int16) at the accel ODR,
  the gyro stream at its own ODR (separate column group or its own table — do not
  upsample/pad to the accel rate), env (temp/hum) stamped from the 2 Hz cache,
  plus mission metadata (`odr_accel, odr_gyro, t0_ms, complete`). Derive per-sample
  time as `t0 + index/ODR` — never per-sample timestamps.
- **Drain scheduler (anti-contention).** Optionally gate concurrent drains so 6
  shuttles don't blast at once (token/`DRAIN_GRANT`, or just rely on natural
  mission-end spread + a small random pre-drain jitter on the STM). Start with
  jitter; add explicit gating only if measured contention loss is bad (§13).
- **tmpfs budget.** ~675 KB × concurrent draining shuttles — negligible on the
  8 GB Jetson; reassembly buffers live in RAM, flushed to Parquet on completion.

---

## 13. Recommended configuration & execution checklist

**Recommended starting configuration** (provisional — see §7 open items):

| Channel | Rate | Filter | Why |
|---|---|---|---|
| Accel | **3333 Hz**, ±4 g | LPF2 = ODR/4 ≈ 833 Hz | widest clean band that needs only Fast-mode I²C; covers plausible fault fundamentals |
| Gyro | **416 Hz** | LPF1 corner < 208 Hz | low-freq rotational context; duty-cycle for power |
| Temp/hum | **2 Hz** cache | — | thermal time constants; already implemented |
| I²C2 | **400 kHz (Fast mode)** | — | sufficient (43 KB/s ≫ 22.5 KB/s); safe on shared bus |
| Drain datagram | **1472 B** | — | measured 4.49 Mbps, ~0 % loss nominal |
| Loss recovery | **NAK selective-repeat ARQ** | — | recovers bursty deployment loss from the PSRAM buffer |

**Contention reality check (6 shuttles):** worst case the per-shuttle share is
~4.49/6 ≈ 0.75 Mbps → a 675 KB mission drains in ~7 s instead of ~1.2 s, plus
ARQ rounds. Still inside a typical IDLE gap **if drains are staggered**. If they
aren't, either stagger (§12 scheduler) or drain across multiple IDLE windows
(the PSRAM holds the mission until `ACK_COMPLETE`).

**Upgrade path** (only with evidence): fault content above ~800 Hz → accel
6667 Hz + LPF2 ODR/4 (1666 Hz band) + I²C **FM+ 1 MHz**. Energy too high →
on-device feature extraction (FFT band energy / RMS / kurtosis / crest) at ~1 Hz,
radio mostly off (the ADR-020 deployment endgame).

**Execution order:**
1. ✅ *(you, CubeMX)* I²C2 → Fast mode (§10 Change 1); regenerated; built;
   sensors init confirmed (421 kHz, boot log shows all sensors up).
2. ✅ *(me, code)* PSRAM memory-map (`Core/Src/psram.c`) + write/read-back
   self-test, called from `USER CODE BEGIN 2`. Build + hardware verified
   (`[PSRAM] self-test PASS`).
3. *(me, code)* capture mode: ISM330 FIFO config (§11), poll-or-DMA burst read
   into PSRAM during MOVING, mission framing.
4. *(me, code)* drain ARQ on STM (§9) + *(me, code)* Jetson drain receiver +
   Parquet writer (§12).
5. *(you + me)* flash, run a real mission, verify a complete Parquet end-to-end,
   then measure loss/drain-time/energy and retune rates (§7).

---

## 14. WiFi power & connection policy (ADR-021)

The radio is the dominant edge-node power draw, so it is **off by default** (EMW3080
held in hardware reset) and wakes only to drain. This supersedes the ADR-015
always-on stream and ADR-020's "live stream unaffected" framing.

### Per-state behaviour

| State | ISM330 capture | PSRAM | WiFi |
|---|---|---|---|
| **Boot** | — | — | on once → beacon (cache Jetson IP) → **off** |
| **MOVING** | high ODR (3333/416 Hz) → FIFO → ring | filling fast | **off** |
| **MOVING→IDLE** | seal mission segment | — | **on → drain mission (ARQ) → ACK → off** |
| **IDLE (short)** | low ODR (~12.5 Hz) → ring | filling slow | off |
| **IDLE (long, hours)** | low ODR baseline | crosses flush watermark | **on → drain → off** |

### Exact wake logic

`WIFI_PowerOn()` is called on exactly two triggers, and `WIFI_PowerOff()` after each:

1. **Mission end** (`MOVING → IDLE`): drain the just-captured high-rate burst.
2. **Flush watermark**: when undrained bytes in the ring cross a threshold
   (provisional 75% of ring). Covers long idle where low-rate capture accumulates,
   and the (practically-never) case of a mission long enough to fill the ring
   mid-motion.

Between triggers the radio is fully off. Re-power costs ~4 s (DHCP-dominated,
hardware-measured) — paid during IDLE where latency is free. `jetson_ip` is cached
across cycles so re-draining skips the beacon. If a drain cannot complete (Jetson
unreachable, ARQ round cap hit), the data **stays in PSRAM** and is retried on the
next trigger — nothing is freed until `ACK_COMPLETE`.

### Implemented vs pending

- **Done + hardware-verified (2026-06-01):** the `WIFI_PowerOn()` / `WIFI_PowerOff()`
  primitives in `main.c`. `WIFI_PowerOff()` closes the socket, disconnects, and holds
  the EMW3080 RESET pin (`WRLS_WKUP_W`) low. The off→on cycle was verified reversible
  (`[SELFTEST] WiFi power-cycle PASS`). The boot path now routes through
  `WIFI_PowerOn()`.
- **Pending:** the capture engine (FIFO → ring, §11) and the drain ARQ (§9) that
  together *invoke* this policy. Until they exist the firmware still runs the
  ADR-015 always-on live stream; `WIFI_PowerOff()` is built but not yet wired to a
  trigger.

> **MCU sleep is a separate, smaller power item:** the idle loop currently busy-waits
> in `WIFI_DelayWithYield` rather than entering `WFI`/stop mode. Tracked as a follow-up.

---

## 15. References

- `decisions.md` ADR-015 (live UDP stream), ADR-020 (high-rate capture + PSRAM),
  ADR-021 (power-aware capture + WiFi duty-cycling).
- `state_machine.md` — FSM thresholds and live-stream rates.
- `wire_protocol.md` — record layout, int16 scaling, 0x7FFF sentinel.
- ISM330DHCX datasheet — ODR ladder, digital filter corner tables, noise
  density, current consumption. *(Note: `hardware_refs.md` line 32 mis-cites
  this as "ISM330DLC" — should be ISM330DHCX.)*
- ST AN5398 — ISM330DHCX application note (filtering, FIFO).
- UM2839 / DB4410 — B-U585I-IOT02A: 8 MB Octo-SPI PSRAM, 64 MB QSPI flash.
