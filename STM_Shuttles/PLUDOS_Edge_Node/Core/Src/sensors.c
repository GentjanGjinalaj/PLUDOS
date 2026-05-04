/* sensors.c — HTS221 temperature+humidity driver for B-U585I-IOT02A.
 * I2C2, 7-bit address 0x5F (8-bit 0xBE). I2C2 already configured by CubeMX for ISM330. */

#include "sensors.h"
#include <string.h>

/* HTS221 register addresses */
#define HTS221_ADDR       0xBEU          /* 8-bit: 0x5F << 1 */
#define HTS221_WHO_AM_I   0x0FU
#define HTS221_WHOAMI_VAL 0xBCU
#define HTS221_AV_CONF    0x10U
#define HTS221_CTRL_REG1  0x20U
#define HTS221_STATUS_REG 0x27U
#define HTS221_HUM_OUT_L  0x28U          /* read 4 bytes: H_L, H_H, T_L, T_H */
#define HTS221_CAL_START  (0x30U | 0x80U) /* 0x80 enables auto-increment burst */

#define HTS221_AV_CONF_VAL  0x1BU        /* 16× temp avg, 32× humidity avg */
#define HTS221_CTRL_REG1_ON 0x85U        /* PD=1 (on), BDU=1, ODR=01 (1 Hz) */

#define HTS221_CAL_LEN      16U
#define HTS221_I2C_TIMEOUT  25U          /* ms; HAL I2C single-byte worst case */
#define HTS221_DRDY_WAIT_MS 10U

/* Calibration stored at init; shared across all Read calls */
static float    hts221_H0, hts221_H1;
static float    hts221_T0, hts221_T1;
static int16_t  hts221_H0_out, hts221_H1_out;
static int16_t  hts221_T0_out, hts221_T1_out;
static uint8_t  hts221_ready = 0U;

/* Decode the 16-byte calibration burst into module-level calibration statics. */
static void decode_calibration(const uint8_t *cal)
{
    /* T0 and T1 are 10-bit values split across two registers */
    uint16_t T0_x8 = ((uint16_t)(cal[5] & 0x03U) << 8) | cal[2];
    uint16_t T1_x8 = ((uint16_t)((cal[5] >> 2U) & 0x03U) << 8) | cal[3];
    hts221_T0 = (float)T0_x8 / 8.0f;
    hts221_T1 = (float)T1_x8 / 8.0f;

    hts221_H0 = (float)cal[0] / 2.0f;
    hts221_H1 = (float)cal[1] / 2.0f;

    /* H0_T0_OUT at bytes 6-7, H1_T0_OUT at bytes 10-11 (bytes 8-9 reserved) */
    hts221_H0_out = (int16_t)((uint16_t)cal[7] << 8 | cal[6]);
    hts221_H1_out = (int16_t)((uint16_t)cal[11] << 8 | cal[10]);

    hts221_T0_out = (int16_t)((uint16_t)cal[13] << 8 | cal[12]);
    hts221_T1_out = (int16_t)((uint16_t)cal[15] << 8 | cal[14]);
}

int8_t SENSOR_Humidity_Init(I2C_HandleTypeDef *hi2c)
{
    uint8_t val;
    uint8_t cal[HTS221_CAL_LEN];

    /* Verify device identity */
    if (HAL_I2C_Mem_Read(hi2c, HTS221_ADDR, HTS221_WHO_AM_I,
                         I2C_MEMADD_SIZE_8BIT, &val, 1, HTS221_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }
    if (val != HTS221_WHOAMI_VAL)
    {
        return -1;
    }

    /* Set averaging configuration (keep explicit even though 0x1B is reset default) */
    val = HTS221_AV_CONF_VAL;
    if (HAL_I2C_Mem_Write(hi2c, HTS221_ADDR, HTS221_AV_CONF,
                          I2C_MEMADD_SIZE_8BIT, &val, 1, HTS221_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }

    /* Power on, enable BDU, set ODR to 1 Hz */
    val = HTS221_CTRL_REG1_ON;
    if (HAL_I2C_Mem_Write(hi2c, HTS221_ADDR, HTS221_CTRL_REG1,
                          I2C_MEMADD_SIZE_8BIT, &val, 1, HTS221_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }

    /* Read all 16 calibration bytes in one burst (auto-increment via bit 7) */
    if (HAL_I2C_Mem_Read(hi2c, HTS221_ADDR, HTS221_CAL_START,
                         I2C_MEMADD_SIZE_8BIT, cal, HTS221_CAL_LEN, HTS221_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }

    decode_calibration(cal);
    hts221_ready = 1U;
    return 0;
}

int8_t SENSOR_Humidity_Read(I2C_HandleTypeDef *hi2c, float *temp_c, float *humidity_pct)
{
    uint8_t status;
    uint8_t raw[4]; /* H_L, H_H, T_L, T_H */
    int16_t raw_H, raw_T;
    float rh;

    if (hts221_ready == 0U)
    {
        return -1;
    }

    /* Poll STATUS_REG for both T_DA (bit 1) and H_DA (bit 0) */
    if (HAL_I2C_Mem_Read(hi2c, HTS221_ADDR, HTS221_STATUS_REG,
                         I2C_MEMADD_SIZE_8BIT, &status, 1, HTS221_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }
    if ((status & 0x03U) != 0x03U)
    {
        /* Data not ready yet; wait once and retry */
        HAL_Delay(HTS221_DRDY_WAIT_MS);
        if (HAL_I2C_Mem_Read(hi2c, HTS221_ADDR, HTS221_STATUS_REG,
                             I2C_MEMADD_SIZE_8BIT, &status, 1, HTS221_I2C_TIMEOUT) != HAL_OK)
        {
            return -1;
        }
        if ((status & 0x03U) != 0x03U)
        {
            return -1; /* Still not ready — caller skips this cycle */
        }
    }

    /* Read humidity (0x28-0x29) and temperature (0x2A-0x2B) in one 4-byte burst.
     * Auto-increment enabled by ORing 0x80 with the start address. */
    if (HAL_I2C_Mem_Read(hi2c, HTS221_ADDR, HTS221_HUM_OUT_L | 0x80U,
                         I2C_MEMADD_SIZE_8BIT, raw, 4, HTS221_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }

    raw_H = (int16_t)((uint16_t)raw[1] << 8 | raw[0]);
    raw_T = (int16_t)((uint16_t)raw[3] << 8 | raw[2]);

    /* Linear interpolation from datasheet §4.6 */
    *temp_c = hts221_T0 + (float)(raw_T - hts221_T0_out)
              * (hts221_T1 - hts221_T0) / (float)(hts221_T1_out - hts221_T0_out);

    rh = hts221_H0 + (float)(raw_H - hts221_H0_out)
         * (hts221_H1 - hts221_H0) / (float)(hts221_H1_out - hts221_H0_out);

    /* Clamp: sensor occasionally returns slightly out-of-range values */
    if (rh < 0.0f)   rh = 0.0f;
    if (rh > 100.0f) rh = 100.0f;
    *humidity_pct = rh;

    return 0;
}

/* =========================================================================
 * LPS22HH — absolute pressure sensor (I2C2, 8-bit addr 0xB8, SA0=GND).
 * No factory calibration read needed; output is in LSB/hPa directly.
 * Verify address against board schematic if WHO_AM_I fails at startup.
 * ========================================================================= */

#define LPS22HH_ADDR            0xB8U   /* 8-bit: 0x5C << 1 (SA0 tied to GND on IOT02A) */
#define LPS22HH_WHO_AM_I        0x0FU
#define LPS22HH_WHOAMI_VAL      0xB3U
#define LPS22HH_CTRL_REG1       0x10U
#define LPS22HH_STATUS          0x27U
#define LPS22HH_PRESS_OUT_XL    0x28U   /* LSB; 0x29 MID, 0x2A MSB — auto-increment on I2C */

/* ODR=1Hz (bits 6:4=001), BDU=1 (bit 1) — matches HTS221 ODR for synchronized reads */
#define LPS22HH_CTRL_REG1_VAL   0x12U
#define LPS22HH_PRESS_SENS      4096.0f /* LSB/hPa per datasheet §4.3 */
#define LPS22HH_I2C_TIMEOUT     25U
#define LPS22HH_DRDY_WAIT_MS    10U

/* Probe WHO_AM_I, set ODR=1Hz + BDU. No calibration registers — factory trimmed. */
int8_t SENSOR_Pressure_Init(I2C_HandleTypeDef *hi2c)
{
    uint8_t val;

    if (HAL_I2C_Mem_Read(hi2c, LPS22HH_ADDR, LPS22HH_WHO_AM_I,
                         I2C_MEMADD_SIZE_8BIT, &val, 1, LPS22HH_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }
    if (val != LPS22HH_WHOAMI_VAL)
    {
        return -1;
    }

    val = LPS22HH_CTRL_REG1_VAL;
    if (HAL_I2C_Mem_Write(hi2c, LPS22HH_ADDR, LPS22HH_CTRL_REG1,
                          I2C_MEMADD_SIZE_8BIT, &val, 1, LPS22HH_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }

    return 0;
}

/* Read 3-byte pressure output; I2C auto-increments address after each byte. */
int8_t SENSOR_Pressure_Read(I2C_HandleTypeDef *hi2c, float *pressure_hpa)
{
    uint8_t status;
    uint8_t raw[3];
    int32_t raw_p;

    if (pressure_hpa == NULL)
    {
        return -1;
    }

    /* Poll P_DA (bit 0) — pressure data available */
    if (HAL_I2C_Mem_Read(hi2c, LPS22HH_ADDR, LPS22HH_STATUS,
                         I2C_MEMADD_SIZE_8BIT, &status, 1, LPS22HH_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }
    if ((status & 0x01U) == 0U)
    {
        HAL_Delay(LPS22HH_DRDY_WAIT_MS);
        if (HAL_I2C_Mem_Read(hi2c, LPS22HH_ADDR, LPS22HH_STATUS,
                             I2C_MEMADD_SIZE_8BIT, &status, 1, LPS22HH_I2C_TIMEOUT) != HAL_OK)
        {
            return -1;
        }
        if ((status & 0x01U) == 0U)
        {
            return -1; /* still not ready — caller keeps payload.pressure_hpa = 0.0f */
        }
    }

    /* Read PRESS_OUT_XL + PRESS_OUT_L + PRESS_OUT_H in one burst */
    if (HAL_I2C_Mem_Read(hi2c, LPS22HH_ADDR, LPS22HH_PRESS_OUT_XL,
                         I2C_MEMADD_SIZE_8BIT, raw, 3, LPS22HH_I2C_TIMEOUT) != HAL_OK)
    {
        return -1;
    }

    /* Reconstruct 24-bit value then sign-extend to int32 */
    raw_p = (int32_t)(((uint32_t)raw[2] << 16) | ((uint32_t)raw[1] << 8) | raw[0]);
    if ((raw[2] & 0x80U) != 0U)
    {
        raw_p |= (int32_t)0xFF000000; /* two's complement sign extension */
    }

    *pressure_hpa = (float)raw_p / LPS22HH_PRESS_SENS;
    return 0;
}
