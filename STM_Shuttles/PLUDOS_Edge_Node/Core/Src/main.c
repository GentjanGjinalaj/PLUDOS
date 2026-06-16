/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "wifi_credentials.h"
#include "sensors.h"
#include "psram.h"
#include <stdio.h>
#include <string.h>
#include <math.h>            /* fabsf() for FSM threshold check */

#include "mx_wifi.h"
#include "mx_wifi_io.h"
#define USE_BSP_I2C_SHUT_DOWN

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* 24-byte unified telemetry payload (ADR-016 v3 / wire_protocol.md §1).
 * Sensor floats replaced by int16_t scaled ×100 (accel g, gyro dps, temp °C) or
 * ×10 (humidity %RH) — halves per-field wire cost; adds ISM330 gyroscope.
 * Sentinel: 0x7FFF (INT16_MAX) for ALL unavailable int16 fields — accel, gyro,
 *           temp, and humidity. See wire_protocol.md §1 and data-engine.py.
 * Python unpack: struct.unpack('<BHIBhhhhhhhh', data) */
#pragma pack(push, 1)
typedef struct {
  uint8_t  shuttle_id;      /* 1-based integer; gateway maps to name via SHUTTLE_NAMES */
  uint16_t sequence_id;     /* monotonic per-shuttle, wraps at 65535                    */
  uint32_t tick_ms;         /* HAL_GetTick() at sample time                             */
  uint8_t  state;           /* 0 = STATE_IDLE, 1 = STATE_MOVING                         */
  int16_t  accel_x;         /* g × 100; 0x7FFF if ISM330 unavailable                   */
  int16_t  accel_y;         /* g × 100                                                   */
  int16_t  accel_z;         /* g × 100                                                   */
  int16_t  gyro_x;          /* dps × 100; 0x7FFF if ISM330 unavailable                  */
  int16_t  gyro_y;          /* dps × 100                                                  */
  int16_t  gyro_z;          /* dps × 100                                                  */
  int16_t  temp_c;          /* °C × 100; 0x7FFF if HTS221 unavailable                   */
  int16_t  humidity_pct;    /* %RH × 10;  0x7FFF if HTS221 unavailable                  */
} __attribute__((packed)) PludosTelemetry_t;   /* total: 24 bytes                        */
#pragma pack(pop)

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* Power figures have moved to the gateway (POWER_IDLE_MW / POWER_MOVING_MW env vars).
 * Reference values for calibration (STM32U585 DS13259 §6.3.7 + EMW3080 §5.2):
 *   IDLE  = (MCU 15mA + sensors 2mA + WiFi assoc 10mA) × 3.3V ≈  89 mW
 *   MOVING = (MCU 15mA + sensors 2mA + WiFi TX ~200mA) × 3.3V ≈ 716 mW (peak burst)
 * Actual average MOVING power depends on 50 Hz TX duty cycle; measure with bench ammeter. */

/* Beacon discovery — STM32 listens for "PLUDOS-GW:<ip>" UDP broadcasts from the gateway.
 * BEACON_TIMEOUT_MS: per-attempt recv timeout passed to MX_SO_RCVTIMEO (milliseconds).
 * BEACON_MAX_RETRIES x BEACON_TIMEOUT_MS gives the total wait ceiling before fallback. */
#define BEACON_PORT             5000U  /* must match BEACON_PORT in data-engine.py / .env */
#define BEACON_TIMEOUT_MS       3000   /* per-attempt timeout at boot (patient) */
#define BEACON_MAX_RETRIES      10U    /* 10 x 3 s = 30 s max; gateway beacons every 10 s */
#define BEACON_RETRY_TIMEOUT_MS  500   /* per-attempt timeout for in-loop quick checks */
#define BEACON_RETRY_PERIOD_MS  30000U /* how often the main loop re-checks for a beacon */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
MDF_HandleTypeDef AdfHandle0;
MDF_FilterConfigTypeDef AdfFilterConfig0;

I2C_HandleTypeDef hi2c1;
I2C_HandleTypeDef hi2c2;

OSPI_HandleTypeDef hospi1;
OSPI_HandleTypeDef hospi2;

SPI_HandleTypeDef hspi2;

UART_HandleTypeDef huart4;
UART_HandleTypeDef huart1;

PCD_HandleTypeDef hpcd_USB_OTG_FS;

/* USER CODE BEGIN PV */
// --- PLUDOS State Machine (ADR-015: unified UDP stream, no SRAM buffer) ---
typedef enum {
  STATE_IDLE   = 0,
  STATE_MOVING = 1
} ShuttleState_t;

static ShuttleState_t current_state = STATE_IDLE;
static uint32_t last_movement_tick = 0U;        /* tick of the most recent above-threshold sample */
static uint32_t last_above_threshold_tick = 0U; /* same value, separate name for debounce clarity */
static uint32_t continuous_movement_start_tick = 0U; /* tick when the current dwell began */
static uint32_t fsm_settle_until_tick = 0U; /* suppress motion trigger until this tick: OUTX is filter-settling after a CTRL1_XL ODR change */

/* UNCALIBRATED — set threshold = mean(idle_mag²) + 5σ after recording
   5 min IDLE + 5 min motion on the actual fixture. Current 0.06 is a guess. */
#define MOVEMENT_THRESHOLD_G2   0.06f    /* |mag² - 1g²| trigger — tilt-immune (magnitude stays 1g at any orientation) and captures horizontal travel (deviation ~= a_horiz²). TUNE from UART rest-floor capture. */
#define MOVEMENT_DWELL_MS       500U     /* continuous-above duration to enter STATE_MOVING (0.5s: reliable real-world trigger without qualifying transient shakes) */
#define MOVEMENT_DEBOUNCE_MS    300U     /* sub-threshold tolerance inside a dwell — survives motion microbreaks */
#define NO_MOVEMENT_TIMEOUT_MS  20000U   /* no above-threshold sample for this long → STATE_IDLE */
#define ACCEL_SETTLE_MS         1000U    /* blank the motion trigger this long after any CTRL1_XL ODR change. Switching ODR (e.g. 104Hz↔12.5Hz at idle-snapshot entry/exit) resets the LPF2 digital filter; OUTX reads mid-reset return ~0g, so |mag²-1g²|≈1.0 and a phantom MOVING dwell completes. 1s covers the slow 12.5Hz settle while leaving 9s of real detection in a 10s snapshot. Empirical — TUNE from UART. */

#define SAMPLE_PERIOD_IDLE_MS   100U     /* 10 Hz internal sampling in IDLE (FSM responsiveness) */
#define SAMPLE_PERIOD_MOVING_MS 20U      /* 50 Hz MOVING main-loop poll period (FSM motion cadence only — NOT a data or TX rate). Under ADR-021 the radio is off during MOVING; high-rate IMU is captured to PSRAM via FIFO and drained over UDP after the run, not transmitted live. */
#define TX_PERIOD_IDLE_MS       10000U   /* 0.1 Hz UDP transmit in IDLE — every 100th sample */
#define ENV_READ_PERIOD_MS      500U     /* 2 Hz HTS221 refresh; cached for every TX */

/* ISM330 I2C addr: SA0 tied to VDD on IOT02A → base 0x6B, left-shifted → 0xD6.
 * Confirmed in board schematic; datasheet default (SA0=0) gives 0x6A = 0xD4. */
#define ISM330_ADDR        0xD6
#define CTRL1_XL           0x10  /* accel control: ODR + FS */
#define CTRL2_G            0x11  /* gyro control:  ODR + FS */
#define CTRL4_C            0x13  /* gyro filter: LPF1_SEL_G enable (bit1) */
#define CTRL6_C            0x15  /* gyro filter: LPF1 bandwidth (FTYPE) */
#define CTRL8_XL           0x17  /* accel filter: LPF2 bandwidth (HPCF_XL) + HP-path enable */
#define OUTX_L_A           0x28  /* accel output X low byte (6-byte burst: X, Y, Z) */
#define OUTX_L_G           0x22  /* gyro output X low byte  (6-byte burst: X, Y, Z) */
/* ISM330DHCX gyro sensitivity at ±250 dps FS: 8.75 mdps/LSB (DS13281 Table 3). */
#define GYRO_SENS_MDPS_LSB 8.75f

/* Global physics variables so the Live Watch can see them */
float vib_x = 0.0f;
float vib_y = 0.0f;
float vib_z = 0.0f;
float gyro_x = 0.0f;
float gyro_y = 0.0f;
float gyro_z = 0.0f;
static uint8_t ism330_gyro_ok = 0U; /* set 1 on successful gyro read each loop; used in TELEMETRY_Send */

static uint16_t current_packet_num = 1U;

// =========================================================================
// PLUDOS NETWORK CONFIGURATION (ADR-015)
// =========================================================================
/* WIFI_SSID, WIFI_PASSWORD, JETSON_IP, and SHUTTLE_ID are in
 * wifi_credentials.h (gitignored — copy from wifi_credentials.h.example). */
#define TELEMETRY_PORT 5683U  /* single UDP port for the unified PludosTelemetry stream */

/* Set to 1 to run a one-shot UDP throughput benchmark at boot (after the socket
 * is armed), then resume normal telemetry. Measures the EMW3080 ceiling: the
 * sender-side pkt/s is the real radio limit because MX_WIFI_Socket_sendto
 * backpressures at the module's actual throughput. Set back to 0 after measuring. */
#define BENCH_THROUGHPUT 0

/* Set to 1 to verify the WiFi power-cycle once at boot: after the first
 * WIFI_PowerOn(), do WIFI_PowerOff() then WIFI_PowerOn() again and log whether
 * the socket re-arms. Proves the held-in-reset → re-Init path is reversible
 * (the load-bearing assumption of the ADR-020 drain). Set back to 0 after. */
#define WIFI_POWERCYCLE_SELFTEST 0

/* ARM Cortex-M33 is little-endian; mx_sockaddr_in.sin_port is network byte order.
 * No stdlib htons() on bare metal — swap bytes at compile time. */
#define PLUDOS_HTONS(x) ((uint16_t)(((uint16_t)(x) >> 8U) | ((uint16_t)(x) << 8U)))

static int32_t socket_id = -1;            /* -1 = socket closed */
static char    uart_buf[120];             /* scratch buffer shared by all UART log messages */

/* Pointer to the MXCHIP driver object owned by the ST WiFi transport layer */
MX_WIFIObject_t *wifi_obj = NULL;
static volatile uint8_t wifi_driver_initialized = 0;
static volatile uint8_t wifi_station_event = 0xFF;
static volatile uint8_t wifi_station_ready = 0;

static char     jetson_ip[16]      = {0};   /* populated from JETSON_IP define at init */
static uint8_t  stm32_ip[4]        = {0};   /* STM32's own DHCP address; used for beacon subnet filter */
static uint8_t  hts221_initialized  = 0U;    /* SENSOR_Humidity_Init succeeded */
static uint8_t  lps22hh_initialized = 0U;    /* SENSOR_Pressure_Init succeeded */
/* Environmental sensor cache (refreshed every ENV_READ_PERIOD_MS so the I²C bus
 * stays out of the 50 Hz hot path; cached values stamp every outgoing packet). */
static float    cached_temp_c       = -999.0f;
static float    cached_humidity_pct =    0.0f;
static float    cached_pressure_hpa =    0.0f;  /* LPS22HH; 0 = no valid read yet */

/* TX bookkeeping for periodic per-second status log. */
static uint32_t last_tx_tick      = 0U;
static uint32_t tx_count_window   = 0U;
static uint32_t tx_window_start_tick = 0U;

/* =========================================================================
 * ADR-020 high-rate capture engine (sampling_strategy.md §11/§13)
 * --------------------------------------------------------------------------
 * MOVING: the ISM330 batches accel 3332 Hz + gyro 416 Hz into its on-chip FIFO
 * (stream mode); the loop drains the FIFO over I²C into the 8 MB PSRAM ring,
 * one mission per MOVING episode. The FSM/live path keeps polling the OUTX
 * registers (still live in FIFO mode) at the decimated loop rate, so motion
 * detection and telemetry are unaffected. IDLE: FIFO bypassed, sensors back to
 * the low-rate anti-aliased live config. All register bytes verified against
 * the ST ism330dhcx_reg.h bitfields/enums — none invented.
 * ========================================================================= */
#define ISM330_FIFO_CTRL3     0x09U   /* BDR_GY[7:4] | BDR_XL[3:0] (FIFO batch rates) */
#define ISM330_FIFO_CTRL4     0x0AU   /* FIFO_MODE[2:0] */
#define ISM330_FIFO_STATUS1   0x3AU   /* diff_fifo[7:0] (unread word count, low byte) */
#define ISM330_FIFO_STATUS2   0x3BU   /* [1:0]=diff_fifo[9:8], bit6=fifo_ovr_ia */
#define ISM330_FIFO_DATA_TAG  0x78U   /* FIFO_DATA_OUT_TAG; 0x78..0x7E auto-wrap per word */

/* Capture-mode sensor config. FS kept at ±2 g / ±250 dps (same as live) so the
 * decimated OUTX read the FSM uses keeps the 0.061 mg/LSB and 8.75 mdps/LSB scaling.
 * (Doc §11 suggests ±4 g for shock headroom — provisional; revisit once rail-joint
 * peak amplitudes are measured. ±2 g avoids a state-dependent scaling branch.) */
#define CAP_CTRL1_XL_MOVING   0x92U   /* accel ODR=3332Hz (1001), FS=±2g (00), LPF2_XL_EN=1 */
#define CAP_CTRL8_XL_MOVING   0x00U   /* HPCF_XL=000 → LPF2 cutoff = ODR/4 ≈ 833 Hz (< 1666 Nyquist) */
#define CAP_CTRL2_G_MOVING    0x60U   /* gyro ODR=416Hz (0110), FS=±250 dps */
#define CAP_FIFO_CTRL3_MOVING 0x69U   /* BDR_GY=417Hz (6), BDR_XL=3333Hz (9) */
#define CAP_FIFO_MODE_STREAM  0x06U   /* FIFO_CTRL4: continuous/stream mode */
#define CAP_FIFO_MODE_BYPASS  0x00U   /* FIFO_CTRL4: bypass (FIFO off / flush) */

/* ADR-021 §1 IDLE snapshot config: same accel+gyro chip at the lowest clean ODR
 * (12.5 Hz, 1:1) so idle data is directly comparable to MOVING capture in the
 * shared sub-6 Hz band. CTRL8_XL stays at LIVE (0x20, LPF2 cutoff ODR/10 ≈ 1.25 Hz
 * < 6.25 Hz Nyquist → alias-free). Codes derived from the MOVING/LIVE defines:
 * ODR 12.5 Hz = 0001 (cf. 104 Hz = 0100 in LIVE_CTRL1_XL=0x42). */
#define CAP_CTRL1_XL_IDLE     0x12U   /* accel ODR=12.5Hz (0001), FS=±2g, LPF2_XL_EN=1 */
#define CAP_CTRL2_G_IDLE      0x10U   /* gyro  ODR=12.5Hz (0001), FS=±250 dps */
#define CAP_FIFO_CTRL3_IDLE   0x11U   /* BDR_GY=12.5Hz (1), BDR_XL=12.5Hz (1) */
#define CAP_IDLE_SNAP_PERIOD_MS  600000U  /* idle snapshot every 10 min (sit-time is exact via tx_tick) */
#define CAP_IDLE_SNAP_DUR_MS      10000U  /* each idle snapshot lasts 10 s */
/* Pre-drain transmit jitter: before powering the radio for a drain, wait a random
 * 1.0–15.0 s (0.1 s granularity) so two shuttles exiting MOVING near-simultaneously
 * don't blast the shared 2.4 GHz channel at once. Decorrelates the short (~1–4 s)
 * drain bursts; timestamp-safe because tx_tick_ms is sampled at BEGIN, after this
 * wait. Stopgap until Phase-2 NAK ARQ — jitter lowers collision odds, it can't
 * recover a lost packet. */
#define DRAIN_JITTER_MIN_MS    1000U
#define DRAIN_JITTER_MAX_MS   15000U
#define DRAIN_JITTER_STEP_MS    100U  /* 0.1 s granularity */
/* Gyro LPF1 (CTRL4_C=0x02, CTRL6_C=0x07 FTYPE=111) left at boot setting: FTYPE=111 is
 * the narrowest LPF1, so its corner is the lowest of all FTYPE codes and stays below
 * the 208 Hz Nyquist at 416 Hz ODR. Exact corner: AN5192/AN5398 Table 14. */

/* Live/IDLE restore bytes — must match the boot init in USER CODE 2. */
#define LIVE_CTRL1_XL         0x42U   /* accel 104Hz, ±2g, LPF2 on */
#define LIVE_CTRL8_XL         0x20U   /* LPF2 cutoff ODR/10 ≈ 10.4 Hz */
#define LIVE_CTRL2_G          0x40U   /* gyro 104Hz, ±250 dps */

#define CAP_FIFO_WORD_BYTES   7U      /* 1 tag byte + 6 data bytes per FIFO word */
#define CAP_FIFO_READ_WORDS   96U     /* max words per Service burst (~672 B ≈ 15 ms I²C @400 kHz) */
#define CAP_MAX_BURSTS_PER_SVC 12U    /* 12×96=1152 > 1023 FIFO depth — drains full snapshot per call */
#define CAP_MAX_MISSIONS      256U    /* mission-metadata ring depth (bookkeeping only; data in PSRAM).
                                       * FIFO-reclaimed (see Capture_AllocSlot): drained slots are reused,
                                       * so normal operation never exhausts it; the depth only bounds how
                                       * many captures survive a long radio-dark idle (most-recent-N kept).
                                       * 256 × sizeof(CaptureMission_t) ≈ 8 KB SRAM. */
#define CAP_RING_WTM_BYTES    (PSRAM_SIZE_BYTES - (PSRAM_SIZE_BYTES / 4U)) /* 75% = 6 MB drain trigger */
#define CAP_WTM_COOLDOWN_MS   600000U /* 10 min back-off after a failed watermark safety-flush drain (gateway down) — stops the radio spinning at max duty overnight (opposite of ADR-021 intent) */

/* Per-mission bookkeeping; the sample bytes themselves live in the PSRAM ring. */
typedef struct
{
    uint16_t mission_id;
    uint32_t start_offset;   /* byte offset into the PSRAM ring where this mission begins */
    uint32_t byte_count;     /* raw FIFO bytes captured */
    uint32_t word_count;     /* FIFO words captured (accel + gyro interleaved) */
    uint32_t overrun_evts;   /* FIFO overrun events seen during this mission (data loss markers) */
    uint32_t start_tick_ms;  /* HAL_GetTick() at mission start — drain t0 for the gateway */
    uint8_t  sealed;         /* 1 once MOVING→IDLE finalizes the mission */
    uint8_t  drained;        /* 1 once Drain_Mission has blasted it (piggyback bookkeeping) */
    uint8_t  is_idle_snapshot; /* 1 = low-rate IDLE snapshot, 0 = MOVING mission */
    int16_t  temp_c_x100;    /* cached HTS221 temp ×100 at seal (idle snapshots); 0x7FFF = invalid */
    uint16_t pressure_hpa_x10; /* cached LPS22HH pressure ×10 at seal (idle snapshots); 0 = invalid */
} CaptureMission_t;

static CaptureMission_t cap_missions[CAP_MAX_MISSIONS];
static uint16_t cap_mission_count = 0U;  /* slots populated so far (grows up to CAP_MAX_MISSIONS) */
static int16_t  cap_active_idx    = -1;  /* index of in-progress mission, -1 = none */
static uint16_t cap_slot_head     = 0U;  /* FIFO reuse pointer: oldest slot, advanced once the ring is full */
static uint32_t cap_ring_wptr     = 0U;  /* next PSRAM write offset [0, PSRAM_SIZE_BYTES) */
static uint16_t cap_next_id       = 1U;
static uint8_t  cap_initialized   = 0U;
static uint8_t  cap_wtm_hit       = 0U;  /* set when total un-drained bytes cross 75% (safety-flush trigger) */
static uint32_t cap_undrained_bytes = 0U; /* cross-mission accumulator: bytes captured but not yet drained */
static uint8_t  cap_snapshot_active    = 0U;  /* 1 while an idle snapshot is capturing */
static uint32_t cap_last_snapshot_tick = 0U;  /* HAL_GetTick() of the last snapshot start */
static uint32_t cap_snapshot_start_tick = 0U; /* HAL_GetTick() when the active snapshot began */
static uint32_t cap_words_window  = 0U;  /* words captured in the current 1 s log window */
static uint8_t  cap_fifo_buf[CAP_FIFO_READ_WORDS * CAP_FIFO_WORD_BYTES]; /* SRAM staging for the burst read */

/* ADR-020/021 drain protocol — blast the sealed PSRAM mission to the gateway on
 * UDP 5684. Frame layout is the authoritative contract in wire_protocol.md §2.
 * Phase 1 = blast-only (no back-channel); NAK/ACK ARQ layers on later (§9). */
#define DRAIN_PORT             5684U
#define DRAIN_MAGIC            0x52444C50UL  /* "PLDR" little-endian */
#define DRAIN_TYPE_BEGIN       1U
#define DRAIN_TYPE_CHUNK       2U
#define DRAIN_TYPE_END         3U
#define DRAIN_TYPE_ACK         6U            /* gateway→shuttle BEGIN liveness echo (delivery evidence, NOT ARQ; types 4/5 reserved for future NAK/ACK_COMPLETE) */
#define DRAIN_CHUNK_PAYLOAD    1400U         /* 200 FIFO words — never splits a word across chunks */
#define DRAIN_CTRL_REPEAT      3U            /* resend BEGIN/END N times (control-loss tolerance) */
#define DRAIN_ACK_WAIT_MS      150           /* per-attempt SO_RCVTIMEO while waiting for the BEGIN echo */
#define DRAIN_ACK_ATTEMPTS     5U            /* echo-wait attempts; >3 so leftover echoes from the prior mission (gateway replies per BEGIN ×3) can be skipped before this mission's fresh ack. Only the silent (gateway-down) case pays the full ~750 ms; queued packets return immediately */
#define DRAIN_CHUNK_PACE_EVERY 8U            /* yield 1 ms every N chunks so the EMW3080 MAC queue and the gateway UDP socket drain between bursts — mitigates bursty consecutive chunk loss */
#define DRAIN_WARMUP_PACKETS   24U           /* sacrificial datagrams to absorb the post-power-on loss window (~16 pkts measured) */
#define DRAIN_WARMUP_GAP_MS    8U            /* inter-packet pace so each junk pkt actually reaches air (advances ARP/MAC-learning) instead of piling into the SPI TX queue */
#define DRAIN_ODR_ACCEL_HZ     3332U
#define DRAIN_ODR_GYRO_HZ      416U

typedef struct __attribute__((packed))
{
  uint32_t magic; uint8_t type; uint8_t shuttle_id; uint16_t mission_id;
  uint16_t total_chunks; uint16_t odr_accel_hz; uint16_t odr_gyro_hz;
  int16_t  temp_c_x100;       /* idle-snapshot env stamp ×100; 0x7FFF = invalid */
  uint16_t pressure_hpa_x10;  /* idle-snapshot env stamp ×10; 0 = invalid */
  uint8_t  is_idle_snapshot;  /* 1 = low-rate idle snapshot, 0 = MOVING mission */
  uint8_t  _pad;
  uint32_t byte_count; uint32_t word_count; uint32_t t0_tick_ms;
  uint32_t tx_tick_ms;  /* HAL_GetTick() at drain time; gateway derives capture age = tx-t0 */
} DrainBegin_t;

typedef struct __attribute__((packed))
{
  uint32_t magic; uint8_t type; uint8_t shuttle_id; uint16_t mission_id;
  uint16_t chunk_seq; uint16_t total_chunks; uint16_t payload_len; uint32_t crc32;
} DrainChunkHdr_t;

typedef struct __attribute__((packed))
{
  uint32_t magic; uint8_t type; uint8_t shuttle_id; uint16_t mission_id;
  uint16_t total_chunks; uint16_t _pad; uint32_t crc32_all;
} DrainEnd_t;

typedef struct __attribute__((packed))
{
  uint32_t magic; uint8_t type; uint8_t shuttle_id; uint16_t mission_id;
} DrainAck_t;  /* 8 bytes — gateway BEGIN-liveness echo (type 6); proves the Jetson received our BEGIN */

static uint8_t drain_buf[sizeof(DrainChunkHdr_t) + DRAIN_CHUNK_PAYLOAD]; /* one chunk datagram staging */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void SystemPower_Config(void);
static void MX_GPIO_Init(void);
static void MX_ADF1_Init(void);
static void MX_I2C1_Init(void);
static void MX_I2C2_Init(void);
static void MX_ICACHE_Init(void);
static void MX_OCTOSPI1_Init(void);
static void MX_OCTOSPI2_Init(void);
static void MX_SPI2_Init(void);
static void MX_UART4_Init(void);
static void MX_USART1_UART_Init(void);
static void MX_UCPD1_Init(void);
static void MX_USB_OTG_FS_PCD_Init(void);
/* USER CODE BEGIN PFP */
static void WIFI_SPI_ApplySafeTiming(void);
static void WIFI_StatusCallback(uint8_t cate, uint8_t event, void *arg);
static uint8_t WIFI_IsIPv4Valid(const uint8_t ip_addr[4]);
static void WIFI_LogStationEvent(uint8_t event);
static MX_WIFI_STATUS_T WIFI_WaitForStationIP(uint8_t ip_addr[4], uint32_t timeout_ms);
static void WIFI_DelayWithYield(uint32_t delay_ms);
static void Drain_BindLocalPort(void);
static void TELEMETRY_RefreshEnvCache(void);
static int32_t TELEMETRY_Send(void);
static int8_t WIFI_PowerOn(void);
static void WIFI_PowerOff(void);
/* ADR-020 high-rate ISM330 FIFO capture engine (state-rated) */
static int8_t   Capture_Init(void);
static int16_t  Capture_AllocSlot(void);
static void     Capture_EnterMoving(void);
static void     Capture_EnterIdleSnapshot(void);
static void     Capture_EnterIdle(void);
static uint16_t Capture_Service(void);
/* ADR-020/021 mission drain to the gateway (UDP 5684, blast-first) */
static uint32_t Drain_CRC32(const uint8_t *data, uint32_t len);
static void     Drain_Mission(int16_t idx);
static void     Drain_AllPending(void);

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

// MX_WIFI SPI driver defines `wifi_obj_get` and `process_txrx_poll` in the
// BSP implementation (mx_wifi_spi.c), so we do not redefine them here.
static void WIFI_SPI_ApplySafeTiming(void)
{
  /*
   * CubeMX generated SPI2 at 80 MHz on this clock tree, which is too fast for
   * the MXCHIP link. Re-apply a conservative configuration before probing WiFi.
   */
  hspi2.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_16;
  hspi2.Init.NSSPMode = SPI_NSS_PULSE_DISABLE;

  if (HAL_SPI_Init(&hspi2) != HAL_OK)
  {
    Error_Handler();
  }
}

static void WIFI_StatusCallback(uint8_t cate, uint8_t event, void *arg)
{
  (void)arg;

  if (cate != MC_STATION)
  {
    return;
  }

  wifi_station_event = event;

  if (event == MWIFI_EVENT_STA_GOT_IP)
  {
    wifi_station_ready = 1U;
  }
  else if (event == MWIFI_EVENT_STA_DOWN)
  {
    wifi_station_ready = 0U;
  }
}

static uint8_t WIFI_IsIPv4Valid(const uint8_t ip_addr[4])
{
  return (uint8_t)((ip_addr[0] | ip_addr[1] | ip_addr[2] | ip_addr[3]) != 0U);
}

static void WIFI_LogStationEvent(uint8_t event)
{
  const char *event_name = "UNKNOWN";

  if (event == MWIFI_EVENT_STA_UP)
  {
    event_name = "STA_UP";
  }
  else if (event == MWIFI_EVENT_STA_DOWN)
  {
    event_name = "STA_DOWN";
  }
  else if (event == MWIFI_EVENT_STA_GOT_IP)
  {
    event_name = "STA_GOT_IP";
  }

  sprintf(uart_buf, "[NETWORK] WiFi event: %s\r\n", event_name);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
}

/* ADR-021 recovery net: hand-rolled independent watchdog. The HAL IWDG module is not
 * enabled in this CubeMX project, so we drive the IWDG registers directly (CMSIS only,
 * no HAL/.ioc dependency). The EMW3080 drain re-init (WIFI_PowerOn) can wedge forever
 * on a silent SPI/EXTI handshake — the BSP's own MX_WIFI_CMD_TIMEOUT never advances
 * when the module is mute. A hang stops kicking the dog, so the chip resets (~16 s) and
 * re-inits cleanly on the next boot instead of freezing in the field until manual reset. */
#define IWDG_PR_DIV128   (0x05U)    /* prescaler /128 (LSI nominal 32 kHz) */
#define IWDG_RLR_RELOAD  (0x0FFFU)  /* 4095 -> (4096 * 128) / 32000 ~= 16.4 s timeout */

/* Start the IWDG with a ~16 s period. Period must exceed the longest single blocking
 * BSP call (MX_WIFI_CMD_TIMEOUT = 10 s) so a slow-but-alive module is not falsely reset.
 * Once started the IWDG cannot be stopped (hardware), so arm only after boot bring-up. */
static void IWDG_Arm(void)
{
  IWDG->KR  = 0x0000CCCCU;     /* start watchdog (also forces LSI on) */
  IWDG->KR  = 0x00005555U;     /* unlock PR/RLR for write */
  IWDG->PR  = IWDG_PR_DIV128;
  IWDG->RLR = IWDG_RLR_RELOAD;
  while (IWDG->SR != 0U) { }   /* wait for PR/RLR to sync into the LSI clock domain */
  IWDG->KR  = 0x0000AAAAU;     /* initial reload */
}

/* Refresh the watchdog counter. No-op before IWDG_Arm() (the reload key does nothing
 * until the watchdog is started), so it is safe on code paths shared by boot and drain. */
static void IWDG_Kick(void)
{
  IWDG->KR = 0x0000AAAAU;
}

static MX_WIFI_STATUS_T WIFI_WaitForStationIP(uint8_t ip_addr[4], uint32_t timeout_ms)
{
  uint32_t start_tick = HAL_GetTick();
  uint32_t last_progress_log = start_tick;
  uint8_t last_event = 0xFF;

  if ((wifi_obj == NULL) || (ip_addr == NULL))
  {
    return MX_WIFI_STATUS_PARAM_ERROR;
  }

  (void)memset(ip_addr, 0, 4);

  while ((HAL_GetTick() - start_tick) < timeout_ms)
  {
    IWDG_Kick();  /* forward progress: DHCP wait can legitimately run up to timeout_ms */
    (void)MX_WIFI_IO_YIELD(wifi_obj, 100);

    if (wifi_station_event != last_event)
    {
      last_event = wifi_station_event;
      WIFI_LogStationEvent(last_event);
    }

    if ((MX_WIFI_GetIPAddress(wifi_obj, ip_addr, MC_STATION) == MX_WIFI_STATUS_OK) &&
        WIFI_IsIPv4Valid(ip_addr))
    {
      wifi_station_ready = 1U;
      return MX_WIFI_STATUS_OK;
    }

    if ((HAL_GetTick() - last_progress_log) >= 1000U)
    {
      last_progress_log = HAL_GetTick();
      sprintf(uart_buf, "[NETWORK] Waiting for DHCP lease...\r\n");
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    }
  }

  return MX_WIFI_STATUS_TIMEOUT;
}

static void WIFI_DelayWithYield(uint32_t delay_ms)
{
  uint32_t start_tick = HAL_GetTick();

  while ((HAL_GetTick() - start_tick) < delay_ms)
  {
    IWDG_Kick();  /* forward progress during link-settle / warm-up gap waits */
    uint32_t elapsed = HAL_GetTick() - start_tick;
    uint32_t remaining = delay_ms - elapsed;
    uint32_t slice_ms = (remaining > 10U) ? 10U : remaining;

    if (slice_ms == 0U)
    {
      break;
    }

    if ((wifi_obj != NULL) && (wifi_driver_initialized != 0U))
    {
      (void)MX_WIFI_IO_YIELD(wifi_obj, slice_ms);
    }
    else
    {
      HAL_Delay(slice_ms);
    }
  }
}

/* Refresh HTS221 cache values. Cached values stamp every TX so the I²C bus
 * does not block the 50 Hz transmit path.
 * Cache is only updated on successful read — preserves last-known value when
 * HTS221 (1 Hz ODR) has no new data ready during a 2 Hz poll. */
static void TELEMETRY_RefreshEnvCache(void)
{
  float new_temp = 0.0f;
  float new_hum  = 0.0f;

  if (hts221_initialized != 0U)
  {
    /* Update cache only on success; keep previous value on "not ready" or I2C error. */
    if (SENSOR_Humidity_Read(&hi2c2, &new_temp, &new_hum) == 0)
    {
      cached_temp_c       = new_temp;
      cached_humidity_pct = new_hum;
    }
  }

  if (lps22hh_initialized != 0U)
  {
    /* Same cache-on-success policy; pressure stamps the idle snapshots (ADR-021 §1). */
    float new_press = 0.0f;
    if (SENSOR_Pressure_Read(&hi2c2, &new_press) == 0)
    {
      cached_pressure_hpa = new_press;
    }
  }
}

/* Tiny base-10 parser for a uint8 prefix of `s`. Stops at the first
 * non-digit; returns 0 on empty input. Used only for beacon shuttle-list
 * parsing — kept local to avoid pulling stdlib atoi/strtol into the image. */
static uint8_t BEACON_ParseUint8(const char *s)
{
  uint8_t v = 0U;
  while ((*s >= '0') && (*s <= '9'))
  {
    v = (uint8_t)((v * 10U) + (uint8_t)(*s - '0'));
    s++;
  }
  return v;
}

/* Listen on BEACON_PORT for a "PLUDOS-GW:<ip>[:csv-ids]" broadcast from the gateway.
 *
 * Two beacon dialects are accepted:
 *   "PLUDOS-GW:<ip>"           — legacy / single-Jetson dev: any shuttle bonds.
 *   "PLUDOS-GW:<ip>:<csv-ids>" — multi-Jetson: only bond if SHUTTLE_ID is in the
 *                                comma-separated list (e.g. "1,2" or "3,4").
 *
 * retries: how many recvfrom attempts before giving up.
 * timeout_ms: per-attempt recv timeout (passed to MX_SO_RCVTIMEO).
 *
 * On success, sets jetson_ip and returns 1. A beacon whose shuttle-list does
 * not include SHUTTLE_ID is silently skipped — the loop continues listening
 * within the retry budget so a same-WiFi beacon from this shuttle's own Jetson
 * can still be caught in a later attempt.
 *
 * On timeout, returns 0 without touching jetson_ip. */
static uint8_t BEACON_Run(uint8_t retries, int32_t timeout_ms)
{
  int32_t               bsock;
  struct mx_sockaddr_in baddr  = {0};
  struct mx_sockaddr_in sender = {0};
  uint32_t              fromlen;
  uint8_t               buf[40] = {0};
  int32_t               n;
  uint8_t               attempt;

  bsock = MX_WIFI_Socket_create(wifi_obj, MX_AF_INET, MX_SOCK_DGRAM, MX_IPPROTO_UDP);
  if (bsock < 0)
  {
    sprintf(uart_buf, "[BEACON] Socket create failed\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    return 0U;
  }

  baddr.sin_len         = sizeof(baddr);
  baddr.sin_family      = MX_AF_INET;
  baddr.sin_port        = PLUDOS_HTONS(BEACON_PORT);
  baddr.sin_addr.s_addr = 0U; /* INADDR_ANY */

  if (MX_WIFI_Socket_bind(wifi_obj, bsock, (struct mx_sockaddr *)&baddr, sizeof(baddr)) != MX_WIFI_STATUS_OK)
  {
    sprintf(uart_buf, "[BEACON] Bind on port %u failed\r\n", (unsigned)BEACON_PORT);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    (void)MX_WIFI_Socket_close(wifi_obj, bsock);
    return 0U;
  }

  (void)MX_WIFI_Socket_setsockopt(wifi_obj, bsock, MX_SOL_SOCKET, MX_SO_RCVTIMEO,
                                  &timeout_ms, sizeof(timeout_ms));

  sprintf(uart_buf, "[BEACON] Listening on UDP %u (%u x %ld ms)...\r\n",
          (unsigned)BEACON_PORT, (unsigned)retries, (long)timeout_ms);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

  for (attempt = 0U; attempt < retries; attempt++)
  {
    fromlen = sizeof(sender);
    memset(buf, 0, sizeof(buf));
    n = MX_WIFI_Socket_recvfrom(wifi_obj, bsock, buf, (int32_t)(sizeof(buf) - 1U),
                                0, (struct mx_sockaddr *)&sender, &fromlen);

    /* "PLUDOS-GW:" prefix is 10 chars; shortest valid IP suffix is 7 ("1.2.3.4"). */
    if ((n > 10) && (strncmp((char *)buf, "PLUDOS-GW:", 10) == 0))
    {
      char    *ip_start;
      char    *id_sep;
      uint8_t  group_match;

      buf[n] = 0U; /* safe: buf is 40 B and recvfrom is capped at sizeof(buf)-1 */

      /* Split optional ":<csv-ids>" suffix from the IP. The first ':' belongs
       * to "PLUDOS-GW:" (already past); the next one, if present, starts the
       * comma-separated shuttle-id list. */
      ip_start = (char *)buf + 10;
      id_sep   = strchr(ip_start, ':');

      group_match = 1U;
      if (id_sep != NULL)
      {
        char *cursor;
        *id_sep = 0;          /* terminate the IP portion in place */
        cursor  = id_sep + 1;

        group_match = 0U;
        while (*cursor != 0)
        {
          if (BEACON_ParseUint8(cursor) == (uint8_t)SHUTTLE_ID)
          {
            group_match = 1U;
            break;
          }
          /* Skip to the character after the next comma. */
          while ((*cursor != 0) && (*cursor != ','))
          {
            cursor++;
          }
          if (*cursor == ',')
          {
            cursor++;
          }
        }
      }

      if (group_match == 0U)
      {
        /* Beacon is for a different shuttle group — keep listening within budget. */
        sprintf(uart_buf, "[BEACON] Ignored beacon (different group): %s:%s\r\n",
                ip_start, id_sep + 1);
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
        continue;
      }

      /* Subnet filter: only bond if the gateway IP is in the same /24 as this
       * STM32's WiFi address. Rejects beacons sourced from VPN/Tailscale
       * interfaces (e.g. 172.31.x.x) that the STM32 cannot reach via WiFi. */
      if (stm32_ip[0] != 0U)
      {
        uint8_t         b0 = BEACON_ParseUint8(ip_start);
        const char     *d1 = strchr(ip_start, '.');
        uint8_t         b1 = d1 ? BEACON_ParseUint8(d1 + 1U) : 0U;
        const char     *d2 = d1 ? strchr(d1 + 1U, '.') : NULL;
        uint8_t         b2 = d2 ? BEACON_ParseUint8(d2 + 1U) : 0U;

        if ((b0 != stm32_ip[0]) || (b1 != stm32_ip[1]) || (b2 != stm32_ip[2]))
        {
          sprintf(uart_buf,
                  "[BEACON] Ignored (wrong subnet %u.%u.%u.x, STM32 is %u.%u.%u.x): %s\r\n",
                  b0, b1, b2, stm32_ip[0], stm32_ip[1], stm32_ip[2], ip_start);
          HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
          continue;
        }
      }

      strncpy(jetson_ip, ip_start, sizeof(jetson_ip) - 1U);
      jetson_ip[sizeof(jetson_ip) - 1U] = 0;
      sprintf(uart_buf, "[BEACON] Gateway found: %s\r\n", jetson_ip);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      (void)MX_WIFI_Socket_close(wifi_obj, bsock);
      return 1U;
    }

    if (retries > 1U) /* suppress noisy log on single-shot quick checks */
    {
      sprintf(uart_buf, "[BEACON] No beacon yet (attempt %u/%u)\r\n",
              (unsigned)(attempt + 1U), (unsigned)retries);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    }
  }

  (void)MX_WIFI_Socket_close(wifi_obj, bsock);
  return 0U;
}

/* Send one PludosTelemetry packet via raw UDP. Fire-and-forget — no ACK, no retry.
 * Returns the byte count written by sendto, or -1 if socket/WiFi is not ready. */
static int32_t TELEMETRY_Send(void)
{
  PludosTelemetry_t pkt = {0};
  struct mx_sockaddr_in dest = {0};
  int32_t sent;

  if ((socket_id < 0) || (wifi_station_ready == 0U) || (jetson_ip[0] == 0))
  {
    return -1;
  }

  pkt.shuttle_id   = SHUTTLE_ID;
  pkt.sequence_id  = current_packet_num;
  pkt.tick_ms      = HAL_GetTick();
  pkt.state        = (uint8_t)current_state;

  /* Scale floats to int16 at 2 dp precision. 0x7FFF sentinel for any unavailable field.
   * vib_x == 99.0f is the accel failure sentinel (99g > ±2g FS — impossible real value). */
  pkt.accel_x      = (vib_x < 90.0f) ? (int16_t)(vib_x * 100.0f) : (int16_t)0x7FFF;
  pkt.accel_y      = (vib_y < 90.0f) ? (int16_t)(vib_y * 100.0f) : (int16_t)0x7FFF;
  pkt.accel_z      = (vib_z < 90.0f) ? (int16_t)(vib_z * 100.0f) : (int16_t)0x7FFF;
  pkt.gyro_x       = ism330_gyro_ok ? (int16_t)(gyro_x * 100.0f) : (int16_t)0x7FFF;
  pkt.gyro_y       = ism330_gyro_ok ? (int16_t)(gyro_y * 100.0f) : (int16_t)0x7FFF;
  pkt.gyro_z       = ism330_gyro_ok ? (int16_t)(gyro_z * 100.0f) : (int16_t)0x7FFF;
  /* cached_temp_c == -999.0f is the HTS221 failure sentinel. */
  pkt.temp_c       = (cached_temp_c > -998.0f) ? (int16_t)(cached_temp_c * 100.0f) : (int16_t)0x7FFF;
  pkt.humidity_pct = (cached_temp_c > -998.0f) ? (int16_t)(cached_humidity_pct * 10.0f) : (int16_t)0x7FFF;

  dest.sin_len         = sizeof(dest);
  dest.sin_family      = MX_AF_INET;
  dest.sin_port        = PLUDOS_HTONS(TELEMETRY_PORT);
  dest.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  sent = MX_WIFI_Socket_sendto(wifi_obj, socket_id, (uint8_t *)&pkt, sizeof(pkt),
                               0, (struct mx_sockaddr *)&dest, sizeof(dest));

  if (sent == (int32_t)sizeof(pkt))
  {
    current_packet_num++;
    tx_count_window++;
  }

  return sent;
}

#if BENCH_THROUGHPUT
/* One-shot UDP throughput benchmark. Blasts datagrams as fast as
 * MX_WIFI_Socket_sendto allows, per payload size, and reports achieved pkt/s
 * and Mbps over UART. The sender-side rate is the EMW3080 ceiling (sendto
 * backpressures at the module's real throughput). 1472 B = 1500 MTU − 20 IP
 * − 8 UDP, the largest payload that avoids IPv4 fragmentation. */
static void TELEMETRY_BenchThroughput(void)
{
  static uint8_t bench_buf[1472];
  const uint16_t sizes[] = {24U, 256U, 512U, 1024U, 1472U};
  struct mx_sockaddr_in dest = {0};

  if ((socket_id < 0) || (wifi_station_ready == 0U) || (jetson_ip[0] == 0))
  {
    sprintf(uart_buf, "[BENCH] skipped: socket/WiFi not ready\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
    return;
  }

  dest.sin_len         = sizeof(dest);
  dest.sin_family      = MX_AF_INET;
  dest.sin_port        = PLUDOS_HTONS(TELEMETRY_PORT);
  dest.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  for (uint32_t s = 0U; s < (sizeof(sizes) / sizeof(sizes[0])); s++)
  {
    uint16_t len = sizes[s];
    uint32_t seq = 0U, ok = 0U, fail = 0U, bytes = 0U;
    uint32_t t0 = HAL_GetTick();

    while ((HAL_GetTick() - t0) < 3000U)   /* 3 s per payload size */
    {
      seq++;
      bench_buf[0] = (uint8_t)(seq >> 24); /* big-endian seq → loss check on the receiver */
      bench_buf[1] = (uint8_t)(seq >> 16);
      bench_buf[2] = (uint8_t)(seq >> 8);
      bench_buf[3] = (uint8_t)(seq);
      int32_t r = MX_WIFI_Socket_sendto(wifi_obj, socket_id, bench_buf, len,
                                        0, (struct mx_sockaddr *)&dest, sizeof(dest));
      if (r == (int32_t)len) { ok++; bytes += len; } else { fail++; }
    }

    uint32_t dt   = HAL_GetTick() - t0;                                   /* ms */
    uint32_t pps  = (dt > 0U) ? ((ok * 1000U) / dt) : 0U;
    uint32_t kbps = (dt > 0U) ? (uint32_t)(((uint64_t)bytes * 8U) / dt) : 0U; /* bytes*8/ms = kbit/s */
    sprintf(uart_buf, "[BENCH] size=%4u ok=%6lu fail=%6lu  %5lu pkt/s  %lu.%03lu Mbps\r\n",
            (unsigned)len, (unsigned long)ok, (unsigned long)fail, (unsigned long)pps,
            (unsigned long)(kbps / 1000U), (unsigned long)(kbps % 1000U));
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 2000);
    HAL_Delay(200);
  }

  sprintf(uart_buf, "[BENCH] done — resuming normal telemetry\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
}
#endif /* BENCH_THROUGHPUT */

/* Post-association settle: time to wait after the socket is armed before the
 * caller's first send, so the AP can learn our MAC / resolve ARP and the first
 * UDP datagrams aren't dropped. Tune empirically (paid in IDLE, latency-free). */
#define WIFI_LINK_SETTLE_MS  1200U

/* Power up the EMW3080 and bring the station fully online: probe the SPI bus
 * (once), hard-reset the module, init the driver, register the status callback,
 * connect to the AP, wait for a DHCP lease and arm the telemetry UDP socket.
 * Returns 0 on success (socket armed), -1 on any failure. Re-runnable after
 * WIFI_PowerOff(): the held-in-reset module comes back cold exactly like boot,
 * so we always hard-reset + re-Init here. This is the on-demand power gate for
 * the ADR-020 buffer-then-drain model — WiFi is only powered when draining. */
static int8_t WIFI_PowerOn(void)
{
  WIFI_SPI_ApplySafeTiming();  /* re-apply the MXCHIP-safe SPI2 timing (idempotent) */

  /* Probe registers the host-side SPI bus driver — needed once per boot only. */
  if (wifi_obj == NULL)
  {
    void *ctx = NULL;
    if ((mxwifi_probe(&ctx) != 0) || (ctx == NULL))
    {
      sprintf(uart_buf, "[NETWORK] ERROR: WiFi SPI bus registration failed\r\n");
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      return -1;
    }
    wifi_obj = (MX_WIFIObject_t *)ctx;
  }

  /* Always hard-reset: releases the RESET pin and reboots the module from the
   * held-in-reset (off) state into a clean SPI handshake. */
  IWDG_Kick();  /* fresh window: each BSP call below can block up to MX_WIFI_CMD_TIMEOUT */
  if (MX_WIFI_HardResetModule(wifi_obj) != MX_WIFI_STATUS_OK)
  {
    sprintf(uart_buf, "[NETWORK] ERROR: hard reset failed\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    wifi_driver_initialized = 0U;
    return -1;
  }

  IWDG_Kick();
  if (MX_WIFI_Init(wifi_obj) != MX_WIFI_STATUS_OK)
  {
    sprintf(uart_buf, "[NETWORK] ERROR: module init failed\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    wifi_driver_initialized = 0U;
    return -1;
  }

  wifi_driver_initialized          = 1U;
  wifi_obj->NetSettings.DHCP_IsEnabled = 1U;
  wifi_station_event               = 0xFF;
  wifi_station_ready               = 0U;
  (void)MX_WIFI_RegisterStatusCallback_if(wifi_obj, WIFI_StatusCallback, NULL, MC_STATION);

  IWDG_Kick();
  if (MX_WIFI_Connect(wifi_obj, WIFI_SSID, WIFI_PASSWORD, MX_WIFI_SEC_AUTO) != MX_WIFI_STATUS_OK)
  {
    sprintf(uart_buf, "[NETWORK] ERROR: connect failed (check SSID/password)\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    return -1;
  }

  uint8_t ip_addr[4] = {0};
  if (WIFI_WaitForStationIP(ip_addr, 15000U) != MX_WIFI_STATUS_OK)
  {
    sprintf(uart_buf, "[NETWORK] ERROR: no DHCP lease\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    return -1;
  }
  sprintf(uart_buf, "[NETWORK] SUCCESS! Station IP: %d.%d.%d.%d\r\n",
          ip_addr[0], ip_addr[1], ip_addr[2], ip_addr[3]);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  (void)memcpy(stm32_ip, ip_addr, 4U);

  /* Resolve the gateway by beacon only the first time; keep the cached IP across
   * later power cycles so re-draining does not pay the beacon wait every time. */
  if (jetson_ip[0] == 0)
  {
    if (BEACON_Run(BEACON_MAX_RETRIES, BEACON_TIMEOUT_MS) == 0U)
    {
      strncpy(jetson_ip, JETSON_IP, sizeof(jetson_ip) - 1U);
      jetson_ip[sizeof(jetson_ip) - 1U] = 0;
      sprintf(uart_buf, "[BEACON] Timed out — fallback IP: %s\r\n", JETSON_IP);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    }
  }

  socket_id = MX_WIFI_Socket_create(wifi_obj, MX_AF_INET, MX_SOCK_DGRAM, MX_IPPROTO_UDP);
  if (socket_id < 0)
  {
    sprintf(uart_buf, "[NETWORK] ERROR: failed to create UDP socket\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    return -1;
  }
  sprintf(uart_buf, "[NETWORK] PludosTelemetry stream armed → udp://%s:%u\r\n",
          jetson_ip, (unsigned)TELEMETRY_PORT);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

  /* Bind the local port so the gateway's DrainAck reaches Drain_WaitForAck. */
  Drain_BindLocalPort();

  /* ADR-021: a DHCP lease does not mean the data path is flushed. The AP still
   * has to learn our MAC and resolve ARP, so the first few UDP datagrams after a
   * fresh association are silently dropped — fatal for a single-chunk idle-snapshot
   * drain that has no chunk redundancy to recover. Settle (yielding to the SPI
   * driver) before the caller's first send so that first BEGIN actually lands.
   * Paid in IDLE where latency is free. Tune WIFI_LINK_SETTLE_MS empirically. */
  WIFI_DelayWithYield(WIFI_LINK_SETTLE_MS);
  return 0;
}

/* Power down the EMW3080 to the lowest-power state: close the socket, leave the
 * AP, then hold the module in hardware reset (RESET pin = WRLS_WKUP_W low). The
 * host-side SPI bus stays registered (wifi_obj kept) so WIFI_PowerOn() can bring
 * it straight back without re-probing. Clears the ready flags so the main loop's
 * WiFi paths stay quiescent until the next WIFI_PowerOn(). */
static void WIFI_PowerOff(void)
{
  if (socket_id >= 0)
  {
    (void)MX_WIFI_Socket_close(wifi_obj, socket_id);
    socket_id = -1;
  }
  if ((wifi_obj != NULL) && (wifi_driver_initialized != 0U))
  {
    (void)MX_WIFI_Disconnect(wifi_obj);
  }
  /* Assert RESET low — module fully off; drains no radio current until PowerOn. */
  HAL_GPIO_WritePin(WRLS_WKUP_W_GPIO_Port, WRLS_WKUP_W_Pin, GPIO_PIN_RESET);

  wifi_driver_initialized = 0U;
  wifi_station_ready      = 0U;
  wifi_station_event      = 0xFF;

  sprintf(uart_buf, "[NETWORK] WiFi powered off (module held in reset)\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
}

/* Put the FIFO in bypass and reset capture bookkeeping. Call once after the ISM330
 * boot init; PSRAM must already be memory-mapped (PSRAM_Init). Returns 0 on success. */
static int8_t Capture_Init(void)
{
  uint8_t bypass = CAP_FIFO_MODE_BYPASS;
  if (HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL4, 1, &bypass, 1, 100) != HAL_OK)
  {
    return -1;
  }
  cap_ring_wptr     = 0U;
  cap_mission_count = 0U;
  cap_slot_head     = 0U;
  cap_active_idx    = -1;
  cap_next_id       = 1U;
  cap_wtm_hit       = 0U;
  cap_initialized   = 1U;
  return 0;
}

/* Pick the metadata slot for a new capture. While the table has room we append; once
 * full we reclaim in FIFO order (oldest slot first), advancing cap_slot_head. Because
 * every mission is marked drained on the next radio-on, normal operation cycles through
 * already-drained slots and never exhausts the table — only a long radio-dark idle, where
 * snapshots accumulate undrained, can wrap, and then we keep the most recent
 * CAP_MAX_MISSIONS captures and drop the oldest. Dropping a still-undrained slot returns
 * its bytes to cap_undrained_bytes so the watermark trigger stays honest. */
static int16_t Capture_AllocSlot(void)
{
  int16_t idx;
  CaptureMission_t *old;

  if (cap_mission_count < CAP_MAX_MISSIONS)
  {
    idx = (int16_t)cap_mission_count;
    cap_mission_count++;
    return idx;
  }

  idx = (int16_t)cap_slot_head;
  cap_slot_head = (uint16_t)((cap_slot_head + 1U) % CAP_MAX_MISSIONS);

  old = &cap_missions[idx];
  if ((old->drained == 0U) && (old->byte_count != 0U))
  {
    cap_undrained_bytes = (cap_undrained_bytes >= old->byte_count)
                          ? (cap_undrained_bytes - old->byte_count) : 0U;
  }
  return idx;
}

/* Switch the ISM330 to high-rate capture (accel 3332 Hz, gyro 416 Hz, FIFO stream)
 * and open a new mission in the PSRAM ring. FS/scaling stay at the live ±2 g/±250 dps,
 * so the FSM's decimated OUTX read keeps working unchanged. */
static void Capture_EnterMoving(void)
{
  uint8_t v;
  CaptureMission_t *m;

  if (cap_initialized == 0U)
  {
    return;
  }

  /* High-rate sensor config (gyro LPF1 CTRL4_C/CTRL6_C left at boot FTYPE=111). */
  v = CAP_CTRL1_XL_MOVING; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL1_XL, 1, &v, 1, 100);
  v = CAP_CTRL8_XL_MOVING; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL8_XL, 1, &v, 1, 100);
  v = CAP_CTRL2_G_MOVING;  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL2_G,  1, &v, 1, 100);
  fsm_settle_until_tick = HAL_GetTick() + ACCEL_SETTLE_MS; /* ODR just changed — blank trigger while LPF2 re-settles */

  /* Batch rates, then BYPASS→STREAM toggle to flush any stale words before capture. */
  v = CAP_FIFO_CTRL3_MOVING; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL3, 1, &v, 1, 100);
  v = CAP_FIFO_MODE_BYPASS;  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL4, 1, &v, 1, 100);
  v = CAP_FIFO_MODE_STREAM;  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL4, 1, &v, 1, 100);

  /* Open a mission record (FIFO metadata ring; the PSRAM data ring wraps independently). */
  cap_active_idx = Capture_AllocSlot();
  m = &cap_missions[cap_active_idx];
  m->mission_id   = cap_next_id++;
  m->start_offset = cap_ring_wptr;
  m->byte_count   = 0U;
  m->word_count   = 0U;
  m->overrun_evts  = 0U;
  m->start_tick_ms = HAL_GetTick();
  m->sealed        = 0U;
  m->drained       = 0U;
  m->is_idle_snapshot = 0U;
  m->temp_c_x100      = (int16_t)0x7FFF;  /* stamped at seal from the env cache */
  m->pressure_hpa_x10 = 0U;

  sprintf(uart_buf, "[CAPTURE] mission %u start @0x%06lX (accel 3332Hz, gyro 416Hz)\r\n",
          (unsigned)m->mission_id, (unsigned long)m->start_offset);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
}

/* ADR-021 §1: open a low-rate IDLE snapshot (accel+gyro both 12.5 Hz, FIFO stream).
 * Same chip/axes/path as MOVING capture — only the ODR differs — so idle and mission
 * data compare directly in the shared sub-6 Hz band. Sealed by Capture_EnterIdle();
 * drains piggyback on the next MOVING→IDLE WiFi wake (no radio cost here). */
static void Capture_EnterIdleSnapshot(void)
{
  uint8_t v;
  CaptureMission_t *m;

  if ((cap_initialized == 0U) || (cap_active_idx >= 0))
  {
    return; /* nothing if uninitialised or a capture is already in progress */
  }

  /* Low-rate config; CTRL8_XL left at LIVE (anti-alias cutoff already < Nyquist). */
  v = CAP_CTRL1_XL_IDLE; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL1_XL, 1, &v, 1, 100);
  v = CAP_CTRL2_G_IDLE;  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL2_G,  1, &v, 1, 100);
  fsm_settle_until_tick = HAL_GetTick() + ACCEL_SETTLE_MS; /* 104Hz→12.5Hz resets LPF2 — blank trigger to kill the phantom-MOVING transient */

  /* Batch rates, then BYPASS→STREAM toggle to flush any stale words before capture. */
  v = CAP_FIFO_CTRL3_IDLE;  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL3, 1, &v, 1, 100);
  v = CAP_FIFO_MODE_BYPASS; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL4, 1, &v, 1, 100);
  v = CAP_FIFO_MODE_STREAM; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL4, 1, &v, 1, 100);

  cap_active_idx = Capture_AllocSlot();
  m = &cap_missions[cap_active_idx];
  m->mission_id   = cap_next_id++;
  m->start_offset = cap_ring_wptr;
  m->byte_count   = 0U;
  m->word_count   = 0U;
  m->overrun_evts  = 0U;
  m->start_tick_ms = HAL_GetTick();
  m->sealed        = 0U;
  m->drained       = 0U;
  m->is_idle_snapshot = 1U;
  m->temp_c_x100      = (int16_t)0x7FFF;  /* stamped at seal from the env cache */
  m->pressure_hpa_x10 = 0U;

  sprintf(uart_buf, "[CAPTURE] idle snapshot %u start @0x%06lX (accel/gyro 12.5Hz)\r\n",
          (unsigned)m->mission_id, (unsigned long)m->start_offset);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
}

/* Drain pending FIFO words into the PSRAM ring. Returns the number of words copied
 * this call. The MOVING loop runs at ~25 Hz, far slower than the 3749 word/s FIFO
 * production rate, so a single 96-word burst per call cannot keep up (96×25 < 3749)
 * and the FIFO overruns. We therefore loop bursts until the FIFO is empty, bounded by
 * CAP_MAX_BURSTS_PER_SVC (12×96 > 1023 = full FIFO depth) so the call still terminates. */
static uint16_t Capture_Service(void)
{
  uint8_t  s1, s2, burst;
  uint16_t diff, n;
  uint32_t bytes, i;
  uint16_t total = 0U;
  volatile uint8_t *ring;
  CaptureMission_t *m;

  if ((cap_initialized == 0U) || (cap_active_idx < 0))
  {
    return 0U;
  }

  m    = &cap_missions[cap_active_idx];
  ring = (volatile uint8_t *)PSRAM_BASE_ADDR;

  for (burst = 0U; burst < CAP_MAX_BURSTS_PER_SVC; burst++)
  {
    if (HAL_I2C_Mem_Read(&hi2c2, ISM330_ADDR, ISM330_FIFO_STATUS1, 1, &s1, 1, 100) != HAL_OK)
    {
      break;
    }
    if (HAL_I2C_Mem_Read(&hi2c2, ISM330_ADDR, ISM330_FIFO_STATUS2, 1, &s2, 1, 100) != HAL_OK)
    {
      break;
    }

    if ((s2 & 0x40U) != 0U)
    {
      m->overrun_evts++; /* fifo_ovr_ia: FIFO filled faster than drained — words were lost */
    }

    diff = (uint16_t)s1 | ((uint16_t)(s2 & 0x03U) << 8); /* 10-bit unread word count */
    if (diff == 0U)
    {
      break; /* FIFO empty — snapshot fully drained */
    }
    n = (diff > CAP_FIFO_READ_WORDS) ? CAP_FIFO_READ_WORDS : diff;

    /* One burst read of n words. FIFO_DATA_OUT (0x78..0x7E) auto-wraps 0x7E→0x78 per word
     * on a multi-byte read (ISM330DHCX datasheet), so n*7 contiguous bytes from 0x78 are
     * n back-to-back [tag, X_L, X_H, Y_L, Y_H, Z_L, Z_H] words. */
    if (HAL_I2C_Mem_Read(&hi2c2, ISM330_ADDR, ISM330_FIFO_DATA_TAG, 1,
                         cap_fifo_buf, (uint16_t)(n * CAP_FIFO_WORD_BYTES), 100) != HAL_OK)
    {
      break;
    }

    /* Copy raw words into the memory-mapped PSRAM ring, wrapping at the 8 MB boundary.
     * Tags are preserved so the gateway demuxes accel (0x02) vs gyro (0x01) and rebuilds
     * each stream's timeline as t0 + index/ODR (sampling_strategy.md §12). */
    bytes = (uint32_t)n * CAP_FIFO_WORD_BYTES;
    /* DEFER (known limitation): cap_ring_wptr wraps with no live-mission collision check
     * and m->byte_count is uncapped, so a wrap during repeated drain failures can overwrite
     * still-undrained data. Unreachable in normal ops (missions ~1.3 MB << 8 MB ring); only
     * bites after many consecutive failed drains. Not fixed here — tracked separately. */
    for (i = 0U; i < bytes; i++)
    {
      ring[cap_ring_wptr] = cap_fifo_buf[i];
      cap_ring_wptr++;
      if (cap_ring_wptr >= PSRAM_SIZE_BYTES)
      {
        cap_ring_wptr = 0U; /* ring wrap */
      }
    }
    m->byte_count += bytes;
    m->word_count += n;
    cap_undrained_bytes += bytes; /* cross-mission total, reset only when missions drain */
    total = (uint16_t)(total + n);

    if (n < CAP_FIFO_READ_WORDS)
    {
      break; /* read everything that was queued — no full burst pending */
    }
  }

  /* Safety-flush watermark (ADR-021 §1): total un-drained bytes across all missions
   * crossing ~75% of the ring forces a WiFi-on drain even mid-idle (overnight park). */
  if ((cap_wtm_hit == 0U) && (cap_undrained_bytes >= CAP_RING_WTM_BYTES))
  {
    cap_wtm_hit = 1U;
  }

  return total;
}

/* Finalize the active mission: empty the FIFO, stop it (bypass), restore the low-rate
 * live config, and seal the mission record for the drain stage. */
static void Capture_EnterIdle(void)
{
  uint8_t v, i;
  CaptureMission_t *m;

  if ((cap_initialized == 0U) || (cap_active_idx < 0))
  {
    return;
  }

  /* Pull remaining queued words; 12 × 96 > 1023 FIFO depth, so the FIFO fully empties. */
  for (i = 0U; i < 12U; i++)
  {
    if (Capture_Service() == 0U)
    {
      break;
    }
  }

  v = CAP_FIFO_MODE_BYPASS; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, ISM330_FIFO_CTRL4, 1, &v, 1, 100);

  /* Restore the anti-aliased live config (matches the boot init in USER CODE 2). */
  v = LIVE_CTRL1_XL; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL1_XL, 1, &v, 1, 100);
  v = LIVE_CTRL8_XL; (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL8_XL, 1, &v, 1, 100);
  v = LIVE_CTRL2_G;  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL2_G,  1, &v, 1, 100);
  fsm_settle_until_tick = HAL_GetTick() + ACCEL_SETTLE_MS; /* restoring 104Hz also resets LPF2 — blank trigger through the settle */

  m = &cap_missions[cap_active_idx];

  /* Every mission (idle snapshot AND MOVING) carries the environment at seal time
   * so Grafana can chart temp/pressure for both — the live telemetry stream is off
   * during IDLE (ADR-021 Phase 1) and absent for the high-rate MOVING capture. */
  m->temp_c_x100 = (cached_temp_c > -998.0f) ? (int16_t)(cached_temp_c * 100.0f)
                                             : (int16_t)0x7FFF;
  m->pressure_hpa_x10 = (cached_pressure_hpa > 0.0f)
                        ? (uint16_t)(cached_pressure_hpa * 10.0f) : 0U;

  m->sealed = 1U;
  sprintf(uart_buf, "[CAPTURE] mission %u sealed: %lu words, %lu B, ovr=%lu\r\n",
          (unsigned)m->mission_id, (unsigned long)m->word_count,
          (unsigned long)m->byte_count, (unsigned long)m->overrun_evts);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  cap_active_idx = -1;  /* mission is sealed; Drain_AllPending picks it up on the next wake */
}

/* Running zlib/IEEE CRC-32 register (reflected, poly 0xEDB88320, no init/final).
 * Lets the whole-mission CRC accumulate across chunks without a second pass. */
static uint32_t Drain_CRC32_Update(uint32_t crc, const uint8_t *data, uint32_t len)
{
  uint32_t i; uint8_t b;
  for (i = 0U; i < len; i++)
  {
    crc ^= data[i];
    for (b = 0U; b < 8U; b++)
    {
      crc = (crc & 1U) ? ((crc >> 1) ^ 0xEDB88320UL) : (crc >> 1);
    }
  }
  return crc;
}

/* One-shot zlib.crc32-compatible CRC over a buffer; matches the gateway's check. */
static uint32_t Drain_CRC32(const uint8_t *data, uint32_t len)
{
  return Drain_CRC32_Update(0xFFFFFFFFUL, data, len) ^ 0xFFFFFFFFUL;
}

/* Bind the shared UDP socket to a fixed local port so the gateway's DrainAck
 * (the BEGIN liveness echo, sent back to our packets' source address) is
 * delivered to recvfrom() in Drain_WaitForAck. The EMW3080 only routes inbound
 * UDP to a bound socket — same reason the beacon listener binds BEACON_PORT — so
 * an unbound socket silently drops the ack and every drain skips its blast.
 * Best-effort: a bind failure must not break sendto (ack just won't arrive), so
 * the socket is left open. Re-run after every socket (re)create. */
static void Drain_BindLocalPort(void)
{
  struct mx_sockaddr_in laddr = {0};

  if (socket_id < 0) { return; }

  laddr.sin_len         = sizeof(laddr);
  laddr.sin_family      = MX_AF_INET;
  laddr.sin_port        = PLUDOS_HTONS(DRAIN_PORT); /* local port; gateway replies to this source */
  laddr.sin_addr.s_addr = 0U;                       /* INADDR_ANY */

  if (MX_WIFI_Socket_bind(wifi_obj, socket_id, (struct mx_sockaddr *)&laddr, sizeof(laddr)) != MX_WIFI_STATUS_OK)
  {
    sprintf(uart_buf, "[DRAIN] WARNING: local bind on port %u failed — ack RX disabled\r\n",
            (unsigned)DRAIN_PORT);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  }
}

/* Bounded wait for the gateway's DRAIN_BEGIN liveness echo (DrainAck, type 6).
 * sendto() returning OK only proves the packet left the radio, not that the Jetson
 * received it; this echo is the delivery evidence. recvfrom() runs on the send socket
 * itself (ephemeral local port), so the gateway's reply to the BEGIN's source address
 * lands here. SO_RCVTIMEO bounds each attempt. Returns 1 on a matching
 * (shuttle_id, mission_id) ack, 0 if none arrives within DRAIN_ACK_ATTEMPTS. */
static uint8_t Drain_WaitForAck(uint16_t mid)
{
  struct mx_sockaddr_in from = {0};
  uint32_t   fromlen;
  DrainAck_t ack;                       /* stack-local: drain_buf is a shared global, must not reuse it here */
  int32_t    timeout_ms = DRAIN_ACK_WAIT_MS;
  int32_t    n;
  uint8_t    attempt;

  (void)MX_WIFI_Socket_setsockopt(wifi_obj, socket_id, MX_SOL_SOCKET, MX_SO_RCVTIMEO,
                                  &timeout_ms, sizeof(timeout_ms));

  for (attempt = 0U; attempt < DRAIN_ACK_ATTEMPTS; attempt++)
  {
    fromlen = sizeof(from);
    n = MX_WIFI_Socket_recvfrom(wifi_obj, socket_id, (uint8_t *)&ack, (int32_t)sizeof(ack),
                                0, (struct mx_sockaddr *)&from, &fromlen);
    if ((n == (int32_t)sizeof(ack)) && (ack.magic == DRAIN_MAGIC) &&
        (ack.type == DRAIN_TYPE_ACK) && (ack.shuttle_id == (uint8_t)SHUTTLE_ID) &&
        (ack.mission_id == mid))
    {
      return 1U;
    }
    /* A stray/unmatched datagram costs one attempt; loop until the budget is spent. */
  }
  return 0U;
}

/* Blast a sealed PSRAM mission to the gateway on UDP 5684 (ADR-020/021, Phase 1).
 * Sends BEGIN ×3, all CHUNKs once (raw 7-byte FIFO words, CRC per chunk), END ×3.
 * No back-channel yet — the gateway reassembles best-effort and flags gaps; the
 * NAK/ACK selective-repeat layer (sampling_strategy.md §9) builds on top later. */
static void Drain_Mission(int16_t idx)
{
  CaptureMission_t *m;
  volatile uint8_t *ring;
  struct mx_sockaddr_in dest = {0};
  DrainBegin_t  beg = {0};
  DrainEnd_t    end = {0};
  DrainChunkHdr_t *hdr;
  uint32_t total_chunks, chunk, off, remaining, crc_all = 0xFFFFFFFFUL;
  uint16_t payload_len, k;

  if ((idx < 0) || (idx >= (int16_t)CAP_MAX_MISSIONS)) { return; }
  m = &cap_missions[idx];
  if ((m->sealed == 0U) || (m->byte_count == 0U)) { return; }

  if ((socket_id < 0) || (wifi_station_ready == 0U) || (jetson_ip[0] == 0))
  {
    sprintf(uart_buf, "[DRAIN] skipped: WiFi/socket not ready (mission %u)\r\n",
            (unsigned)m->mission_id);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    return;
  }

  ring = (volatile uint8_t *)PSRAM_BASE_ADDR;
  dest.sin_len         = sizeof(dest);
  dest.sin_family      = MX_AF_INET;
  dest.sin_port        = PLUDOS_HTONS(DRAIN_PORT);
  dest.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  total_chunks = (m->byte_count + DRAIN_CHUNK_PAYLOAD - 1U) / DRAIN_CHUNK_PAYLOAD;

  /* BEGIN — repeated so a single control-packet loss doesn't strand the mission. */
  beg.magic        = DRAIN_MAGIC;
  beg.type         = DRAIN_TYPE_BEGIN;
  beg.shuttle_id   = (uint8_t)SHUTTLE_ID;
  beg.mission_id   = m->mission_id;
  beg.total_chunks = (uint16_t)total_chunks;
  /* ODR depends on the capture mode so the gateway rebuilds each stream's timeline
   * correctly (idle snapshots are 12.5 Hz on both axes; MOVING is 3332/416). */
  beg.odr_accel_hz = (m->is_idle_snapshot != 0U) ? 12U : DRAIN_ODR_ACCEL_HZ;
  beg.odr_gyro_hz  = (m->is_idle_snapshot != 0U) ? 12U : DRAIN_ODR_GYRO_HZ;
  beg.temp_c_x100      = m->temp_c_x100;
  beg.pressure_hpa_x10 = m->pressure_hpa_x10;
  beg.is_idle_snapshot = m->is_idle_snapshot;
  beg.byte_count   = m->byte_count;
  beg.word_count   = m->word_count;
  beg.t0_tick_ms   = m->start_tick_ms;
  /* Transmit-time tick: tx_tick - t0_tick is the exact STM-measured age of this data
   * (capture start → drain, same boot/clock). Includes idle-exit wait + WiFi power-on,
   * so the gateway needs only: capture_wall = BEGIN_arrival - (tx_tick - t0_tick). */
  beg.tx_tick_ms   = HAL_GetTick();
  for (k = 0U; k < DRAIN_CTRL_REPEAT; k++)
  {
    (void)MX_WIFI_Socket_sendto(wifi_obj, socket_id, (uint8_t *)&beg, sizeof(beg),
                                0, (struct mx_sockaddr *)&dest, sizeof(dest));
  }

  /* Delivery evidence (gap 1): wait for the gateway's BEGIN echo before committing to
   * the multi-MB chunk blast. No echo ⇒ gateway unreachable or down — skip the blast to
   * keep the radio dark (ADR-021 intent) and leave drained=0 so this mission retries on
   * the next wake. The re-drain is idempotent: the gateway dedups on
   * (shuttle_id, mission_id, sample_index). cap_undrained_bytes / cap_wtm_hit are left
   * untouched so the watermark accounting still reflects the un-delivered data. */
  if (Drain_WaitForAck(m->mission_id) == 0U)
  {
    sprintf(uart_buf, "[DRAIN] mission %u: no gateway ack — skipping blast, retry next wake\r\n",
            (unsigned)m->mission_id);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    return;
  }

  /* CHUNKs — copy each window out of the PSRAM ring (byte-wise wrap), CRC it, send. */
  hdr = (DrainChunkHdr_t *)drain_buf;
  off = m->start_offset;
  for (chunk = 0U; chunk < total_chunks; chunk++)
  {
    IWDG_Kick();  /* large missions = many unyielded sends; keep the dog fed per chunk */
    remaining   = m->byte_count - (chunk * DRAIN_CHUNK_PAYLOAD);
    payload_len = (remaining > DRAIN_CHUNK_PAYLOAD) ? (uint16_t)DRAIN_CHUNK_PAYLOAD
                                                    : (uint16_t)remaining;
    for (k = 0U; k < payload_len; k++)
    {
      drain_buf[sizeof(DrainChunkHdr_t) + k] = ring[off];
      off++;
      if (off >= PSRAM_SIZE_BYTES) { off = 0U; } /* ring wrap */
    }

    hdr->magic        = DRAIN_MAGIC;
    hdr->type         = DRAIN_TYPE_CHUNK;
    hdr->shuttle_id   = (uint8_t)SHUTTLE_ID;
    hdr->mission_id   = m->mission_id;
    hdr->chunk_seq    = (uint16_t)chunk;
    hdr->total_chunks = (uint16_t)total_chunks;
    hdr->payload_len  = payload_len;
    hdr->crc32        = Drain_CRC32(&drain_buf[sizeof(DrainChunkHdr_t)], payload_len);
    crc_all = Drain_CRC32_Update(crc_all, &drain_buf[sizeof(DrainChunkHdr_t)], payload_len);

    (void)MX_WIFI_Socket_sendto(wifi_obj, socket_id, drain_buf,
                                (int32_t)(sizeof(DrainChunkHdr_t) + payload_len),
                                0, (struct mx_sockaddr *)&dest, sizeof(dest));

    /* Breather every N chunks: lets the MAC TX queue + gateway socket flush so a
     * sustained blast doesn't overrun either and drop a consecutive run of chunks. */
    if (((chunk + 1U) % DRAIN_CHUNK_PACE_EVERY) == 0U)
    {
      HAL_Delay(1);
    }
  }
  crc_all ^= 0xFFFFFFFFUL;

  /* END — carries the whole-mission CRC so the gateway can validate completeness. */
  end.magic        = DRAIN_MAGIC;
  end.type         = DRAIN_TYPE_END;
  end.shuttle_id   = (uint8_t)SHUTTLE_ID;
  end.mission_id   = m->mission_id;
  end.total_chunks = (uint16_t)total_chunks;
  end.crc32_all    = crc_all;
  for (k = 0U; k < DRAIN_CTRL_REPEAT; k++)
  {
    (void)MX_WIFI_Socket_sendto(wifi_obj, socket_id, (uint8_t *)&end, sizeof(end),
                                0, (struct mx_sockaddr *)&dest, sizeof(dest));
  }

  sprintf(uart_buf, "[DRAIN] mission %u sent: %lu chunks, %lu B, crc=%08lX\r\n",
          (unsigned)m->mission_id, (unsigned long)total_chunks,
          (unsigned long)m->byte_count, (unsigned long)crc_all);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

  /* Retire the mission from the un-drained accumulator; clear the watermark once
   * the ring drops back below 75% so the safety flush re-arms for the next fill. */
  m->drained = 1U;
  cap_undrained_bytes = (cap_undrained_bytes >= m->byte_count)
                        ? (cap_undrained_bytes - m->byte_count) : 0U;
  if (cap_undrained_bytes < CAP_RING_WTM_BYTES)
  {
    cap_wtm_hit = 0U;
  }
}

/* Wake the radio once to blast every sealed-but-undrained mission (the just-ended
 * MOVING mission plus any idle snapshots queued since the last wake), then power it
 * back down. Amortises the ~4 s WiFi bring-up across all pending data (ADR-021 §1). */
/* Sacrificial warm-up burst — fire DRAIN_WARMUP_PACKETS zero-magic datagrams
 * before any real mission. The radio reliably loses ~16 packets right after
 * WIFI_PowerOn while the AP learns our MAC / resolves ARP; this is packet-count
 * driven, NOT time driven — a passive settle delay does NOT help (verified on
 * hardware 2026-06-04, WIFI_LINK_SETTLE_MS=1200 still lost two idle snapshots).
 * Only actual outbound traffic warms the path, so we sacrifice junk packets into
 * that window. magic=0 (!= DRAIN_MAGIC) so the gateway drops them silently and
 * never reassembles or writes them. Sized to a real BEGIN for wire realism. */
static void Drain_WarmupBurst(void)
{
  struct mx_sockaddr_in dest = {0};
  uint8_t warm[sizeof(DrainBegin_t)] = {0};  /* all-zero payload → magic 0 → gateway discards */
  uint8_t k;

  if ((socket_id < 0) || (wifi_station_ready == 0U) || (jetson_ip[0] == 0)) { return; }

  dest.sin_len         = sizeof(dest);
  dest.sin_family      = MX_AF_INET;
  dest.sin_port        = PLUDOS_HTONS(DRAIN_PORT);
  dest.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  /* Pace the burst: an unspaced loop dumps all 24 packets into the EMW3080 SPI TX
   * queue in <5 ms, so only a handful actually hit the air before real data follows
   * and the BEGIN still lands inside the ARP/association loss window. Spacing each
   * junk packet by DRAIN_WARMUP_GAP_MS forces them onto the air over ~200 ms, which
   * reliably completes ARP/MAC-learning before the first real BEGIN. */
  for (k = 0U; k < DRAIN_WARMUP_PACKETS; k++)
  {
    (void)MX_WIFI_Socket_sendto(wifi_obj, socket_id, warm, (int32_t)sizeof(warm),
                                0, (struct mx_sockaddr *)&dest, sizeof(dest));
    WIFI_DelayWithYield(DRAIN_WARMUP_GAP_MS);
  }

  sprintf(uart_buf, "[DRAIN] warm-up burst sent: %u x %u B @ %u ms gap (sacrificial)\r\n",
          (unsigned)DRAIN_WARMUP_PACKETS, (unsigned)sizeof(warm), (unsigned)DRAIN_WARMUP_GAP_MS);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
}

/* Wait a pseudo-random 1.0–15.0 s before powering the radio so concurrent shuttles
 * don't contend the 2.4 GHz channel at the same instant. Seeded once from the 96-bit
 * device UID (unique per board) XOR HAL_GetTick, so each shuttle draws a different
 * xorshift32 sequence. The wait uses the IWDG-kicked delay — safe up to 15 s. */
static void Drain_JitterDelay(void)
{
  static uint32_t rng_state = 0U;

  if (rng_state == 0U)
  {
    const uint32_t *uid = (const uint32_t *)UID_BASE;  /* CMSIS: 96-bit unique device ID */
    rng_state = uid[0] ^ uid[1] ^ uid[2] ^ HAL_GetTick();
    if (rng_state == 0U) { rng_state = 0xA5A5A5A5U; }   /* xorshift can't start at 0 */
  }

  rng_state ^= rng_state << 13;
  rng_state ^= rng_state >> 17;
  rng_state ^= rng_state << 5;

  uint32_t steps   = (DRAIN_JITTER_MAX_MS - DRAIN_JITTER_MIN_MS) / DRAIN_JITTER_STEP_MS; /* 140 */
  uint32_t wait_ms = DRAIN_JITTER_MIN_MS + (rng_state % (steps + 1U)) * DRAIN_JITTER_STEP_MS;

  sprintf(uart_buf, "[DRAIN] pre-TX jitter %lu.%lu s\r\n",
          (unsigned long)(wait_ms / 1000U), (unsigned long)((wait_ms % 1000U) / 100U));
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

  WIFI_DelayWithYield(wait_ms);
}

static void Drain_AllPending(void)
{
  uint16_t i;
  uint8_t any = 0U;

  for (i = 0U; i < cap_mission_count; i++)
  {
    if ((cap_missions[i].sealed != 0U) && (cap_missions[i].drained == 0U) &&
        (cap_missions[i].byte_count != 0U))
    {
      any = 1U;
      break;
    }
  }
  if (any == 0U)
  {
    return; /* nothing pending — keep the radio dark */
  }

  Drain_JitterDelay();  /* decorrelate concurrent shuttles before the radio comes up */

  /* One bounded retry: a soft bring-up failure (transient connect / no DHCP) shouldn't
   * strand a whole batch of sealed missions until the next MOVING run. PowerOff fully
   * resets the module (RESET pin low) so the retry starts from a known-cold state. A
   * true silent-SPI hang never returns here — the IWDG handles that path. */
  int8_t powered = WIFI_PowerOn();
  if (powered != 0)
  {
    WIFI_PowerOff();
    WIFI_DelayWithYield(500U);
    powered = WIFI_PowerOn();
  }

  if (powered == 0)
  {
    uint8_t ip_refreshed = 0U;
    Drain_WarmupBurst();  /* absorb the post-power-on loss window before real data */
    for (i = 0U; i < cap_mission_count; i++)
    {
      if ((cap_missions[i].sealed != 0U) && (cap_missions[i].drained == 0U))
      {
        Drain_Mission((int16_t)i);
        /* Stale-IP self-heal (gap 2): a mission still undrained after Drain_Mission means
         * no gateway echo — the cached jetson_ip may be stale (gateway DHCP lease changed).
         * The radio is already up here, so refresh the IP once per wake via a quick beacon;
         * later missions in this batch use the new address, and the failed one retries next
         * wake. On a beacon miss BEACON_Run keeps the existing IP, so this is safe. */
        if ((cap_missions[i].drained == 0U) && (ip_refreshed == 0U))
        {
          (void)BEACON_Run(1U, BEACON_RETRY_TIMEOUT_MS);
          ip_refreshed = 1U;
        }
      }
    }
  }
  else
  {
    sprintf(uart_buf, "[DRAIN] skipped — WiFi power-on failed (after retry)\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  }
  WIFI_PowerOff();
}

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the System Power */
  SystemPower_Config();

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_ADF1_Init();
  MX_I2C1_Init();
  MX_I2C2_Init();
  MX_ICACHE_Init();
  MX_OCTOSPI1_Init();
  MX_OCTOSPI2_Init();
  MX_SPI2_Init();
  MX_UART4_Init();
  MX_USART1_UART_Init();
  MX_UCPD1_Init();
  MX_USB_OTG_FS_PCD_Init();
  /* USER CODE BEGIN 2 */

  // -----------------------------------------------------------------
  // BOOT RESET-CAUSE REPORT — diagnose the unexpected reboots that reset the
  // mission counter and drop any undrained PSRAM. Read the RCC reset flags once,
  // then clear them so the next boot reports its own cause. PINRST is checked last
  // because the internal reset pulse also asserts NRST on most reset sources.
  // -----------------------------------------------------------------
  {
    const char *cause;
    if      (__HAL_RCC_GET_FLAG(RCC_FLAG_IWDGRST)) { cause = "IWDG watchdog"; }
    else if (__HAL_RCC_GET_FLAG(RCC_FLAG_WWDGRST)) { cause = "WWDG watchdog"; }
    else if (__HAL_RCC_GET_FLAG(RCC_FLAG_BORRST))  { cause = "BOR brownout"; }
    else if (__HAL_RCC_GET_FLAG(RCC_FLAG_LPWRRST)) { cause = "low-power"; }
    else if (__HAL_RCC_GET_FLAG(RCC_FLAG_SFTRST))  { cause = "software"; }
    else if (__HAL_RCC_GET_FLAG(RCC_FLAG_PINRST))  { cause = "NRST pin"; }
    else                                           { cause = "unknown"; }
    sprintf(uart_buf, "[BOOT] reset cause: %s\r\n", cause);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    __HAL_RCC_CLEAR_RESET_FLAGS();
  }

  // -----------------------------------------------------------------
  // PSRAM BRING-UP (APS6408 on OCTOSPI1) — ADR-020 capture buffer
  // -----------------------------------------------------------------
  /* Finish device-side config (CubeMX only init the peripheral) and enter
     memory-mapped mode, then self-test before relying on the 8 MB region. */
  if (PSRAM_Init() == 0)
  {
    sprintf(uart_buf, "[PSRAM] APS6408 memory-mapped at 0x%08lX (%lu KB)\r\n",
            PSRAM_BASE_ADDR, PSRAM_SIZE_BYTES / 1024U);
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

    if (PSRAM_SelfTest() == 0)
    {
      sprintf(uart_buf, "[PSRAM] self-test PASS\r\n");
    }
    else
    {
      sprintf(uart_buf, "[PSRAM] ERROR: self-test FAILED\r\n");
    }
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }
  else
  {
    sprintf(uart_buf, "[PSRAM] ERROR: init failed\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }

  // -----------------------------------------------------------------
  // ACCELEROMETER INITIALIZATION (ISM330)
  // -----------------------------------------------------------------
  sprintf(uart_buf, "[SENSOR] Initializing ISM330 accelerometer...\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

  /* Anti-alias chain for the 50 Hz MOVING read (Nyquist 25 Hz):
     ODR=104 Hz (code 0100) with the on-chip LPF2 enabled and its cutoff set to
     ODR/10 ≈ 10.4 Hz (CTRL8_XL HPCF_XL=001, LP path). Cutoff < Nyquist, so the
     50 Hz stream is alias-free over the shuttle's low-frequency motion band
     (0–10 Hz). This resolves review item P1-A for the accelerometer. Capturing
     content above ~10 Hz would require raising ODR, the read rate, and the cutoff
     together (and re-measuring WiFi throughput). Register values per the ST
     ism330dhcx driver enums (ODR_XL=0100, FS_XL=00, LPF2_XL_EN bit1). */
  uint8_t accel_config = 0x42;  /* CTRL1_XL: ODR=104 Hz, FS=±2g, LPF2_XL_EN=1 */
  if (HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL1_XL, 1, &accel_config, 1, 100) == HAL_OK)
  {
    sprintf(uart_buf, "[SENSOR] ISM330 accelerometer enabled (104Hz, ±2g, LPF2 on)\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }
  else
  {
    sprintf(uart_buf, "[SENSOR] ERROR: Failed to initialize ISM330 accelerometer\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }

  /* CTRL8_XL: HPCF_XL=001 (LPF2 cutoff ODR/10), hp_slope_xl_en=0 → low-pass (not HP) path. */
  uint8_t accel_filter = 0x20;
  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL8_XL, 1, &accel_filter, 1, 100);

  HAL_Delay(100);  /* allow ISM330 to stabilize */

  /* Enable gyroscope: ODR matches accelerometer (104 Hz), ±250 dps FS. */
  uint8_t gyro_config = 0x40;  /* CTRL2_G: ODR=104 Hz, FS_G=00 → ±250 dps */
  if (HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL2_G, 1, &gyro_config, 1, 100) == HAL_OK)
  {
    sprintf(uart_buf, "[SENSOR] ISM330 gyroscope enabled (104Hz, ±250 dps)\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }
  else
  {
    sprintf(uart_buf, "[SENSOR] ERROR: Failed to initialize ISM330 gyroscope\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }

  /* Gyro anti-alias for the 50 Hz read (Nyquist 25 Hz). Two writes needed:
       CTRL4_C  LPF1_SEL_G=1  → route the gyro through digital LPF1 (else the
                               FTYPE setting is ignored and bandwidth stays 33 Hz).
       CTRL6_C  FTYPE=111     → narrowest LPF1; at ODR=104 Hz this gives 11.5 Hz
                               bandwidth (phase −64° @ 20 Hz). Source: ST AN5192
                               Table 14 (LSM6 family, identical gyro filter chain).
     11.5 Hz < 25 Hz Nyquist, matching the accel's ~10.4 Hz LPF2 → the gyro stream
     is now alias-free over the 0–10 Hz motion band. LPF1 is active only in the
     gyro's default high-performance mode (CTRL7_G left at default). The −64° phase
     lag is acceptable: gyro is the low-frequency motion-context channel, not used
     for real-time orientation fusion. */
  uint8_t gyro_lpf1_en = 0x02;  /* CTRL4_C: LPF1_SEL_G=1, other fields default 0 */
  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL4_C, 1, &gyro_lpf1_en, 1, 100);
  uint8_t gyro_filter = 0x07;   /* CTRL6_C: FTYPE=111, other fields default 0 */
  (void)HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL6_C, 1, &gyro_filter, 1, 100);

  /* ADR-020: arm the high-rate FIFO capture engine (FIFO bypassed until first MOVING). */
  if (Capture_Init() != 0)
  {
    sprintf(uart_buf, "[CAPTURE] ERROR: FIFO init failed\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  }

  // -----------------------------------------------------------------
  // HUMIDITY/TEMPERATURE INITIALIZATION (HTS221)
  // -----------------------------------------------------------------
  if (SENSOR_Humidity_Init(&hi2c2) == 0)
  {
    hts221_initialized = 1U;
    sprintf(uart_buf, "[SENSOR] HTS221 initialized (1 Hz, BDU, calib loaded)\r\n");
  }
  else
  {
    sprintf(uart_buf, "[SENSOR] WARNING: HTS221 not found on I2C2 — temp/humidity disabled\r\n");
  }
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

  // -----------------------------------------------------------------
  // PRESSURE INITIALIZATION (LPS22HH) — stamps idle snapshots (ADR-021 §1)
  // -----------------------------------------------------------------
  if (SENSOR_Pressure_Init(&hi2c2) == 0)
  {
    lps22hh_initialized = 1U;
    sprintf(uart_buf, "[SENSOR] LPS22HH initialized (pressure)\r\n");
  }
  else
  {
    sprintf(uart_buf, "[SENSOR] WARNING: LPS22HH not found on I2C2 — pressure disabled\r\n");
  }
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

  sprintf(uart_buf, "[NETWORK] WiFi init sequence starting...\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

  /* Bring the radio fully online (probe + reset + init + connect + DHCP + socket).
   * This is now the single on-demand power gate (WIFI_PowerOn); the ADR-020 drain
   * model toggles it off during MOVING and back on only to drain. */
  if (WIFI_PowerOn() != 0)
  {
    sprintf(uart_buf, "[NETWORK] WiFi bring-up failed — telemetry disabled this boot\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }

#if WIFI_POWERCYCLE_SELFTEST
  /* One-shot reversibility check: power the radio down to held-in-reset, then back
   * up, and confirm the socket re-arms. Validates the load-bearing ADR-020 drain
   * assumption that WiFi can be cycled. Remove (set define to 0) after verifying. */
  if (socket_id >= 0)
  {
    sprintf(uart_buf, "[SELFTEST] WiFi power-cycle: powering OFF...\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
    WIFI_PowerOff();
    HAL_Delay(1000);
    sprintf(uart_buf, "[SELFTEST] WiFi power-cycle: powering ON...\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
    sprintf(uart_buf, (WIFI_PowerOn() == 0)
            ? "[SELFTEST] WiFi power-cycle PASS (socket re-armed)\r\n"
            : "[SELFTEST] WiFi power-cycle FAIL (re-arm failed)\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }
#endif

#if BENCH_THROUGHPUT
  /* One-shot radio throughput sweep on a freshly armed socket, then normal telemetry resumes. */
  if (socket_id >= 0)
  {
    TELEMETRY_BenchThroughput();
  }
#endif

  /* ADR-021: enter the main loop with the radio dark. jetson_ip is cached from the
   * boot beacon (or fallback); WiFi powers on again only to drain at MOVING→IDLE. */
  if (wifi_driver_initialized != 0U)
  {
    WIFI_PowerOff();
  }

  /* Arm the watchdog only now — past the boot beacon (up to 30 s) and the one-shot
   * self-test/bench blocks, which would otherwise false-trip it. From here on, any
   * hang in the on-demand drain re-init (the observed field failure) stops the kicks
   * and the chip self-resets into a clean boot instead of freezing. */
  IWDG_Arm();
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */

    IWDG_Kick();  /* steady-state heartbeat; each loop iteration is well under the ~16 s period */

    /* Drive MXCHIP IO so async events (STA_UP / STA_DOWN / IP changes) are processed. */
    if ((wifi_obj != NULL) && (wifi_driver_initialized != 0U))
    {
      (void)MX_WIFI_IO_YIELD(wifi_obj, 1);
    }

    /* -----------------------------------------------------------------
     * PHASE 1: refresh environmental cache (2 Hz, off the hot path)
     * --------------------------------------------------------------- */
    {
      static uint32_t last_env_tick = 0U;
      if ((HAL_GetTick() - last_env_tick) >= ENV_READ_PERIOD_MS)
      {
        last_env_tick = HAL_GetTick();
        TELEMETRY_RefreshEnvCache();
      }
    }

    /* -----------------------------------------------------------------
     * PHASE 2: read accelerometer, update FSM with debounce
     * --------------------------------------------------------------- */
    {
      uint8_t raw[6] = {0};
      float a_mag_g2 = 0.0f;
      float deviation = 0.0f;

      if (HAL_I2C_Mem_Read(&hi2c2, ISM330_ADDR, OUTX_L_A, 1, raw, 6, 100) == HAL_OK)
      {
        int16_t raw_x = (int16_t)((raw[1] << 8) | raw[0]);
        int16_t raw_y = (int16_t)((raw[3] << 8) | raw[2]);
        int16_t raw_z = (int16_t)((raw[5] << 8) | raw[4]);

        vib_x = (raw_x * 0.061f) / 1000.0f;
        vib_y = (raw_y * 0.061f) / 1000.0f;
        vib_z = (raw_z * 0.061f) / 1000.0f;

        /* Gyro: same ISM330 chip, OUTX_L_G registers, identical 6-byte little-endian layout. */
        uint8_t raw_g[6] = {0};
        if (HAL_I2C_Mem_Read(&hi2c2, ISM330_ADDR, OUTX_L_G, 1, raw_g, 6, 100) == HAL_OK)
        {
          int16_t raw_gx = (int16_t)((raw_g[1] << 8) | raw_g[0]);
          int16_t raw_gy = (int16_t)((raw_g[3] << 8) | raw_g[2]);
          int16_t raw_gz = (int16_t)((raw_g[5] << 8) | raw_g[4]);
          gyro_x = (raw_gx * GYRO_SENS_MDPS_LSB) / 1000.0f;  /* mdps/LSB → dps */
          gyro_y = (raw_gy * GYRO_SENS_MDPS_LSB) / 1000.0f;
          gyro_z = (raw_gz * GYRO_SENS_MDPS_LSB) / 1000.0f;
          ism330_gyro_ok = 1U;
        }
        else
        {
          ism330_gyro_ok = 0U;
          sprintf(uart_buf, "[SENSOR] ERROR: I2C gyro read failed\r\n");
          HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
        }

        a_mag_g2 = (vib_x * vib_x) + (vib_y * vib_y) + (vib_z * vib_z);
        deviation = fabsf(a_mag_g2 - 1.0f);

        /* Magnitude-deviation detection: |a_mag² - 1g²|. Tilt-immune (gravity keeps
         * total magnitude at 1g for any mounting orientation, so static tilt reads ~0)
         * yet still catches travel in any axis — for flat-mount horizontal motion
         * deviation ≈ a_horiz². A raw X/Y trigger would false-fire on tilt because
         * gravity leaks onto X/Y, so it is deliberately not used. */
        /* Filter-settle guard: after any CTRL1_XL ODR change the LPF2 chain re-settles
         * and OUTX briefly reads ~0g (|mag²-1g²|≈1.0). Skip the whole trigger evaluation
         * during the window so that transient cannot phantom-complete a MOVING dwell, and
         * so it neither advances the dwell nor trips the MOVING→IDLE timeout. */
        if (HAL_GetTick() < fsm_settle_until_tick)
        {
          /* settling — leave FSM state untouched this cycle */
        }
        else
        {
        uint8_t moving_now = (deviation > MOVEMENT_THRESHOLD_G2);

        /* Above threshold: refresh timestamps, advance dwell if in IDLE. */
        if (moving_now)
        {
          last_above_threshold_tick = HAL_GetTick();
          last_movement_tick        = HAL_GetTick();

          if (current_state == STATE_IDLE)
          {
            if (continuous_movement_start_tick == 0U)
            {
              continuous_movement_start_tick = HAL_GetTick();
              sprintf(uart_buf, "[FSM] Dwell start (IDLE) dev=%.3f\r\n", deviation);
              HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
            }
            else if ((HAL_GetTick() - continuous_movement_start_tick) >= MOVEMENT_DWELL_MS)
            {
              current_state                  = STATE_MOVING;
              continuous_movement_start_tick = 0U;
              sprintf(uart_buf, "[FSM] IDLE -> MOVING  (dwell %ums reached)\r\n",
                      (unsigned)MOVEMENT_DWELL_MS);
              HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
              /* A real mission interrupted an idle snapshot: seal the partial snapshot
               * (stamped + queued for the next drain) before starting MOVING capture. */
              if (cap_snapshot_active != 0U)
              {
                Capture_EnterIdle();
                cap_snapshot_active    = 0U;
                cap_last_snapshot_tick = HAL_GetTick();
              }
              Capture_EnterMoving(); /* start high-rate FIFO capture for this mission */
            }
          }
        }
        else
        {
          /* Below threshold: only reset the dwell after MOVEMENT_DEBOUNCE_MS of quiet.
           * A single noisy sample does not erase progress toward MOVING. */
          if ((continuous_movement_start_tick != 0U) &&
              ((HAL_GetTick() - last_above_threshold_tick) > MOVEMENT_DEBOUNCE_MS))
          {
            continuous_movement_start_tick = 0U;
          }

          /* MOVING -> IDLE when no above-threshold sample for NO_MOVEMENT_TIMEOUT_MS. */
          if (current_state == STATE_MOVING)
          {
            if ((HAL_GetTick() - last_movement_tick) > NO_MOVEMENT_TIMEOUT_MS)
            {
              current_state = STATE_IDLE;
              sprintf(uart_buf, "[FSM] MOVING -> IDLE  (no movement %us)\r\n",
                      (unsigned)(NO_MOVEMENT_TIMEOUT_MS / 1000U));
              HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
              Capture_EnterIdle(); /* drain + seal the mission, restore live config */
              /* ADR-021: wake the radio once to drain this mission plus any idle
               * snapshots queued since the last wake, then power it back down. The
               * ~4 s power-on cost is paid in IDLE where latency is free; jetson_ip
               * is cached so the beacon wait is skipped. */
              Drain_AllPending();
            }
          }
        }
        } /* end filter-settle guard else */
      }
      else
      {
        vib_x = 99.0f; vib_y = 99.0f; vib_z = 99.0f; /* sentinel: 99g > ±2g FS, gateway converts to NaN */
        ism330_gyro_ok = 0U;
        sprintf(uart_buf, "[SENSOR] ERROR: I2C accel read failed\r\n");
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      }
      (void)a_mag_g2; /* suppress unused-warning when FSM branches don't read it directly */
    }

    /* -----------------------------------------------------------------
     * PHASE 2b: drain the high-rate ISM330 FIFO into PSRAM while MOVING
     * --------------------------------------------------------------- */
    if (current_state == STATE_MOVING)
    {
      cap_words_window += Capture_Service();
    }

    /* -----------------------------------------------------------------
     * PHASE 2c: low-rate IDLE snapshot (ADR-021 §1) — 10 s every 10 min,
     * accel+gyro at 12.5 Hz, stamped with temp/pressure at seal, drained on
     * the next MOVING→IDLE WiFi wake. Skipped entirely while MOVING.
     * --------------------------------------------------------------- */
    if (current_state == STATE_IDLE)
    {
      if (cap_snapshot_active == 0U)
      {
        if ((HAL_GetTick() - cap_last_snapshot_tick) >= CAP_IDLE_SNAP_PERIOD_MS)
        {
          Capture_EnterIdleSnapshot();
          cap_snapshot_active     = 1U;
          cap_snapshot_start_tick = HAL_GetTick();
        }
      }
      else
      {
        cap_words_window += Capture_Service();
        if ((HAL_GetTick() - cap_snapshot_start_tick) >= CAP_IDLE_SNAP_DUR_MS)
        {
          Capture_EnterIdle();          /* stamps env + seals; data waits for next drain */
          cap_snapshot_active    = 0U;
          cap_last_snapshot_tick = HAL_GetTick();
        }
      }
    }

    /* -----------------------------------------------------------------
     * PHASE 2d: ADR-021 safety flush — total un-drained ring crossed 75%.
     * Force a drain without a mission boundary (overnight idle-park guard).
     * --------------------------------------------------------------- */
    {
      /* Retry back-off (gap 3): cap_wtm_hit is only cleared by a successful drain, so a
       * gateway-down night would otherwise re-fire this every loop iteration — jitter +
       * 2× WIFI_PowerOn continuously, radio at max duty (opposite of ADR-021's intent).
       * After a failed safety-flush drain, hold off CAP_WTM_COOLDOWN_MS before retrying.
       * Signed tick diff is wrap-safe over the 10 min window. */
      static uint32_t cap_wtm_retry_after_tick = 0U;
      if ((cap_wtm_hit != 0U) && (current_state == STATE_IDLE) &&
          ((int32_t)(HAL_GetTick() - cap_wtm_retry_after_tick) >= 0))
      {
        if (cap_snapshot_active != 0U)
        {
          Capture_EnterIdle();          /* seal the in-flight snapshot first */
          cap_snapshot_active    = 0U;
          cap_last_snapshot_tick = HAL_GetTick();
        }
        sprintf(uart_buf, "[DRAIN] safety flush — undrained %luB >= 75%%\r\n",
                (unsigned long)cap_undrained_bytes);
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
        Drain_AllPending();
        /* Still hot ⇒ the drain failed to relieve pressure (gateway down). Back off. */
        if (cap_wtm_hit != 0U)
        {
          cap_wtm_retry_after_tick = HAL_GetTick() + CAP_WTM_COOLDOWN_MS;
        }
      }
    }

    /* -----------------------------------------------------------------
     * PHASE 3: WiFi reconnect handling (non-blocking)
     * --------------------------------------------------------------- */
    {
      static uint8_t reconnect_issued = 0U;

      if ((wifi_station_ready == 0U) && (wifi_driver_initialized != 0U))
      {
        if (!reconnect_issued)
        {
          sprintf(uart_buf, "[NETWORK] STA_DOWN — reconnecting to '%s'\r\n", WIFI_SSID);
          HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
          MX_WIFI_Connect(wifi_obj, WIFI_SSID, WIFI_PASSWORD, MX_WIFI_SEC_AUTO);
          reconnect_issued = 1U;
        }
      }
      else if (reconnect_issued)
      {
        reconnect_issued = 0U;
        sprintf(uart_buf, "[NETWORK] Reconnected — recreating UDP socket\r\n");
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

        /* MXCHIP invalidates all sockets on WiFi drop; recreate so telemetry resumes. */
        if (socket_id >= 0)
        {
          (void)MX_WIFI_Socket_close(wifi_obj, socket_id);
          socket_id = -1;
        }
        socket_id = MX_WIFI_Socket_create(wifi_obj, MX_AF_INET, MX_SOCK_DGRAM, MX_IPPROTO_UDP);
        sprintf(uart_buf, socket_id >= 0
                ? "[NETWORK] Socket recreated (ID: %ld)\r\n"
                : "[NETWORK] WARNING: socket recreate failed (%ld)\r\n", (long)socket_id);
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

        /* Re-bind the local port on the fresh socket so DrainAck RX keeps working. */
        Drain_BindLocalPort();

        /* Network may have changed (different AP, new DHCP lease). Do a short beacon
         * probe so the loop is back in business in ≤BEACON_RETRY_TIMEOUT_MS ms — the FSM
         * cannot tolerate a 30 s pause here (last_movement_tick would go stale and trigger
         * a spurious MOVING→IDLE on resume). The previous jetson_ip is kept; if the
         * network actually changed, the IDLE-only PHASE 3b periodic retry reconverges
         * within BEACON_RETRY_PERIOD_MS. */
        (void)BEACON_Run(1U, BEACON_RETRY_TIMEOUT_MS);
      }
    }

    /* -----------------------------------------------------------------
     * PHASE 3b: periodic beacon re-check (IDLE only, every BEACON_RETRY_PERIOD_MS)
     * Picks up a late-starting Jetson or an IP change without requiring a reflash.
     * Skipped during MOVING to avoid a 500 ms pause in the 50 Hz stream.
     * On success: jetson_ip updated immediately. On miss: existing IP kept as-is.
     * --------------------------------------------------------------- */
    /* Gated on wifi_driver_initialized: in the ADR-021 duty-cycle the radio is off
     * through IDLE, so beacon retry only runs in the brief drain window. */
    if ((current_state == STATE_IDLE) && (wifi_driver_initialized != 0U))
    {
      static uint32_t last_beacon_retry_tick = 0U;
      if ((HAL_GetTick() - last_beacon_retry_tick) >= BEACON_RETRY_PERIOD_MS)
      {
        last_beacon_retry_tick = HAL_GetTick();
        (void)BEACON_Run(1U, BEACON_RETRY_TIMEOUT_MS);
      }
    }

    /* -----------------------------------------------------------------
     * PHASE 4: transmit telemetry at the state-appropriate rate
     *   MOVING: every loop iteration  (50 Hz target, WiFi-capped)
     *   IDLE:   every TX_PERIOD_IDLE_MS (0.1 Hz)
     * --------------------------------------------------------------- */
    {
      uint8_t should_tx = 0U;
      if (current_state == STATE_MOVING)
      {
        should_tx = 1U;
      }
      else if ((HAL_GetTick() - last_tx_tick) >= TX_PERIOD_IDLE_MS)
      {
        should_tx = 1U;
      }

      /* ADR-021: the live 5683 stream is gone in the duty-cycle model — the radio is
       * off except to drain, so TELEMETRY_Send only fires inside a drain window. */
      if (should_tx && (wifi_driver_initialized != 0U))
      {
        (void)TELEMETRY_Send();
        last_tx_tick = HAL_GetTick();
      }
    }

    /* -----------------------------------------------------------------
     * PHASE 5: per-second status log — only while actually capturing
     * (a MOVING run or an active idle snapshot store data). Plain IDLE
     * polling stores nothing, so it gets a sparse 30 s heartbeat instead
     * of 1 Hz terminal noise, which also cuts needless UART traffic.
     * --------------------------------------------------------------- */
    if ((HAL_GetTick() - tx_window_start_tick) >= 1000U)
    {
      static uint32_t idle_hb_count = 0U;        /* seconds since last IDLE heartbeat */
      uint8_t capturing = (current_state == STATE_MOVING) || (cap_snapshot_active != 0U);

      if (capturing)
      {
        idle_hb_count = 0U;
        sprintf(uart_buf,
                "[STREAM] %s tx=%lu/s (live decimated view) accel=(%.2f,%.2f,%.2f)g gyro=(%.1f,%.1f,%.1f)dps temp=%.1fC hum=%.0f%%\r\n",
                (current_state == STATE_MOVING) ? "MOVING" : "SNAPSHOT",
                (unsigned long)tx_count_window,
                vib_x, vib_y, vib_z,
                gyro_x, gyro_y, gyro_z,
                cached_temp_c, cached_humidity_pct);
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      }
      /* IDLE: terse liveness line every 30 s so the terminal isn't dead between snapshots. */
      else if (++idle_hb_count >= 30U)
      {
        idle_hb_count = 0U;
        sprintf(uart_buf,
                "[IDLE] alive accel=(%.2f,%.2f,%.2f)g temp=%.1fC hum=%.0f%%\r\n",
                vib_x, vib_y, vib_z, cached_temp_c, cached_humidity_pct);
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      }

      /* Capture throughput while MOVING (expect ~3749 words/s = 3332 accel + 416 gyro). */
      if ((current_state == STATE_MOVING) || (cap_words_window > 0U))
      {
        CaptureMission_t *cm = (cap_active_idx >= 0) ? &cap_missions[cap_active_idx] : NULL;
        sprintf(uart_buf, "[CAPTURE] %lu words/s ring=%luKB ovr=%lu wtm=%u\r\n",
                (unsigned long)cap_words_window,
                (unsigned long)((cm != NULL) ? (cm->byte_count / 1024U) : 0U),
                (unsigned long)((cm != NULL) ? cm->overrun_evts : 0U),
                (unsigned)cap_wtm_hit);
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      }
      cap_words_window = 0U;

      tx_count_window      = 0U;
      tx_window_start_tick = HAL_GetTick();
    }

    /* -----------------------------------------------------------------
     * PHASE 6: state-appropriate loop delay
     * --------------------------------------------------------------- */
    WIFI_DelayWithYield(current_state == STATE_MOVING
                        ? SAMPLE_PERIOD_MOVING_MS
                        : SAMPLE_PERIOD_IDLE_MS);
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  if (HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE1) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI48|RCC_OSCILLATORTYPE_HSI
                              |RCC_OSCILLATORTYPE_MSI;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.HSI48State = RCC_HSI48_ON;
  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.MSIState = RCC_MSI_ON;
  RCC_OscInitStruct.MSICalibrationValue = RCC_MSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.MSIClockRange = RCC_MSIRANGE_4;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_MSI;
  RCC_OscInitStruct.PLL.PLLMBOOST = RCC_PLLMBOOST_DIV1;
  RCC_OscInitStruct.PLL.PLLM = 1;
  RCC_OscInitStruct.PLL.PLLN = 80;
  RCC_OscInitStruct.PLL.PLLP = 2;
  RCC_OscInitStruct.PLL.PLLQ = 2;
  RCC_OscInitStruct.PLL.PLLR = 2;
  RCC_OscInitStruct.PLL.PLLRGE = RCC_PLLVCIRANGE_0;
  RCC_OscInitStruct.PLL.PLLFRACN = 0;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2
                              |RCC_CLOCKTYPE_PCLK3;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB3CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_4) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief Power Configuration
  * @retval None
  */
static void SystemPower_Config(void)
{
  HAL_PWREx_EnableVddIO2();

  /*
   * Switch to SMPS regulator instead of LDO
   */
  if (HAL_PWREx_ConfigSupply(PWR_SMPS_SUPPLY) != HAL_OK)
  {
    Error_Handler();
  }
/* USER CODE BEGIN PWR */
/* USER CODE END PWR */
}

/**
  * @brief ADF1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_ADF1_Init(void)
{

  /* USER CODE BEGIN ADF1_Init 0 */

  /* USER CODE END ADF1_Init 0 */

  /* USER CODE BEGIN ADF1_Init 1 */

  /* USER CODE END ADF1_Init 1 */

  /**
    AdfHandle0 structure initialization and HAL_MDF_Init function call
  */
  AdfHandle0.Instance = ADF1_Filter0;
  AdfHandle0.Init.CommonParam.ProcClockDivider = 1;
  AdfHandle0.Init.CommonParam.OutputClock.Activation = DISABLE;
  AdfHandle0.Init.SerialInterface.Activation = ENABLE;
  AdfHandle0.Init.SerialInterface.Mode = MDF_SITF_LF_MASTER_SPI_MODE;
  AdfHandle0.Init.SerialInterface.ClockSource = MDF_SITF_CCK0_SOURCE;
  AdfHandle0.Init.SerialInterface.Threshold = 4;
  AdfHandle0.Init.FilterBistream = MDF_BITSTREAM0_FALLING;
  if (HAL_MDF_Init(&AdfHandle0) != HAL_OK)
  {
    Error_Handler();
  }

  /**
    AdfFilterConfig0 structure initialization

    WARNING : only structure is filled, no specific init function call for filter
  */
  AdfFilterConfig0.DataSource = MDF_DATA_SOURCE_BSMX;
  AdfFilterConfig0.Delay = 0;
  AdfFilterConfig0.CicMode = MDF_ONE_FILTER_SINC4;
  AdfFilterConfig0.DecimationRatio = 2;
  AdfFilterConfig0.Gain = 0;
  AdfFilterConfig0.ReshapeFilter.Activation = DISABLE;
  AdfFilterConfig0.HighPassFilter.Activation = DISABLE;
  AdfFilterConfig0.SoundActivity.Activation = DISABLE;
  AdfFilterConfig0.AcquisitionMode = MDF_MODE_ASYNC_CONT;
  AdfFilterConfig0.FifoThreshold = MDF_FIFO_THRESHOLD_NOT_EMPTY;
  AdfFilterConfig0.DiscardSamples = 0;
  /* USER CODE BEGIN ADF1_Init 2 */

  /* USER CODE END ADF1_Init 2 */

}

/**
  * @brief I2C1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_I2C1_Init(void)
{

  /* USER CODE BEGIN I2C1_Init 0 */

  /* USER CODE END I2C1_Init 0 */

  /* USER CODE BEGIN I2C1_Init 1 */

  /* USER CODE END I2C1_Init 1 */
  hi2c1.Instance = I2C1;
  hi2c1.Init.Timing = 0x30909DEC;
  hi2c1.Init.OwnAddress1 = 0;
  hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  hi2c1.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  hi2c1.Init.OwnAddress2 = 0;
  hi2c1.Init.OwnAddress2Masks = I2C_OA2_NOMASK;
  hi2c1.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  hi2c1.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&hi2c1) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure Analogue filter
  */
  if (HAL_I2CEx_ConfigAnalogFilter(&hi2c1, I2C_ANALOGFILTER_ENABLE) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure Digital filter
  */
  if (HAL_I2CEx_ConfigDigitalFilter(&hi2c1, 0) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN I2C1_Init 2 */

  /* USER CODE END I2C1_Init 2 */

}

/**
  * @brief I2C2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_I2C2_Init(void)
{

  /* USER CODE BEGIN I2C2_Init 0 */

  /* USER CODE END I2C2_Init 0 */

  /* USER CODE BEGIN I2C2_Init 1 */

  /* USER CODE END I2C2_Init 1 */
  hi2c2.Instance = I2C2;
  hi2c2.Init.Timing = 0x00F07BFF;
  hi2c2.Init.OwnAddress1 = 0;
  hi2c2.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  hi2c2.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  hi2c2.Init.OwnAddress2 = 0;
  hi2c2.Init.OwnAddress2Masks = I2C_OA2_NOMASK;
  hi2c2.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  hi2c2.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&hi2c2) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure Analogue filter
  */
  if (HAL_I2CEx_ConfigAnalogFilter(&hi2c2, I2C_ANALOGFILTER_ENABLE) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure Digital filter
  */
  if (HAL_I2CEx_ConfigDigitalFilter(&hi2c2, 0) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN I2C2_Init 2 */

  /* USER CODE END I2C2_Init 2 */

}

/**
  * @brief ICACHE Initialization Function
  * @param None
  * @retval None
  */
static void MX_ICACHE_Init(void)
{

  /* USER CODE BEGIN ICACHE_Init 0 */

  /* USER CODE END ICACHE_Init 0 */

  /* USER CODE BEGIN ICACHE_Init 1 */

  /* USER CODE END ICACHE_Init 1 */

  /** Enable instruction cache in 1-way (direct mapped cache)
  */
  if (HAL_ICACHE_ConfigAssociativityMode(ICACHE_1WAY) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_ICACHE_Enable() != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN ICACHE_Init 2 */

  /* USER CODE END ICACHE_Init 2 */

}

/**
  * @brief OCTOSPI1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_OCTOSPI1_Init(void)
{

  /* USER CODE BEGIN OCTOSPI1_Init 0 */

  /* USER CODE END OCTOSPI1_Init 0 */

  OSPIM_CfgTypeDef sOspiManagerCfg = {0};
  HAL_OSPI_DLYB_CfgTypeDef HAL_OSPI_DLYB_Cfg_Struct = {0};

  /* USER CODE BEGIN OCTOSPI1_Init 1 */

  /* USER CODE END OCTOSPI1_Init 1 */
  /* OCTOSPI1 parameter configuration*/
  hospi1.Instance = OCTOSPI1;
  hospi1.Init.FifoThreshold = 1;
  hospi1.Init.DualQuad = HAL_OSPI_DUALQUAD_DISABLE;
  hospi1.Init.MemoryType = HAL_OSPI_MEMTYPE_APMEMORY;
  hospi1.Init.DeviceSize = 23;
  hospi1.Init.ChipSelectHighTime = 1;
  hospi1.Init.FreeRunningClock = HAL_OSPI_FREERUNCLK_DISABLE;
  hospi1.Init.ClockMode = HAL_OSPI_CLOCK_MODE_0;
  hospi1.Init.WrapSize = HAL_OSPI_WRAP_NOT_SUPPORTED;
  hospi1.Init.ClockPrescaler = 2;
  hospi1.Init.SampleShifting = HAL_OSPI_SAMPLE_SHIFTING_NONE;
  hospi1.Init.DelayHoldQuarterCycle = HAL_OSPI_DHQC_ENABLE;
  hospi1.Init.ChipSelectBoundary = 10;
  hospi1.Init.DelayBlockBypass = HAL_OSPI_DELAY_BLOCK_USED;
  hospi1.Init.MaxTran = 0;
  hospi1.Init.Refresh = 100;
  if (HAL_OSPI_Init(&hospi1) != HAL_OK)
  {
    Error_Handler();
  }
  sOspiManagerCfg.ClkPort = 1;
  sOspiManagerCfg.DQSPort = 1;
  sOspiManagerCfg.NCSPort = 1;
  sOspiManagerCfg.IOLowPort = HAL_OSPIM_IOPORT_1_LOW;
  sOspiManagerCfg.IOHighPort = HAL_OSPIM_IOPORT_1_HIGH;
  if (HAL_OSPIM_Config(&hospi1, &sOspiManagerCfg, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  HAL_OSPI_DLYB_Cfg_Struct.Units = 0;
  HAL_OSPI_DLYB_Cfg_Struct.PhaseSel = 0;
  if (HAL_OSPI_DLYB_SetConfig(&hospi1, &HAL_OSPI_DLYB_Cfg_Struct) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN OCTOSPI1_Init 2 */

  /* USER CODE END OCTOSPI1_Init 2 */

}

/**
  * @brief OCTOSPI2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_OCTOSPI2_Init(void)
{

  /* USER CODE BEGIN OCTOSPI2_Init 0 */

  /* USER CODE END OCTOSPI2_Init 0 */

  OSPIM_CfgTypeDef sOspiManagerCfg = {0};
  HAL_OSPI_DLYB_CfgTypeDef HAL_OSPI_DLYB_Cfg_Struct = {0};

  /* USER CODE BEGIN OCTOSPI2_Init 1 */

  /* USER CODE END OCTOSPI2_Init 1 */
  /* OCTOSPI2 parameter configuration*/
  hospi2.Instance = OCTOSPI2;
  hospi2.Init.FifoThreshold = 4;
  hospi2.Init.DualQuad = HAL_OSPI_DUALQUAD_DISABLE;
  hospi2.Init.MemoryType = HAL_OSPI_MEMTYPE_MACRONIX;
  hospi2.Init.DeviceSize = 26;
  hospi2.Init.ChipSelectHighTime = 2;
  hospi2.Init.FreeRunningClock = HAL_OSPI_FREERUNCLK_DISABLE;
  hospi2.Init.ClockMode = HAL_OSPI_CLOCK_MODE_0;
  hospi2.Init.WrapSize = HAL_OSPI_WRAP_NOT_SUPPORTED;
  hospi2.Init.ClockPrescaler = 4;
  hospi2.Init.SampleShifting = HAL_OSPI_SAMPLE_SHIFTING_NONE;
  hospi2.Init.DelayHoldQuarterCycle = HAL_OSPI_DHQC_ENABLE;
  hospi2.Init.ChipSelectBoundary = 0;
  hospi2.Init.DelayBlockBypass = HAL_OSPI_DELAY_BLOCK_USED;
  hospi2.Init.MaxTran = 0;
  hospi2.Init.Refresh = 0;
  if (HAL_OSPI_Init(&hospi2) != HAL_OK)
  {
    Error_Handler();
  }
  sOspiManagerCfg.ClkPort = 2;
  sOspiManagerCfg.DQSPort = 2;
  sOspiManagerCfg.NCSPort = 2;
  sOspiManagerCfg.IOLowPort = HAL_OSPIM_IOPORT_2_LOW;
  sOspiManagerCfg.IOHighPort = HAL_OSPIM_IOPORT_2_HIGH;
  if (HAL_OSPIM_Config(&hospi2, &sOspiManagerCfg, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    Error_Handler();
  }
  HAL_OSPI_DLYB_Cfg_Struct.Units = 0;
  HAL_OSPI_DLYB_Cfg_Struct.PhaseSel = 0;
  if (HAL_OSPI_DLYB_SetConfig(&hospi2, &HAL_OSPI_DLYB_Cfg_Struct) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN OCTOSPI2_Init 2 */

  /* USER CODE END OCTOSPI2_Init 2 */

}

/**
  * @brief SPI2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_SPI2_Init(void)
{

  /* USER CODE BEGIN SPI2_Init 0 */

  /* USER CODE END SPI2_Init 0 */

  SPI_AutonomousModeConfTypeDef HAL_SPI_AutonomousMode_Cfg_Struct = {0};

  /* USER CODE BEGIN SPI2_Init 1 */

  /* USER CODE END SPI2_Init 1 */
  /* SPI2 parameter configuration*/
  hspi2.Instance = SPI2;
  hspi2.Init.Mode = SPI_MODE_MASTER;
  hspi2.Init.Direction = SPI_DIRECTION_2LINES;
  hspi2.Init.DataSize = SPI_DATASIZE_8BIT;
  hspi2.Init.CLKPolarity = SPI_POLARITY_LOW;
  hspi2.Init.CLKPhase = SPI_PHASE_1EDGE;
  hspi2.Init.NSS = SPI_NSS_SOFT;
  hspi2.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_16;
  hspi2.Init.FirstBit = SPI_FIRSTBIT_MSB;
  hspi2.Init.TIMode = SPI_TIMODE_DISABLE;
  hspi2.Init.CRCCalculation = SPI_CRCCALCULATION_DISABLE;
  hspi2.Init.CRCPolynomial = 0x7;
  hspi2.Init.NSSPMode = SPI_NSS_PULSE_DISABLE;
  hspi2.Init.NSSPolarity = SPI_NSS_POLARITY_LOW;
  hspi2.Init.FifoThreshold = SPI_FIFO_THRESHOLD_01DATA;
  hspi2.Init.MasterSSIdleness = SPI_MASTER_SS_IDLENESS_00CYCLE;
  hspi2.Init.MasterInterDataIdleness = SPI_MASTER_INTERDATA_IDLENESS_00CYCLE;
  hspi2.Init.MasterReceiverAutoSusp = SPI_MASTER_RX_AUTOSUSP_DISABLE;
  hspi2.Init.MasterKeepIOState = SPI_MASTER_KEEP_IO_STATE_DISABLE;
  hspi2.Init.IOSwap = SPI_IO_SWAP_DISABLE;
  hspi2.Init.ReadyMasterManagement = SPI_RDY_MASTER_MANAGEMENT_INTERNALLY;
  hspi2.Init.ReadyPolarity = SPI_RDY_POLARITY_HIGH;
  if (HAL_SPI_Init(&hspi2) != HAL_OK)
  {
    Error_Handler();
  }
  HAL_SPI_AutonomousMode_Cfg_Struct.TriggerState = SPI_AUTO_MODE_DISABLE;
  HAL_SPI_AutonomousMode_Cfg_Struct.TriggerSelection = SPI_GRP1_GPDMA_CH0_TCF_TRG;
  HAL_SPI_AutonomousMode_Cfg_Struct.TriggerPolarity = SPI_TRIG_POLARITY_RISING;
  if (HAL_SPIEx_SetConfigAutonomousMode(&hspi2, &HAL_SPI_AutonomousMode_Cfg_Struct) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN SPI2_Init 2 */

  /* USER CODE END SPI2_Init 2 */

}

/**
  * @brief UART4 Initialization Function
  * @param None
  * @retval None
  */
static void MX_UART4_Init(void)
{

  /* USER CODE BEGIN UART4_Init 0 */

  /* USER CODE END UART4_Init 0 */

  /* USER CODE BEGIN UART4_Init 1 */

  /* USER CODE END UART4_Init 1 */
  huart4.Instance = UART4;
  huart4.Init.BaudRate = 115200;
  huart4.Init.WordLength = UART_WORDLENGTH_8B;
  huart4.Init.StopBits = UART_STOPBITS_1;
  huart4.Init.Parity = UART_PARITY_NONE;
  huart4.Init.Mode = UART_MODE_TX_RX;
  huart4.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart4.Init.OverSampling = UART_OVERSAMPLING_16;
  huart4.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
  huart4.Init.ClockPrescaler = UART_PRESCALER_DIV1;
  huart4.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;
  if (HAL_UART_Init(&huart4) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_SetTxFifoThreshold(&huart4, UART_TXFIFO_THRESHOLD_1_8) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_SetRxFifoThreshold(&huart4, UART_RXFIFO_THRESHOLD_1_8) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_DisableFifoMode(&huart4) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN UART4_Init 2 */

  /* USER CODE END UART4_Init 2 */

}

/**
  * @brief USART1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART1_UART_Init(void)
{

  /* USER CODE BEGIN USART1_Init 0 */

  /* USER CODE END USART1_Init 0 */

  /* USER CODE BEGIN USART1_Init 1 */

  /* USER CODE END USART1_Init 1 */
  huart1.Instance = USART1;
  huart1.Init.BaudRate = 115200;
  huart1.Init.WordLength = UART_WORDLENGTH_8B;
  huart1.Init.StopBits = UART_STOPBITS_1;
  huart1.Init.Parity = UART_PARITY_NONE;
  huart1.Init.Mode = UART_MODE_TX_RX;
  huart1.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart1.Init.OverSampling = UART_OVERSAMPLING_16;
  huart1.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
  huart1.Init.ClockPrescaler = UART_PRESCALER_DIV1;
  huart1.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;
  if (HAL_UART_Init(&huart1) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_SetTxFifoThreshold(&huart1, UART_TXFIFO_THRESHOLD_1_8) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_SetRxFifoThreshold(&huart1, UART_RXFIFO_THRESHOLD_1_8) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_UARTEx_DisableFifoMode(&huart1) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART1_Init 2 */

  /* USER CODE END USART1_Init 2 */

}

/**
  * @brief UCPD1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_UCPD1_Init(void)
{

  /* USER CODE BEGIN UCPD1_Init 0 */

  /* USER CODE END UCPD1_Init 0 */

  LL_GPIO_InitTypeDef GPIO_InitStruct = {0};

  /* Peripheral clock enable */
  LL_APB1_GRP2_EnableClock(LL_APB1_GRP2_PERIPH_UCPD1);

  LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOA);
  LL_AHB2_GRP1_EnableClock(LL_AHB2_GRP1_PERIPH_GPIOB);
  /**UCPD1 GPIO Configuration
  PA15 (JTDI)   ------> UCPD1_CC1
  PB15   ------> UCPD1_CC2
  */
  GPIO_InitStruct.Pin = LL_GPIO_PIN_15;
  GPIO_InitStruct.Mode = LL_GPIO_MODE_ANALOG;
  GPIO_InitStruct.Pull = LL_GPIO_PULL_NO;
  LL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  GPIO_InitStruct.Pin = LL_GPIO_PIN_15;
  GPIO_InitStruct.Mode = LL_GPIO_MODE_ANALOG;
  GPIO_InitStruct.Pull = LL_GPIO_PULL_NO;
  LL_GPIO_Init(GPIOB, &GPIO_InitStruct);

  /* USER CODE BEGIN UCPD1_Init 1 */

  /* USER CODE END UCPD1_Init 1 */
  /* USER CODE BEGIN UCPD1_Init 2 */

  /* USER CODE END UCPD1_Init 2 */

}

/**
  * @brief USB_OTG_FS Initialization Function
  * @param None
  * @retval None
  */
static void MX_USB_OTG_FS_PCD_Init(void)
{

  /* USER CODE BEGIN USB_OTG_FS_Init 0 */

  /* USER CODE END USB_OTG_FS_Init 0 */

  /* USER CODE BEGIN USB_OTG_FS_Init 1 */

  /* USER CODE END USB_OTG_FS_Init 1 */
  hpcd_USB_OTG_FS.Instance = USB_OTG_FS;
  hpcd_USB_OTG_FS.Init.dev_endpoints = 6;
  hpcd_USB_OTG_FS.Init.speed = PCD_SPEED_FULL;
  hpcd_USB_OTG_FS.Init.phy_itface = PCD_PHY_EMBEDDED;
  hpcd_USB_OTG_FS.Init.Sof_enable = DISABLE;
  hpcd_USB_OTG_FS.Init.low_power_enable = DISABLE;
  hpcd_USB_OTG_FS.Init.lpm_enable = DISABLE;
  hpcd_USB_OTG_FS.Init.battery_charging_enable = DISABLE;
  hpcd_USB_OTG_FS.Init.use_dedicated_ep1 = DISABLE;
  hpcd_USB_OTG_FS.Init.vbus_sensing_enable = DISABLE;
  hpcd_USB_OTG_FS.Init.dma_enable = DISABLE;
  if (HAL_PCD_Init(&hpcd_USB_OTG_FS) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USB_OTG_FS_Init 2 */

  /* USER CODE END USB_OTG_FS_Init 2 */

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOG_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOI_CLK_ENABLE();
  __HAL_RCC_GPIOH_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOD_CLK_ENABLE();
  __HAL_RCC_GPIOE_CLK_ENABLE();
  __HAL_RCC_GPIOF_CLK_ENABLE();

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(UCPD_PWR_GPIO_Port, UCPD_PWR_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(GPIOH, LED_RED_Pin|LED_GREEN_Pin|Mems_VL53_xshut_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(WRLS_WKUP_B_GPIO_Port, WRLS_WKUP_B_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(WRLS_NSS_GPIO_Port, WRLS_NSS_Pin, GPIO_PIN_SET);

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(GPIOF, Mems_STSAFE_RESET_Pin|WRLS_WKUP_W_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin : WRLS_FLOW_Pin */
  GPIO_InitStruct.Pin = WRLS_FLOW_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_IT_RISING;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(WRLS_FLOW_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pin : PH3_BOOT0_Pin */
  GPIO_InitStruct.Pin = PH3_BOOT0_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(PH3_BOOT0_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pin : UCPD_PWR_Pin */
  GPIO_InitStruct.Pin = UCPD_PWR_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(UCPD_PWR_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pin : USER_Button_Pin */
  GPIO_InitStruct.Pin = USER_Button_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(USER_Button_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pins : LED_RED_Pin LED_GREEN_Pin Mems_VL53_xshut_Pin */
  GPIO_InitStruct.Pin = LED_RED_Pin|LED_GREEN_Pin|Mems_VL53_xshut_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOH, &GPIO_InitStruct);

  /*Configure GPIO pin : MIC_CCK1_Pin */
  GPIO_InitStruct.Pin = MIC_CCK1_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  GPIO_InitStruct.Alternate = GPIO_AF6_MDF1;
  HAL_GPIO_Init(MIC_CCK1_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pin : WRLS_WKUP_B_Pin */
  GPIO_InitStruct.Pin = WRLS_WKUP_B_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(WRLS_WKUP_B_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pins : Mems_VLX_GPIO_Pin Mems_INT_LPS22HH_Pin */
  GPIO_InitStruct.Pin = Mems_VLX_GPIO_Pin|Mems_INT_LPS22HH_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOG, &GPIO_InitStruct);

  /*Configure GPIO pin : WRLS_NOTIFY_Pin */
  GPIO_InitStruct.Pin = WRLS_NOTIFY_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_IT_RISING;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(WRLS_NOTIFY_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pins : USB_UCPD_FLT_Pin Mems_ISM330DLC_INT1_Pin */
  GPIO_InitStruct.Pin = USB_UCPD_FLT_Pin|Mems_ISM330DLC_INT1_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOE, &GPIO_InitStruct);

  /*Configure GPIO pins : Mems_INT_IIS2MDC_Pin USB_IANA_Pin */
  GPIO_InitStruct.Pin = Mems_INT_IIS2MDC_Pin|USB_IANA_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOD, &GPIO_InitStruct);

  /*Configure GPIO pin : USB_VBUS_SENSE_Pin */
  GPIO_InitStruct.Pin = USB_VBUS_SENSE_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(USB_VBUS_SENSE_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pin : WRLS_NSS_Pin */
  GPIO_InitStruct.Pin = WRLS_NSS_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  HAL_GPIO_Init(WRLS_NSS_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pins : Mems_STSAFE_RESET_Pin WRLS_WKUP_W_Pin */
  GPIO_InitStruct.Pin = Mems_STSAFE_RESET_Pin|WRLS_WKUP_W_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOF, &GPIO_InitStruct);

  /*Configure GPIO pin : MIC_SDIN0_Pin */
  GPIO_InitStruct.Pin = MIC_SDIN0_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  GPIO_InitStruct.Alternate = GPIO_AF6_MDF1;
  HAL_GPIO_Init(MIC_SDIN0_GPIO_Port, &GPIO_InitStruct);

  /* EXTI interrupt init*/
  HAL_NVIC_SetPriority(EXTI14_IRQn, 5, 0);
  HAL_NVIC_EnableIRQ(EXTI14_IRQn);

  HAL_NVIC_SetPriority(EXTI15_IRQn, 5, 0);
  HAL_NVIC_EnableIRQ(EXTI15_IRQn);

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* EXTI for MXCHIP flow/notify pins (PD14 + PG15) */
  HAL_NVIC_SetPriority(EXTI15_IRQn, 5, 0);
  HAL_NVIC_EnableIRQ(EXTI15_IRQn);

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
