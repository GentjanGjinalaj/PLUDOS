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
#include <stdio.h>
#include <string.h>
#include <math.h>            /* fabsf() for FSM threshold check */

#include "mx_wifi.h"
#include "mx_wifi_io.h"
#define USE_BSP_I2C_SHUT_DOWN

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* 28-byte unified telemetry payload (ADR-015 v2 / wire_protocol.md §1).
 * pressure_hpa removed — LPS22HH absent on this board; power_mw removed — gateway
 * derives it from state using POWER_IDLE_MW / POWER_MOVING_MW env vars.
 * shuttle_id shrunk from char[12] to uint8_t: 1 = STM32-Alpha, 2 = STM32-Beta, …
 * Python unpack: struct.unpack('<BHIBfffff', data) */
#pragma pack(push, 1)
typedef struct {
  uint8_t  shuttle_id;      /* 1-based integer; gateway maps to name via SHUTTLE_NAMES */
  uint16_t sequence_id;     /* monotonic per-shuttle, wraps at 65535                    */
  uint32_t tick_ms;         /* HAL_GetTick() at sample time                             */
  uint8_t  state;           /* 0 = STATE_IDLE, 1 = STATE_MOVING                         */
  float    accel_x;         /* g, ISM330 X axis — raw signal; AC content = vibration    */
  float    accel_y;         /* g, ISM330 Y axis                                          */
  float    accel_z;         /* g, ISM330 Z axis                                          */
  float    temp_c;          /* HTS221 °C; -999.0 = sensor unavailable                   */
  float    humidity_pct;    /* HTS221 %RH 0–100; 0.0 if temp sentinel                   */
} __attribute__((packed)) PludosTelemetry_t;   /* total: 28 bytes                        */
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

#define MOVEMENT_THRESHOLD_G2   0.05f    /* deviation from 1g² — ~0.0247 g, above ISM330 noise floor */
#define MOVEMENT_DWELL_MS       500U     /* continuous-above duration to enter STATE_MOVING */
#define MOVEMENT_DEBOUNCE_MS    300U     /* sub-threshold tolerance inside a dwell — survives motion microbreaks */
#define NO_MOVEMENT_TIMEOUT_MS  20000U   /* no above-threshold sample for this long → STATE_IDLE */

#define SAMPLE_PERIOD_IDLE_MS   100U     /* 10 Hz internal sampling in IDLE (FSM responsiveness) */
#define SAMPLE_PERIOD_MOVING_MS 20U      /* 50 Hz sampling + transmit in MOVING */
#define TX_PERIOD_IDLE_MS       1000U    /* 1 Hz UDP transmit in IDLE — every 10th sample */
#define ENV_READ_PERIOD_MS      500U     /* 2 Hz HTS221 refresh; cached for every TX */

/* ISM330 I2C addr: SA0 tied to VDD on IOT02A → base 0x6B, left-shifted → 0xD6.
 * Confirmed in board schematic; datasheet default (SA0=0) gives 0x6A = 0xD4. */
#define ISM330_ADDR 0xD6
#define CTRL1_XL    0x10
#define OUTX_L_A    0x28

/* Global physics variables so the Live Watch can see them */
float vib_x = 0.0f;
float vib_y = 0.0f;
float vib_z = 0.0f;

static uint16_t current_packet_num = 1U;

// =========================================================================
// PLUDOS NETWORK CONFIGURATION (ADR-015)
// =========================================================================
/* WIFI_SSID, WIFI_PASSWORD, JETSON_IP, and SHUTTLE_ID are in
 * wifi_credentials.h (gitignored — copy from wifi_credentials.h.example). */
#define TELEMETRY_PORT 5683U  /* single UDP port for the unified PludosTelemetry stream */

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
static uint8_t  hts221_initialized  = 0U;    /* SENSOR_Humidity_Init succeeded */
/* Environmental sensor cache (refreshed every ENV_READ_PERIOD_MS so the I²C bus
 * stays out of the 50 Hz hot path; cached values stamp every outgoing packet). */
static float    cached_temp_c       = -999.0f;
static float    cached_humidity_pct =    0.0f;

/* TX bookkeeping for periodic per-second status log. */
static uint32_t last_tx_tick      = 0U;
static uint32_t tx_count_window   = 0U;
static uint32_t tx_window_start_tick = 0U;

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
static void TELEMETRY_RefreshEnvCache(void);
static int32_t TELEMETRY_Send(void);

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

  pkt.shuttle_id    = SHUTTLE_ID;
  pkt.sequence_id   = current_packet_num;
  pkt.tick_ms       = HAL_GetTick();
  pkt.state         = (uint8_t)current_state;
  pkt.accel_x       = vib_x;
  pkt.accel_y       = vib_y;
  pkt.accel_z       = vib_z;
  pkt.temp_c        = cached_temp_c;
  pkt.humidity_pct  = cached_humidity_pct;

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
  // ACCELEROMETER INITIALIZATION (ISM330)
  // -----------------------------------------------------------------
  sprintf(uart_buf, "[SENSOR] Initializing ISM330 accelerometer...\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

  // Enable accelerometer: CTRL1_XL = 0x50 (416 Hz, ±2g range, normal mode)
  uint8_t accel_config = 0x50;  // 416Hz, ±2g, normal mode
  if (HAL_I2C_Mem_Write(&hi2c2, ISM330_ADDR, CTRL1_XL, 1, &accel_config, 1, 100) == HAL_OK)
  {
    sprintf(uart_buf, "[SENSOR] ISM330 accelerometer enabled (416Hz, ±2g)\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }
  else
  {
    sprintf(uart_buf, "[SENSOR] ERROR: Failed to initialize ISM330 accelerometer\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
  }

  HAL_Delay(100);  /* allow ISM330 to stabilize */

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

  sprintf(uart_buf, "[NETWORK] WiFi init sequence starting...\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

  WIFI_SPI_ApplySafeTiming();
  sprintf(uart_buf, "[NETWORK] SPI2 reconfigured for MXCHIP safe mode (~10 MHz)\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

  sprintf(uart_buf, "[NETWORK] Registering WiFi SPI bus...\r\n");
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

  void *wifi_ll_context = NULL;
  uint32_t probe_start = HAL_GetTick();
  int probe_result = mxwifi_probe(&wifi_ll_context);
  uint32_t probe_duration = HAL_GetTick() - probe_start;

  wifi_obj = (MX_WIFIObject_t *)wifi_ll_context;

  sprintf(uart_buf, "[NETWORK] WiFi SPI bus probe result: %d (took %lu ms)\r\n", probe_result, probe_duration);
  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

  if ((probe_result == 0) && (wifi_obj != NULL))
  {
    MX_WIFI_STATUS_T reset_status = MX_WIFI_STATUS_OK;

    if (wifi_obj->Runtime.interfaces == 0U)
    {
      uint32_t reset_start = HAL_GetTick();
      sprintf(uart_buf, "[NETWORK] Performing WiFi module hard reset...\r\n");
      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

      reset_status = MX_WIFI_HardResetModule(wifi_obj);

      sprintf(uart_buf, "[NETWORK] MX_WIFI_HardResetModule returned: 0x%02X after %lu ms\r\n",
              reset_status, HAL_GetTick() - reset_start);
      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
    }

    if (reset_status == MX_WIFI_STATUS_OK)
    {
      uint32_t init_start_time = HAL_GetTick();

      sprintf(uart_buf, "[NETWORK] Initializing MXCHIP (SPI Handshake)...\r\n");
      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

      MX_WIFI_STATUS_T init_status = MX_WIFI_Init(wifi_obj);
      uint32_t init_duration = HAL_GetTick() - init_start_time;

      sprintf(uart_buf, "[NETWORK] MX_WIFI_Init returned: 0x%02X after %lu ms\r\n", init_status, init_duration);
      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

      if (init_status == MX_WIFI_STATUS_OK)
      {
        MX_WIFI_STATUS_T callback_status;

        wifi_driver_initialized = 1U;
        wifi_obj->NetSettings.DHCP_IsEnabled = 1U;
        wifi_station_event = 0xFF;
        wifi_station_ready = 0U;

        callback_status = MX_WIFI_RegisterStatusCallback_if(wifi_obj, WIFI_StatusCallback, NULL, MC_STATION);
        sprintf(uart_buf, "[NETWORK] MX_WIFI_RegisterStatusCallback_if returned: 0x%02X\r\n", callback_status);
        HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

        sprintf(uart_buf, "[NETWORK] SPI link OK. Connecting to WiFi: '%s'\r\n", WIFI_SSID);
        HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

        uint32_t connect_start = HAL_GetTick();
        MX_WIFI_STATUS_T connect_status = MX_WIFI_Connect(wifi_obj, WIFI_SSID, WIFI_PASSWORD, MX_WIFI_SEC_AUTO);
        uint32_t connect_duration = HAL_GetTick() - connect_start;

        sprintf(uart_buf, "[NETWORK] MX_WIFI_Connect returned: 0x%02X after %lu ms\r\n",
                connect_status, connect_duration);
        HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

        if (connect_status == MX_WIFI_STATUS_OK)
        {
          uint8_t ip_addr[4] = {0};
          MX_WIFI_STATUS_T ip_wait_status = WIFI_WaitForStationIP(ip_addr, 15000U);

          sprintf(uart_buf, "[NETWORK] DHCP wait result: 0x%02X\r\n", ip_wait_status);
          HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

          if (ip_wait_status == MX_WIFI_STATUS_OK)
          {
            sprintf(uart_buf, "[NETWORK] SUCCESS! Station IP: %d.%d.%d.%d\r\n",
                    ip_addr[0], ip_addr[1], ip_addr[2], ip_addr[3]);
            HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

            /* Try beacon first; fall back to JETSON_IP if the Jetson is not yet reachable.
             * The main loop retries the beacon every BEACON_RETRY_PERIOD_MS (IDLE only),
             * so a late-starting Jetson is picked up automatically without a reflash. */
            if (BEACON_Run(BEACON_MAX_RETRIES, BEACON_TIMEOUT_MS) == 0U)
            {
              /* Bounded copy: JETSON_IP comes from wifi_credentials.h; if a misconfigured
               * value is longer than the 16-byte jetson_ip buffer, strncpy clamps safely. */
              strncpy(jetson_ip, JETSON_IP, sizeof(jetson_ip) - 1U);
              jetson_ip[sizeof(jetson_ip) - 1U] = 0;
              sprintf(uart_buf, "[BEACON] Timed out — fallback IP: %s\r\n", JETSON_IP);
              HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
            }

            socket_id = MX_WIFI_Socket_create(wifi_obj, MX_AF_INET, MX_SOCK_DGRAM, MX_IPPROTO_UDP);
            if (socket_id >= 0)
            {
              sprintf(uart_buf, "[NETWORK] UDP Socket created (ID: %ld)\r\n", (long)socket_id);
              HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
              sprintf(uart_buf, "[NETWORK] PludosTelemetry stream armed → udp://%s:%u\r\n",
                      jetson_ip, (unsigned)TELEMETRY_PORT);
            }
            else
            {
              sprintf(uart_buf, "[NETWORK] ERROR: Failed to create UDP socket\r\n");
              socket_id = -1;
            }
            HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
          }
          else
          {
            sprintf(uart_buf, "[NETWORK] ERROR: WiFi connected command accepted, but no station IP was assigned\r\n");
            HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
            socket_id = -1;
          }
        }
        else
        {
          sprintf(uart_buf, "[NETWORK] Connection failed - check SSID/password/security mode\r\n");
          HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
          socket_id = -1;
        }
      }
      else
      {
        wifi_driver_initialized = 0U;
        sprintf(uart_buf, "[NETWORK] Module init failed\r\n");
        HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
        socket_id = -1;
      }
    }
    else
    {
      wifi_driver_initialized = 0U;
      sprintf(uart_buf, "[NETWORK] Hard reset failed - check module power and firmware\r\n");
      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
      socket_id = -1;
    }
  }
  else
  {
    wifi_driver_initialized = 0U;
    sprintf(uart_buf, "[NETWORK] ERROR: WiFi SPI bus registration failed\r\n");
    HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
    socket_id = -1;
  }
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */

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

        a_mag_g2 = (vib_x * vib_x) + (vib_y * vib_y) + (vib_z * vib_z);
        deviation = fabsf(a_mag_g2 - 1.0f);

        /* Above threshold: refresh timestamps, advance dwell if in IDLE. */
        if (deviation > MOVEMENT_THRESHOLD_G2)
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
            }
          }
        }
      }
      else
      {
        vib_x = 99.0f; vib_y = 99.0f; vib_z = 99.0f; /* sentinel; gateway can detect */
        sprintf(uart_buf, "[SENSOR] ERROR: I2C accel read failed\r\n");
        HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      }
      (void)a_mag_g2; /* suppress unused-warning when FSM branches don't read it directly */
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
    if (current_state == STATE_IDLE)
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
     *   MOVING: every loop iteration  (50 Hz)
     *   IDLE:   every TX_PERIOD_IDLE_MS (1 Hz)
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

      if (should_tx)
      {
        (void)TELEMETRY_Send();
        last_tx_tick = HAL_GetTick();
      }
    }

    /* -----------------------------------------------------------------
     * PHASE 5: per-second status log so the terminal shows live activity
     * --------------------------------------------------------------- */
    if ((HAL_GetTick() - tx_window_start_tick) >= 1000U)
    {
      sprintf(uart_buf,
              "[STREAM] st=%u tx=%lu/s accel=(%.2f,%.2f,%.2f)g temp=%.1fC hum=%.0f%%\r\n",
              (unsigned)current_state, (unsigned long)tx_count_window,
              vib_x, vib_y, vib_z,
              cached_temp_c, cached_humidity_pct);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
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
  hi2c2.Init.Timing = 0x30909DEC;
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
  hspi2.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_2;
  hspi2.Init.FirstBit = SPI_FIRSTBIT_MSB;
  hspi2.Init.TIMode = SPI_TIMODE_DISABLE;
  hspi2.Init.CRCCalculation = SPI_CRCCALCULATION_DISABLE;
  hspi2.Init.CRCPolynomial = 0x7;
  hspi2.Init.NSSPMode = SPI_NSS_PULSE_ENABLE;
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
