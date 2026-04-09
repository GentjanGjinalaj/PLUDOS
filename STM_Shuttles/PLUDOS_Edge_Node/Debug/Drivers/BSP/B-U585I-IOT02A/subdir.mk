################################################################################
# Automatically-generated file. Do not edit!
# Toolchain: GNU Tools for STM32 (13.3.rel1)
################################################################################

# Add inputs and outputs from these tool invocations to the build variables 
C_SRCS += \
../Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a.c \
../Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_bus.c 

OBJS += \
./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a.o \
./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_bus.o 

C_DEPS += \
./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a.d \
./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_bus.d 


# Each subdirectory must supply rules for building sources it contributes
Drivers/BSP/B-U585I-IOT02A/%.o Drivers/BSP/B-U585I-IOT02A/%.su Drivers/BSP/B-U585I-IOT02A/%.cyclo: ../Drivers/BSP/B-U585I-IOT02A/%.c Drivers/BSP/B-U585I-IOT02A/subdir.mk
	arm-none-eabi-gcc "$<" -mcpu=cortex-m33 -std=gnu11 -g3 -DDEBUG -DUSE_FULL_LL_DRIVER -DUSE_HAL_DRIVER -DSTM32U585xx -c -I../Core/Inc -I../Drivers/STM32U5xx_HAL_Driver/Inc -I../Drivers/STM32U5xx_HAL_Driver/Inc/Legacy -I../Drivers/CMSIS/Device/ST/STM32U5xx/Include -I../Drivers/CMSIS/Include -I"/home/ggjinalaj/STM_Shuttles/PLUDOS_Edge_Node/Drivers/BSP/Components/mx_wifi" -I"/home/ggjinalaj/STM_Shuttles/PLUDOS_Edge_Node/Drivers/BSP/Components/mx_wifi/core" -I"/home/ggjinalaj/STM_Shuttles/PLUDOS_Edge_Node/Drivers/BSP/B-U585I-IOT02A" -I"/home/ggjinalaj/STM_Shuttles/PLUDOS_Edge_Node/Drivers/BSP/Components/mx_wifi/io_pattern" -O0 -ffunction-sections -fdata-sections -Wall -fstack-usage -fcyclomatic-complexity -MMD -MP -MF"$(@:%.o=%.d)" -MT"$@" --specs=nano.specs -mfpu=fpv5-sp-d16 -mfloat-abi=hard -mthumb -o "$@"

clean: clean-Drivers-2f-BSP-2f-B-2d-U585I-2d-IOT02A

clean-Drivers-2f-BSP-2f-B-2d-U585I-2d-IOT02A:
	-$(RM) ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a.cyclo ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a.d ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a.o ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a.su ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_bus.cyclo ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_bus.d ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_bus.o ./Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_bus.su

.PHONY: clean-Drivers-2f-BSP-2f-B-2d-U585I-2d-IOT02A

