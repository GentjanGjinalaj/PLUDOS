import sys
import os

# Construct path relative to this script's location
script_dir = os.path.dirname(os.path.abspath(__file__))
file_path = os.path.join(script_dir, '../../Core/Src/main.c')

with open(file_path, "r") as f:
    content = f.read()

# 1. Update NETWORK_ConfigureUdpSocket to accept timeout
old_configure_udp = """static void NETWORK_ConfigureUdpSocket(int32_t sock_fd)
{
  struct mx_timeval timeout = {0};
  int32_t timeout_status;

  if ((wifi_obj == NULL) || (sock_fd < 0))
  {
    return;
  }

  timeout.tv_sec = (long)(COAP_ACK_TIMEOUT_MS / 1000U);
  timeout.tv_usec = (long)((COAP_ACK_TIMEOUT_MS % 1000U) * 1000U);

  timeout_status = MX_WIFI_Socket_setsockopt(wifi_obj, sock_fd, MX_SOL_SOCKET, MX_SO_RCVTIMEO,
                                             &timeout, sizeof(timeout));
  sprintf(uart_buf, "[NETWORK] UDP recv timeout set: %ld\\r\\n", (long)timeout_status);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

  timeout_status = MX_WIFI_Socket_setsockopt(wifi_obj, sock_fd, MX_SOL_SOCKET, MX_SO_SNDTIMEO,
                                             &timeout, sizeof(timeout));
  sprintf(uart_buf, "[NETWORK] UDP send timeout set: %ld\\r\\n", (long)timeout_status);
  HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
}"""

new_configure_udp = """static void NETWORK_ConfigureUdpSocket(int32_t sock_fd, uint32_t timeout_ms)
{
  struct mx_timeval timeout = {0};
  int32_t timeout_status;

  if ((wifi_obj == NULL) || (sock_fd < 0))
  {
    return;
  }

  timeout.tv_sec = (long)(timeout_ms / 1000U);
  timeout.tv_usec = (long)((timeout_ms % 1000U) * 1000U);

  timeout_status = MX_WIFI_Socket_setsockopt(wifi_obj, sock_fd, MX_SOL_SOCKET, MX_SO_RCVTIMEO,
                                             &timeout, sizeof(timeout));
  
  timeout_status = MX_WIFI_Socket_setsockopt(wifi_obj, sock_fd, MX_SOL_SOCKET, MX_SO_SNDTIMEO,
                                             &timeout, sizeof(timeout));
}"""

if old_configure_udp in content:
    content = content.replace(old_configure_udp, new_configure_udp)
    print("Patched NETWORK_ConfigureUdpSocket")
else:
    print("Failed to patch NETWORK_ConfigureUdpSocket")

# 2. Add UDP_SendNonCritical function and update COAP_SendBufferedBatch backoff
old_coap_send = """  dest_addr.sin_len = sizeof(dest_addr);
  dest_addr.sin_family = MX_AF_INET;
  dest_addr.sin_port = JETSON_PORT;
  dest_addr.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  for (attempt = 1U; attempt <= COAP_MAX_RETRY_COUNT; attempt++)
  {
    int32_t sent_result;
    int32_t recv_result;

    sent_result = MX_WIFI_Socket_sendto(wifi_obj, socket_id, coap_packet, packet_len,
                                        0, (struct mx_sockaddr *)&dest_addr, sizeof(dest_addr));

    if (sent_result != packet_len)
    {
      sprintf(uart_buf, "[COAP] ERROR: send failed on try %u/%u\\r\\n",
              attempt, COAP_MAX_RETRY_COUNT);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      continue;
    }

    from_addr_len = sizeof(from_addr);
    recv_result = MX_WIFI_Socket_recvfrom(wifi_obj, socket_id, ack_packet, sizeof(ack_packet),
                                          0, (struct mx_sockaddr *)&from_addr, &from_addr_len);

    if (COAP_IsAckValid(ack_packet, recv_result, message_id, token, sizeof(token)) != 0U)
    {
      sprintf(uart_buf, "[COAP] ACK received for MID=0x%04X, sample sent\\r\\n", message_id);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

      SENSOR_BufferDrop(1);  // Drop 1 sample
      coap_message_id++;
      return 1;  // Sent 1 sample
    }

    sprintf(uart_buf, "[COAP] No valid ACK on try %u/%u for MID=0x%04X\\r\\n",
            attempt, COAP_MAX_RETRY_COUNT, message_id);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
  }

  coap_message_id++;
  return -1;
}"""

new_coap_send = """  dest_addr.sin_len = sizeof(dest_addr);
  dest_addr.sin_family = MX_AF_INET;
  dest_addr.sin_port = JETSON_PORT;
  dest_addr.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);

  // RFC 7252 Binary Exponential Backoff
  uint32_t current_timeout_ms = COAP_ACK_TIMEOUT_MS; // Start at 2000ms

  for (attempt = 1U; attempt <= 4U; attempt++) // MAX_RETRANSMIT = 4
  {
    int32_t sent_result;
    int32_t recv_result;

    NETWORK_ConfigureUdpSocket(socket_id, current_timeout_ms);

    sent_result = MX_WIFI_Socket_sendto(wifi_obj, socket_id, coap_packet, packet_len,
                                        0, (struct mx_sockaddr *)&dest_addr, sizeof(dest_addr));

    if (sent_result != packet_len)
    {
      sprintf(uart_buf, "[COAP] ERROR: send failed on try %u/4\\r\\n", attempt);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
      current_timeout_ms *= 2;
      continue;
    }

    from_addr_len = sizeof(from_addr);
    recv_result = MX_WIFI_Socket_recvfrom(wifi_obj, socket_id, ack_packet, sizeof(ack_packet),
                                          0, (struct mx_sockaddr *)&from_addr, &from_addr_len);

    if (COAP_IsAckValid(ack_packet, recv_result, message_id, token, sizeof(token)) != 0U)
    {
      sprintf(uart_buf, "[COAP] ACK received for MID=0x%04X, sample sent\\r\\n", message_id);
      HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);

      SENSOR_BufferDrop(1);  // Drop 1 sample
      coap_message_id++;
      return 1;  // Sent 1 sample
    }

    sprintf(uart_buf, "[COAP] No valid ACK on try %u/4 for MID=0x%04X. Backoff: %lu ms\\r\\n",
            attempt, message_id, (unsigned long)current_timeout_ms);
    HAL_UART_Transmit(&huart1, (uint8_t *)uart_buf, strlen(uart_buf), 1000);
    
    current_timeout_ms *= 2; // Exponential backoff
  }

  coap_message_id++;
  return -1;
}

static void UDP_SendNonCritical(void)
{
  if (socket_id < 0 || wifi_station_ready == 0U || jetson_ip[0] == 0) return;
  
  NonCriticalPayload payload;
  payload.temp_c = 25.0f; // Placeholder
  payload.humidity_pct = 60.0f; // Placeholder
  
  struct mx_sockaddr_in dest_addr = {0};
  dest_addr.sin_len = sizeof(dest_addr);
  dest_addr.sin_family = MX_AF_INET;
  dest_addr.sin_port = JETSON_PORT;
  dest_addr.sin_addr.s_addr = (uint32_t)mx_aton_r(jetson_ip);
  
  MX_WIFI_Socket_sendto(wifi_obj, socket_id, (uint8_t*)&payload, sizeof(payload),
                        0, (struct mx_sockaddr *)&dest_addr, sizeof(dest_addr));
}"""

if old_coap_send in content:
    content = content.replace(old_coap_send, new_coap_send)
    print("Patched COAP_SendBufferedBatch and added UDP_SendNonCritical")
else:
    print("Failed to patch COAP_SendBufferedBatch")

# 3. Update NETWORK_ConfigureUdpSocket prototype
old_proto = "static void NETWORK_ConfigureUdpSocket(int32_t sock_fd);"
new_proto = "static void NETWORK_ConfigureUdpSocket(int32_t sock_fd, uint32_t timeout_ms);"
content = content.replace(old_proto, new_proto)

# Update the call in main()
old_call = "NETWORK_ConfigureUdpSocket(socket_id);"
new_call = "NETWORK_ConfigureUdpSocket(socket_id, COAP_ACK_TIMEOUT_MS);"
content = content.replace(old_call, new_call)

# 4. State Machine and Memory Protection
# Find the while(1) loop
old_while = """  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
      if ((wifi_obj != NULL) && (wifi_driver_initialized != 0U))
      {
        (void)MX_WIFI_IO_YIELD(wifi_obj, 1);
      }

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
				  sprintf(uart_buf, "[STATE] Movement detected! Switching to MOVING state.\\r\\n");
				  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
				  current_state = STATE_MOVING;
			  }
			  last_movement_tick = HAL_GetTick();
		  } else {
			  if (current_state == STATE_MOVING) {
				  if (HAL_GetTick() - last_movement_tick > NO_MOVEMENT_TIMEOUT_MS) {
					  sprintf(uart_buf, "[STATE] No movement for %d seconds. Switching to IDLE state.\\r\\n", NO_MOVEMENT_TIMEOUT_MS / 1000);
					  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
					  current_state = STATE_IDLE;
				  }
			  }
		  }

		  // -----------------------------------------------------------------
		  // PHASE 2: RAM BUFFERING (only if sensor read was successful)
		  // -----------------------------------------------------------------
		  SensorSample_t sample = {0};

		  sample.sequence_id = current_packet_num;
		  sample.relative_tick_count = HAL_GetTick();
		  sample.adc_power_mw = 150.0f; // Placeholder until ADC is configured in .ioc
		  sample.accel_x = vib_x;
		  sample.accel_y = vib_y;
		  sample.accel_z = vib_z;

		  SENSOR_BufferPush(&sample);
		  current_packet_num++; // Increment packet number for each sample
	  }
	  else
	  {
		  vib_x = 99.0f; vib_y = 99.0f; vib_z = 99.0f; // Hardware fault state
		  sprintf(uart_buf, "[SENSOR] ERROR: I2C read failed\\r\\n");
		  HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
	  }


	  // -----------------------------------------------------------------
	  // PHASE 3: COAP FLUSH WHEN BUFFER REACHES 70%
	  // -----------------------------------------------------------------
	  NETWORK_ProcessBufferedSamples();

	  // -----------------------------------------------------------------
	  // PHASE 4: STATE-BASED DELAY
	  // -----------------------------------------------------------------
	  if (current_state == STATE_MOVING) {
		  WIFI_DelayWithYield(MOVING_SAMPLE_DELAY_MS);
	  } else {
		  WIFI_DelayWithYield(IDLE_SAMPLE_DELAY_MS);
	  }
  }"""

new_while = """  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  static uint32_t continuous_movement_start_tick = 0;
  static uint8_t suspend_sampling = 0;

  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
      if ((wifi_obj != NULL) && (wifi_driver_initialized != 0U))
      {
        (void)MX_WIFI_IO_YIELD(wifi_obj, 1);
      }

      // Memory Protection Logic
      if (current_state == STATE_MOVING && sensor_buffer_count >= (uint16_t)(SENSOR_BUFFER_CAPACITY * 0.95f)) {
          if (!suspend_sampling) {
              suspend_sampling = 1;
              sprintf(uart_buf, "[BUFFER] CRITICAL: SRAM at 95%%! Suspending ADC/I2C sampling.\\r\\n");
              HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
          }
      }

      if (suspend_sampling) {
          if (current_state == STATE_IDLE && sensor_buffer_count == 0) {
              suspend_sampling = 0;
              sprintf(uart_buf, "[BUFFER] Buffer cleared. Resuming sampling.\\r\\n");
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
                      } else if (HAL_GetTick() - continuous_movement_start_tick >= 500) {
				          sprintf(uart_buf, "[STATE] Continuous movement for 500ms! Switching to MOVING state.\\r\\n");
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
					      sprintf(uart_buf, "[STATE] No movement for %d seconds. Switching to IDLE state.\\r\\n", NO_MOVEMENT_TIMEOUT_MS / 1000);
					      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
					      current_state = STATE_IDLE;
                          sensor_flush_requested = 1U; // Trigger CoAP transmission upon entering IDLE
				      }
			      }
		      }

		      // -----------------------------------------------------------------
		      // PHASE 2: RAM BUFFERING (only if sensor read was successful)
		      // -----------------------------------------------------------------
		      SensorSample_t sample = {0};

		      sample.sequence_id = current_packet_num;
		      sample.relative_tick_count = HAL_GetTick();
		      sample.adc_power_mw = 150.0f; // Placeholder until ADC is configured in .ioc
		      sample.accel_x = vib_x;
		      sample.accel_y = vib_y;
		      sample.accel_z = vib_z;

		      SENSOR_BufferPush(&sample);
		      current_packet_num++; // Increment packet number for each sample
	      }
	      else
	      {
		      vib_x = 99.0f; vib_y = 99.0f; vib_z = 99.0f; // Hardware fault state
		      sprintf(uart_buf, "[SENSOR] ERROR: I2C read failed\\r\\n");
		      HAL_UART_Transmit(&huart1, (uint8_t*)uart_buf, strlen(uart_buf), 1000);
	      }
      } // End of if (!suspend_sampling)


	  // -----------------------------------------------------------------
	  // PHASE 3: COAP FLUSH WHEN BUFFER REACHES 70%
	  // -----------------------------------------------------------------
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
  }"""

if old_while in content:
    content = content.replace(old_while, new_while)
    print("Patched State Machine and Main Loop")
else:
    print("Failed to patch State Machine and Main Loop")

with open(file_path, "w") as f:
    f.write(content)
