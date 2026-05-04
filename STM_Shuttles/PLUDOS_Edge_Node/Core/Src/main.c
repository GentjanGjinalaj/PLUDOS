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
#include <string.h> // Required for the strlen() command

#include "mx_wifi.h"
#include "mx_wifi_io.h"
#define USE_BSP_I2C_SHUT_DOWN

/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
typedef struct
{
  uint16_t sequence_id;
  uint32_t tick_ms;    /* HAL_GetTick() at sample time */
  float accel_x;
  float accel_y;
  float accel_z;
  float power_mw;      /* board-level estimate: MCU + WiFi current × 3.3V (P2-2 for real ADC) */
} SensorSample_t;

/* 39-byte packed binary payload — matches data-engine.py struct '<12sHIBfffff'.
 * Field order and types MUST match wire_protocol.md §1 exactly. */
#pragma pack(push, 1)
typedef struct {
  char     shuttle_id[12];  /* null-padded ASCII, identifies the shuttle */
  uint16_t sequence_id;     /* monotonic counter; gateway uses it to sort packets */
  uint32_t tick_ms;         /* HAL_GetTick(); gateway converts to absolute via NTP offset */
  uint8_t  mission_active;  /* 1 = moving, 0 = mission ended (gateway writes Parquet on 0) */
  float    ram_usage_pct;   /* SRAM buffer fill % (0–100) */
  float    accel_x;
  float    accel_y;
  float    accel_z;
  float    power_mw;        /* board-level estimate: MCU + WiFi (P2-2 for real ADC) */
} CriticalPayload;          /* total: 39 bytes */
#pragma pack(pop)

/* 30-byte packed UDP payload for non-critical environmental data.
 * shuttle_id identifies the source; gateway correlates to CoAP stream via shuttle_id.
 * Python unpack: struct.unpack('<12sHIfff', data)
 * pressure_hpa = 0.0 is the sentinel for "LPS22HH unavailable or read failed". */
#pragma pack(push, 1)
typedef struct {
  char     shuttle_id[12];  /* null-padded ASCII, same value as CriticalPayload */
  uint16_t sequence_id;     /* monotonic counter shared with CoAP sequence space */
  uint32_t tick_ms;         /* HAL_GetTick() at sensor read time */
  float    temp_c;          /* °C from HTS221 */
  float    humidity_pct;    /* % RH from HTS221, clamped [0, 100] */
  float    pressure_hpa;    /* hPa from LPS22HH; 0.0 if sensor unavailable */
} NonCriticalPayload;       /* total: 30 bytes */
#pragma pack(pop)

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* State-based power estimation constants for POWER_EstimateMilliwatts().
 * Source: STM32U585 DS13259 §6.3.7 Table 31 (160 MHz LDO run, 3.3V, 25°C).
 *         MXCHIP EMW3080 datasheet §5.2 (802.11n HT20, 3.3V supply).
 * These are conservative mid-range figures. Calibrate with a bench ammeter if precision matters. */
#define POWER_EST_MCU_RUN_MA        15.0f   /* STM32U585 run mode @ 160 MHz */
#define POWER_EST_SENSORS_MA         2.0f   /* ISM330 + HTS221 + LPS22HH on I2C2 */
#define POWER_EST_WIFI_ASSOC_MA     10.0f   /* MXCHIP associated, no active TX */
#define POWER_EST_WIFI_TX_MA       200.0f   /* MXCHIP burst TX (802.11n MCS5) */
#define POWER_EST_VDD_MV          3300.0f   /* 3.3V board rail */

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
// --- PLUDOS State Machine ---
typedef enum {
  STATE_IDLE,
  STATE_MOVING
} ShuttleState_t;

static ShuttleState_t current_state = STATE_IDLE;
static uint32_t last_movement_tick = 0;

#define MOVEMENT_THRESHOLD_G2    0.05f    /* deviation from 1g² below which the shuttle is considered still */
#define MOVEMENT_DWELL_MS        500U     /* continuous movement required before entering STATE_MOVING */
#define NO_MOVEMENT_TIMEOUT_MS   10000U   /* continuous stillness required before returning to STATE_IDLE */

#define IDLE_SAMPLE_DELAY_MS     500U     /* 2 Hz sampling in IDLE */
#define MOVING_SAMPLE_DELAY_MS   20U      /* 50 Hz sampling in MOVING */


// Sensor I2C Address (0x6A shifted left by 1)
#define ISM330_ADDR 0xD6

// Sensor Registers
#define CTRL1_XL 0x10
#define OUTX_L_A 0x28

// Global physics variables so the Live Watch can see them
float vib_x = 0.0f;
float vib_y = 0.0f;
float vib_z = 0.0f;

static uint16_t current_packet_num = 1U;

// =========================================================================
// PLUDOS NETWORK CONFIGURATION
// =========================================================================
/* WIFI_SSID, WIFI_PASSWORD, JETSON_IP, and SHUTTLE_ID are in
 * wifi_credentials.h (gitignored — copy from wifi_credentials.h.example). */
#define JETSON_PORT    5683U  /* CoAP CON critical packets — owned by aiocoap */
#define JETSON_NC_PORT 5684U  /* raw UDP non-critical packets — separate socket on gateway */

#define SENSOR_BUFFER_CAPACITY              256U
#define SENSOR_BUFFER_TRIGGER_COUNT         ((SENSOR_BUFFER_CAPACITY * 70U) / 100U)
#define SENSOR_BUFFER_SUSPEND_COUNT         ((SENSOR_BUFFER_CAPACITY * 95U) / 100U)
#define IDLE_TRANSMIT_JITTER_MAX_MS         2000U   /* uniform window for IDLE-entry delay */
#define COAP_URI_PATH                       "vib"
#define COAP_PAYLOAD_BUFFER_SIZE            1024U
#define COAP_PACKET_BUFFER_SIZE             1152U
#define COAP_ACK_BUFFER_SIZE                128U
#define COAP_ACK_TIMEOUT_MS                 2000U
#define COAP_MAX_RETRY_COUNT                4U   /* max transmit attempts per packet (RFC 7252 §4.8) */

static int32_t socket_id = -1;       /* -1 = socket closed */
static char    uart_buf[120];         /* scratch buffer shared by all UART log messages */

// Pointer to the MXCHIP driver object owned by the ST WiFi transport layer
MX_WIFIObject_t *wifi_obj = NULL;
static volatile uint8_t wifi_driver_initialized = 0;
static volatile uint8_t wifi_station_event = 0xFF;
static volatile uint8_t wifi_station_ready = 0;
static SensorSample_t sensor_buffer[SENSOR_BUFFER_CAPACITY];
static uint16_t sensor_buffer_head = 0U;
static uint16_t sensor_buffer_tail = 0U;
static uint16_t sensor_buffer_count = 0U;
static uint8_t sensor_flush_requested = 0U;
static uint16_t coap_message_id = 1U;
static uint32_t sensor_buffer_overflow_count = 0U;
static char     jetson_ip[16]   = {0};   /* populated from JETSON_IP define at init */
static uint32_t idle_entry_tick = 0U;    /* HAL_GetTick() when MOVING→IDLE transition occurred */
static uint32_t idle_jitter_ms  = 0U;   /* per-shuttle delay before first IDLE flush */
static uint8_t  hts221_initialized = 0U; /* set to 1 if SENSOR_Humidity_Init succeeds */
static uint8_t  lps22hh_initialized = 0U; /* set to 1 if SENSOR_Pressure_Init succeeds */

/* These drive the state machine across loop iterations — file scope keeps them
 * visible to any future helper functions without needing to pass by pointer. */
static uint32_t continuous_movement_start_tick = 0U; /* tick when unbroken movement began */
static uint8_t  suspend_sampling = 0U;               /* 1 while buffer ≥ 95% — halts I2C reads */

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
static void SENSOR_BufferPush(const SensorSample_t *sample);
static void SENSOR_BufferDrop(uint16_t sample_count);
static int32_t SENSOR_BuildSamplePayload(char *payload, uint32_t payload_size, uint16_t *samples_built);
static uint16_t COAP_BuildConfirmablePost(uint8_t *packet, uint16_t packet_size,
                                          const uint8_t *payload, uint16_t payload_len,
                                          uint16_t message_id, const uint8_t *token, uint8_t token_len);
static uint8_t COAP_IsAckValid(const uint8_t *packet, int32_t packet_len,
                               uint16_t expected_message_id, const uint8_t *expected_token, uint8_t expected_token_len);
static void NETWORK_ConfigureUdpSocket(int32_t sock_fd, uint32_t timeout_ms);
static int32_t COAP_SendBufferedBatch(void);
static void NETWORK_ProcessBufferedSamples(void);
static float POWER_EstimateMilliwatts(void);

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

static void SENSOR_BufferPush(const SensorSample_t *sample)
{
  if (sample == NULL)
  {
    return;
  }

  if (sensor_buffer_count >= SENSOR_BUFFER_CAPACITY)
  {
    sensor_buffer_tail = (uint16_t)((sensor_buffer_tail + 1U) % SENSOR_BUFFER_CAPACITY);
    sensor_buffer_count--;
    sensor_buffer_overflow_count++;

    if ((sensor_buffer_overflow_count % 10U) == 1U)
    {
      sprintf(uart_buf, "[BUFFER] WARNING: overflow, dropped oldest sample(s): %lu\r\n",
              (unsigned long)sensor_buffer_overflow_count);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    }
  }

  sensor_buffer[sensor_buffer_head] = *sample;
  sensor_buffer_head = (uint16_t)((sensor_buffer_head + 1U) % SENSOR_BUFFER_CAPACITY);
  sensor_buffer_count++;

  if (sensor_buffer_count >= SENSOR_BUFFER_TRIGGER_COUNT)
  {
    sensor_flush_requested = 1U;
  }
}

static void SENSOR_BufferDrop(uint16_t sample_count)
{
  uint16_t drop_count = MIN(sample_count, sensor_buffer_count);

  if (drop_count == 0U)
  {
    return;
  }

  sensor_buffer_tail = (uint16_t)((sensor_buffer_tail + drop_count) % SENSOR_BUFFER_CAPACITY);
  sensor_buffer_count = (uint16_t)(sensor_buffer_count - drop_count);

  if (sensor_buffer_count == 0U)
  {
    sensor_flush_requested = 0U;
  }
}

/* Serialise the oldest buffered sample into a packed CriticalPayload; returns byte count or -1. */
static int32_t SENSOR_BuildSamplePayload(char *payload, uint32_t payload_size, uint16_t *samples_built)
{
  const SensorSample_t *sample;
  CriticalPayload coap_data = {0};

  if ((payload == NULL) || (samples_built == NULL) || (payload_size == 0U) || (sensor_buffer_count == 0U))
  {
    return -1;
  }

  sample = &sensor_buffer[sensor_buffer_tail]; /* oldest sample */

  memcpy(coap_data.shuttle_id, SHUTTLE_ID, sizeof(coap_data.shuttle_id)); /* copies 11 chars + null from literal */
  coap_data.sequence_id    = sample->sequence_id;
  coap_data.tick_ms        = sample->tick_ms;
  coap_data.mission_active = (current_state == STATE_MOVING) ? 1U : 0U; /* 0 signals mission end to gateway */
  coap_data.ram_usage_pct  = (float)sensor_buffer_count / (float)SENSOR_BUFFER_CAPACITY * 100.0f;
  coap_data.accel_x        = sample->accel_x;
  coap_data.accel_y        = sample->accel_y;
  coap_data.accel_z        = sample->accel_z;
  coap_data.power_mw       = sample->power_mw;

  memcpy(payload, &coap_data, sizeof(CriticalPayload));
  *samples_built = 1U;

  return (int32_t)sizeof(CriticalPayload);
}

static uint16_t COAP_BuildConfirmablePost(uint8_t *packet, uint16_t packet_size,
                                          const uint8_t *payload, uint16_t payload_len,
                                          uint16_t message_id, const uint8_t *token, uint8_t token_len)
{
  uint16_t pos = 0U;
  const uint8_t uri_path_len = (uint8_t)strlen(COAP_URI_PATH);

  if ((packet == NULL) || (token == NULL) || (token_len > 8U) || (uri_path_len >= 13U))
  {
    return 0U;
  }

  if ((uint32_t)packet_size < (uint32_t)(4U + token_len + 1U + uri_path_len + 2U + 1U + payload_len))
  {
    return 0U;
  }

  packet[pos++] = (uint8_t)(0x40U | token_len);  // Ver=1, Type=CON, TKL=token_len
  packet[pos++] = 0x02U;                         // POST
  packet[pos++] = (uint8_t)(message_id >> 8);
  packet[pos++] = (uint8_t)(message_id & 0xFFU);

  (void)memcpy(&packet[pos], token, token_len);
  pos = (uint16_t)(pos + token_len);

  packet[pos++] = (uint8_t)((11U << 4) | uri_path_len);  // Uri-Path: "vib"
  (void)memcpy(&packet[pos], COAP_URI_PATH, uri_path_len);
  pos = (uint16_t)(pos + uri_path_len);

  packet[pos++] = 0x11U;  // Option delta=1 (Content-Format), length=1
  packet[pos++] = 42U;    // application/octet-stream

  if ((payload != NULL) && (payload_len > 0U))
  {
    packet[pos++] = 0xFFU;
    (void)memcpy(&packet[pos], payload, payload_len);
    pos = (uint16_t)(pos + payload_len);
  }

  return pos;
}

static uint8_t COAP_IsAckValid(const uint8_t *packet, int32_t packet_len,
                               uint16_t expected_message_id, const uint8_t *expected_token, uint8_t expected_token_len)
{
  uint8_t version;
  uint8_t type;
  uint8_t token_len;
  uint8_t code;
  uint16_t message_id;

  if ((packet == NULL) || (expected_token == NULL) || (packet_len < 4))
  {
    return 0U;
  }

  version = (uint8_t)(packet[0] >> 6);
  type = (uint8_t)((packet[0] >> 4) & 0x03U);
  token_len = (uint8_t)(packet[0] & 0x0FU);
  code = packet[1];
  message_id = (uint16_t)(((uint16_t)packet[2] << 8) | packet[3]);

  if ((version != 1U) || (type != 2U) || (token_len != expected_token_len) ||
      (message_id != expected_message_id) || (packet_len < (4 + expected_token_len)))
  {
    return 0U;
  }

  if (memcmp(&packet[4], expected_token, expected_token_len) != 0)
  {
    return 0U;
  }

  if ((code == 0U) || ((code >> 5) == 2U))
  {
    return 1U;
  }

  return 0U;
}

static void NETWORK_ConfigureUdpSocket(int32_t sock_fd, uint32_t timeout_ms)
{
  struct mx_timeval timeout = {0};
  int32_t status;

  if ((wifi_obj == NULL) || (sock_fd < 0))
  {
    return;
  }

  timeout.tv_sec  = (long)(timeout_ms / 1000U);
  timeout.tv_usec = (long)((timeout_ms % 1000U) * 1000U);

  status = MX_WIFI_Socket_setsockopt(wifi_obj, sock_fd, MX_SOL_SOCKET, MX_SO_RCVTIMEO,
                                     &timeout, sizeof(timeout));
  if (status != 0)
  {
    sprintf(uart_buf, "[NETWORK] WARNING: set RX timeout failed on socket %ld\r\n", (long)sock_fd);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  }

  status = MX_WIFI_Socket_setsockopt(wifi_obj, sock_fd, MX_SOL_SOCKET, MX_SO_SNDTIMEO,
                                     &timeout, sizeof(timeout));
  if (status != 0)
  {
    sprintf(uart_buf, "[NETWORK] WARNING: set TX timeout failed on socket %ld\r\n", (long)sock_fd);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  }
}

static int32_t COAP_SendBufferedBatch(void)
{
  uint8_t token[2];
  uint8_t coap_packet[COAP_PACKET_BUFFER_SIZE] = {0};
  uint8_t ack_packet[COAP_ACK_BUFFER_SIZE] = {0};
  char coap_payload_buf[COAP_PAYLOAD_BUFFER_SIZE] = {0};
  struct mx_sockaddr_in dest_addr = {0};
  struct mx_sockaddr_in from_addr = {0};
  uint32_t from_addr_len = sizeof(from_addr);
  uint16_t samples_in_batch = 0U;
  uint16_t packet_len;
  uint16_t message_id = coap_message_id;
  int32_t payload_len;
  uint8_t attempt;

  if ((socket_id < 0) || (wifi_station_ready == 0U) || (sensor_buffer_count == 0U))
  {
    return -1;
  }

  payload_len = SENSOR_BuildSamplePayload(coap_payload_buf, sizeof(coap_payload_buf), &samples_in_batch);
  if ((payload_len <= 0) || (samples_in_batch == 0U))
  {
    return -1;
  }

  token[0] = (uint8_t)(message_id >> 8);
  token[1] = (uint8_t)(message_id & 0xFFU);

  packet_len = COAP_BuildConfirmablePost(coap_packet, sizeof(coap_packet),
                                         (const uint8_t *)coap_payload_buf, (uint16_t)payload_len,
                                         message_id, token, sizeof(token));
  if (packet_len == 0U)
  {
    return -1;
  }

  dest_addr.sin_len = sizeof(dest_addr);
  dest_addr.sin_family = MX_AF_INET;
  dest_addr.sin_port = JETSON_PORT;
  dest_addr.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  // RFC 7252 Binary Exponential Backoff
  uint32_t current_timeout_ms = COAP_ACK_TIMEOUT_MS; // Start at 2000ms

  for (attempt = 1U; attempt <= COAP_MAX_RETRY_COUNT; attempt++)
  {
    int32_t sent_result;
    int32_t recv_result;

    NETWORK_ConfigureUdpSocket(socket_id, current_timeout_ms);

    sent_result = MX_WIFI_Socket_sendto(wifi_obj, socket_id, coap_packet, packet_len,
                                        0, (struct mx_sockaddr *)&dest_addr, sizeof(dest_addr));

    if (sent_result != packet_len)
    {
      sprintf(uart_buf, "[COAP] ERROR: send failed on try %u/4\r\n", attempt);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      current_timeout_ms *= 2;
      continue;
    }

    from_addr_len = sizeof(from_addr);
    recv_result = MX_WIFI_Socket_recvfrom(wifi_obj, socket_id, ack_packet, sizeof(ack_packet),
                                          0, (struct mx_sockaddr *)&from_addr, &from_addr_len);

    if (COAP_IsAckValid(ack_packet, recv_result, message_id, token, sizeof(token)) != 0U)
    {
      sprintf(uart_buf, "[COAP] ACK received for MID=0x%04X, sample sent\r\n", message_id);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

      SENSOR_BufferDrop(1);  // Drop 1 sample
      coap_message_id++;
      return 1;  // Sent 1 sample
    }

    sprintf(uart_buf, "[COAP] No valid ACK on try %u/4 for MID=0x%04X. Backoff: %lu ms\r\n",
            attempt, message_id, (unsigned long)current_timeout_ms);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    
    current_timeout_ms *= 2; // Exponential backoff
  }

  coap_message_id++;
  return -1;
}

/* Send temperature and humidity over raw UDP during STATE_IDLE; drops packet on sensor error. */
static void UDP_SendNonCritical(void)
{
  NonCriticalPayload payload = {0};
  struct mx_sockaddr_in dest_addr = {0};

  if ((socket_id < 0) || (wifi_station_ready == 0U) || (jetson_ip[0] == 0))
  {
    return;
  }
  if (hts221_initialized == 0U)
  {
    return; /* sensor not available — drop rather than send stale placeholder */
  }

  if (SENSOR_Humidity_Read(&hi2c2, &payload.temp_c, &payload.humidity_pct) != 0)
  {
    return; /* data not ready or I2C error — skip this cycle */
  }

  /* Identify the source shuttle so the gateway can correlate env data to a shuttle */
  memcpy(payload.shuttle_id, SHUTTLE_ID, sizeof(payload.shuttle_id));
  payload.sequence_id = current_packet_num;
  payload.tick_ms     = HAL_GetTick();

  /* Read pressure; stays 0.0f (sentinel = unavailable) if sensor not initialised or times out */
  if (lps22hh_initialized != 0U)
  {
    (void)SENSOR_Pressure_Read(&hi2c2, &payload.pressure_hpa);
  }

  dest_addr.sin_len    = sizeof(dest_addr);
  dest_addr.sin_family = MX_AF_INET;
  dest_addr.sin_port   = JETSON_NC_PORT;  /* separate port — avoids collision with aiocoap on 5683 */
  dest_addr.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  MX_WIFI_Socket_sendto(wifi_obj, socket_id, (uint8_t *)&payload, sizeof(payload),
                        0, (struct mx_sockaddr *)&dest_addr, sizeof(dest_addr));
}

static void NETWORK_ProcessBufferedSamples(void)
{
  int32_t send_result;

  if ((socket_id < 0) || (wifi_station_ready == 0U) || (sensor_buffer_count == 0U))
  {
    return;
  }

  if ((sensor_flush_requested == 0U) && (sensor_buffer_count < SENSOR_BUFFER_TRIGGER_COUNT))
  {
    return;
  }

  send_result = COAP_SendBufferedBatch();

  if (send_result > 0)
  {
    sprintf(uart_buf, "[BUFFER] CoAP flush ok. Remaining: %u/%u\r\n",
            sensor_buffer_count, SENSOR_BUFFER_CAPACITY);
  }
  else
  {
    sprintf(uart_buf, "[BUFFER] CoAP flush pending. Buffered: %u/%u\r\n",
            sensor_buffer_count, SENSOR_BUFFER_CAPACITY);
  }
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
}

/* Estimate total board power from datasheet current figures — no ADC wired (P2-2).
 * MCU run mode + I2C sensors are constant; WiFi TX dominates in STATE_MOVING.
 * Accuracy: ±40%. Replace with real ADC reading once a shunt + CubeMX path is added. */
static float POWER_EstimateMilliwatts(void)
{
  float current_ma = POWER_EST_MCU_RUN_MA + POWER_EST_SENSORS_MA;

  if (wifi_station_ready != 0U)
  {
    /* MOVING = 50 Hz CoAP burst TX; IDLE = occasional UDP only */
    current_ma += (current_state == STATE_MOVING) ? POWER_EST_WIFI_TX_MA
                                                  : POWER_EST_WIFI_ASSOC_MA;
  }

  return current_ma * (POWER_EST_VDD_MV / 1000.0f); /* I(mA) × V(V) = P(mW) */
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

  // -----------------------------------------------------------------
  // PRESSURE SENSOR INITIALIZATION (LPS22HH)
  // -----------------------------------------------------------------
  if (SENSOR_Pressure_Init(&hi2c2) == 0)
  {
    lps22hh_initialized = 1U;
    sprintf(uart_buf, "[SENSOR] LPS22HH initialized (1 Hz, BDU)\r\n");
  }
  else
  {
    /* Non-fatal: pressure field in UDP packet will be 0.0 */
    sprintf(uart_buf, "[SENSOR] WARNING: LPS22HH not found on I2C2 — pressure disabled\r\n");
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

            // --- ZERO-TOUCH PROVISIONING: DYNAMIC IP DISCOVERY ---
            sprintf(uart_buf, "[NETWORK] Skipping beacon discovery, using hardcoded IP\r\n");
            HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);

            // Skip discovery, use hardcoded IP
            strcpy(jetson_ip, JETSON_IP);

            socket_id = MX_WIFI_Socket_create(wifi_obj, MX_AF_INET, MX_SOCK_DGRAM, MX_IPPROTO_UDP);
            if (socket_id >= 0)
            {
              sprintf(uart_buf, "[NETWORK] UDP Socket created (ID: %ld)\r\n", (long)socket_id);
              HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
              NETWORK_ConfigureUdpSocket(socket_id, COAP_ACK_TIMEOUT_MS);
              sprintf(uart_buf, "[NETWORK] CoAP confirmable mode armed on udp://%s:%u/%s\r\n",
                      jetson_ip, JETSON_PORT, COAP_URI_PATH);
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
      if ((wifi_obj != NULL) && (wifi_driver_initialized != 0U))
      {
        (void)MX_WIFI_IO_YIELD(wifi_obj, 1);
      }

      // Memory Protection Logic
      if (current_state == STATE_MOVING && sensor_buffer_count >= SENSOR_BUFFER_SUSPEND_COUNT) {
          if (!suspend_sampling) {
              suspend_sampling = 1U;
              sprintf(uart_buf, "[BUFFER] CRITICAL: SRAM at 95%%! Suspending ADC/I2C sampling.\r\n");
              HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
          }
      }

      if (suspend_sampling) {
          /* Buffer drains only on successful CoAP ACK (via SENSOR_BufferDrop),
           * so empty-in-IDLE is equivalent to "IDLE + ACK received". */
          if (current_state == STATE_IDLE && sensor_buffer_count == 0U) {
              suspend_sampling = 0U;
              sprintf(uart_buf, "[BUFFER] Buffer cleared. Resuming sampling.\r\n");
              HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
          }
      }

      if (!suspend_sampling) {
	      // -----------------------------------------------------------------
	      // PHASE 1: SENSOR ACQUISITION
	      // -----------------------------------------------------------------
	      uint8_t raw_data[6] = {0};
          float a_mag_g2 = 0.0f; // Acceleration magnitude squared in g

	      // Read accelerometer. Only update floats if I2C returns HAL_OK.
	      if (HAL_I2C_Mem_Read(&hi2c2, ISM330_ADDR, OUTX_L_A, 1, raw_data, 6, 100) == HAL_OK)
	      {
		      int16_t raw_x = (int16_t)((raw_data[1] << 8) | raw_data[0]);
		      int16_t raw_y = (int16_t)((raw_data[3] << 8) | raw_data[2]);
		      int16_t raw_z = (int16_t)((raw_data[5] << 8) | raw_data[4]);

		      vib_x = (raw_x * 0.061f) / 1000.0f;
		      vib_y = (raw_y * 0.061f) / 1000.0f;
		      vib_z = (raw_z * 0.061f) / 1000.0f;

		      // --- STATE MACHINE LOGIC ---
		      a_mag_g2 = (vib_x * vib_x) + (vib_y * vib_y) + (vib_z * vib_z);
		      float deviation = fabsf(a_mag_g2 - 1.0f); // Deviation from 1g (gravity)

		      if (deviation > MOVEMENT_THRESHOLD_G2) {
			      if (current_state == STATE_IDLE) {
                      if (continuous_movement_start_tick == 0) {
                          continuous_movement_start_tick = HAL_GetTick();
                      } else if (HAL_GetTick() - continuous_movement_start_tick >= MOVEMENT_DWELL_MS) {
				          sprintf(uart_buf, "[STATE] Continuous movement for 500ms! Switching to MOVING state.\r\n");
				          HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
				          current_state = STATE_MOVING;
                          continuous_movement_start_tick = 0; // Reset
			          }
                  }
			      last_movement_tick = HAL_GetTick();
		      } else {
                  continuous_movement_start_tick = 0; // Reset immediately if threshold drops
			      if (current_state == STATE_MOVING) {
				      if (HAL_GetTick() - last_movement_tick > NO_MOVEMENT_TIMEOUT_MS) {
					      sprintf(uart_buf, "[STATE] No movement for %d seconds. Switching to IDLE state.\r\n", NO_MOVEMENT_TIMEOUT_MS / 1000);
					      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
					      current_state   = STATE_IDLE;
                          idle_entry_tick = HAL_GetTick();
                          /* Jitter window: spread flushes across shuttles entering IDLE simultaneously */
                          idle_jitter_ms  = HAL_GetTick() % IDLE_TRANSMIT_JITTER_MAX_MS;
                          /* sensor_flush_requested armed by jitter check in PHASE 3 */
				      }
			      }
		      }

		      // -----------------------------------------------------------------
		      // PHASE 2: RAM BUFFERING (only if sensor read was successful)
		      // -----------------------------------------------------------------
		      SensorSample_t sample = {0};

		      sample.sequence_id = current_packet_num;
		      sample.tick_ms     = HAL_GetTick();
		      sample.power_mw    = POWER_EstimateMilliwatts(); /* datasheet estimate; real ADC in P2-2 */
		      sample.accel_x     = vib_x;
		      sample.accel_y     = vib_y;
		      sample.accel_z     = vib_z;

		      SENSOR_BufferPush(&sample);
		      current_packet_num++; // Increment packet number for each sample
	      }
	      else
	      {
		      vib_x = 99.0f; vib_y = 99.0f; vib_z = 99.0f; // Hardware fault state
		      sprintf(uart_buf, "[SENSOR] ERROR: I2C read failed\r\n");
		      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
	      }
      } // End of if (!suspend_sampling)


	  // -----------------------------------------------------------------
	  // PHASE 3: COAP FLUSH WHEN BUFFER REACHES 70%
	  // -----------------------------------------------------------------
	  /* Arm the IDLE flush only once the per-shuttle jitter window has elapsed.
	   * If MOVING is re-entered before the window expires, flush stays disarmed. */
	  if ((current_state == STATE_IDLE) && (sensor_flush_requested == 0U) &&
	      (sensor_buffer_count > 0U) &&
	      (HAL_GetTick() - idle_entry_tick >= idle_jitter_ms))
	  {
	      sensor_flush_requested = 1U;
	  }
	  NETWORK_ProcessBufferedSamples();

	  // -----------------------------------------------------------------
	  // PHASE 4: STATE-BASED DELAY
	  // -----------------------------------------------------------------
	  if (current_state == STATE_MOVING) {
		  WIFI_DelayWithYield(MOVING_SAMPLE_DELAY_MS);
	  } else {
          UDP_SendNonCritical(); // Send Non-Critical telemetry during idle
		  WIFI_DelayWithYield(IDLE_SAMPLE_DELAY_MS);
	  }
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
