#include "b_u585i_iot02a_bus.h"
#include "mx_wifi.h"

extern SPI_HandleTypeDef hspi2;

// We define the physical memory for these objects right here
// so the Linker can never lose them again.
MX_WIFI_IO_t MxWifiObjIO;

static uint16_t SPI_Send_HAL(uint8_t *pData, uint16_t len) {
    // Turn on the Green LED while sending data
    HAL_GPIO_WritePin(GPIOH, GPIO_PIN_7, GPIO_PIN_SET);

    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_12, GPIO_PIN_RESET); // NSS Low
    HAL_StatusTypeDef status = HAL_SPI_Transmit(&hspi2, pData, len, 500);
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_12, GPIO_PIN_SET);   // NSS High

    HAL_GPIO_WritePin(GPIOH, GPIO_PIN_7, GPIO_PIN_RESET); // LED Off
    return (status == HAL_OK) ? len : 0;
}

// Do the same for SPI_Receive_HAL!
static uint16_t SPI_Receive_HAL(uint8_t *pData, uint16_t len) {
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_12, GPIO_PIN_RESET);
    uint16_t result = 0;
    if (HAL_SPI_Receive(&hspi2, pData, len, 1000) == HAL_OK) {
        result = len;
    }
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_12, GPIO_PIN_SET);
    return result;
}

/**
  * @brief  The function that connects the Wi-Fi "Brain" to the SPI "Nerves"
  */
int32_t WIFI_RegisterBusIO (void)
{
  MxWifiObjIO.IO_Init      = NULL;
  MxWifiObjIO.IO_DeInit    = NULL;
  MxWifiObjIO.IO_Send      = SPI_Send_HAL;
  MxWifiObjIO.IO_Receive   = SPI_Receive_HAL;

  return 0;
}
