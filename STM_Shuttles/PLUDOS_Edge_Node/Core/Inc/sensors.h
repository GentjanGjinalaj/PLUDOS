#ifndef SENSORS_H
#define SENSORS_H

#include "stm32u5xx_hal.h"

/* Probe WHO_AM_I, configure AV_CONF and CTRL_REG1, read calibration coefficients.
 * Returns 0 on success, -1 if sensor not found or I2C error. */
int8_t SENSOR_Humidity_Init(I2C_HandleTypeDef *hi2c);

/* Read temperature and relative humidity using cached calibration.
 * Polls STATUS_REG; waits up to 10 ms for data-ready before returning -1.
 * Returns 0 on success, -1 on I2C error or data-not-ready timeout. */
int8_t SENSOR_Humidity_Read(I2C_HandleTypeDef *hi2c, float *temp_c, float *humidity_pct);

/* Probe WHO_AM_I (0xB3), set CTRL_REG1 to 1 Hz + BDU.
 * Returns 0 on success, -1 if sensor not found or I2C error. */
int8_t SENSOR_Pressure_Init(I2C_HandleTypeDef *hi2c);

/* Read absolute pressure in hPa using a 3-byte burst read.
 * Polls STATUS_REG; waits up to 10 ms for data-ready before returning -1.
 * Returns 0 on success, -1 on I2C error or data-not-ready timeout. */
int8_t SENSOR_Pressure_Read(I2C_HandleTypeDef *hi2c, float *pressure_hpa);

#endif /* SENSORS_H */
