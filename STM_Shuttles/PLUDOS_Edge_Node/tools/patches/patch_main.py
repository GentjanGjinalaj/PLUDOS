import sys
import os

# Construct path relative to this script's location
script_dir = os.path.dirname(os.path.abspath(__file__))
main_c_path = os.path.join(script_dir, '../../Core/Src/main.c')

with open(main_c_path, "r") as f:
    content = f.read()

# 1. Update typedefs
old_typedef = """typedef struct
{
  uint16_t packet_num;
  uint32_t tick_ms;
  float vib_x;
  float vib_y;
  float vib_z;
} SensorSample_t;"""

new_typedef = """typedef struct
{
  uint16_t sequence_id;
  uint32_t relative_tick_count;
  float accel_x;
  float accel_y;
  float accel_z;
  float adc_power_mw;
} SensorSample_t;

// 1. Critical Payload (CoAP)
#pragma pack(push, 1)
typedef struct {
  char shuttle_id[12];
  uint16_t sequence_id;
  uint32_t relative_tick_count;
  uint8_t mission_active;
  float ram_usage_pct;
  float accel_x;
  float accel_y;
  float accel_z;
  float adc_power_mw;
} CriticalPayload;
#pragma pack(pop)

// 2. Non-Critical Payload (UDP)
#pragma pack(push, 1)
typedef struct {
  float temp_c;
  float humidity_pct;
} NonCriticalPayload;
#pragma pack(pop)"""

if old_typedef in content:
    content = content.replace(old_typedef, new_typedef)
else:
    print("Warning: old_typedef not found")

# 2. Update SENSOR_BuildBatchPayload
old_build = """  // Build new JSON structure matching Python server expectations
  written = snprintf(payload, (size_t)payload_size,
                     "{\\"header\\":{\\"shuttle_id\\":\\"STM32-Alpha\\",\\"packet_num\\":%u},"
                     "\\"status\\":{\\"mission_active\\":%s,\\"ram_usage_pct\\":%.1f},"
                     "\\"energy\\":{\\"power_mw\\":%.2f},"
                     "\\"sensors\\":{\\"vib_x\\":%.6f,\\"vib_y\\":%.6f,\\"vib_z\\":%.6f,\\"temp_c\\":%.2f,\\"humidity_pct\\":%.2f}}",
                     sample->packet_num,
                     (sensor_buffer_count < 179) ? "true" : "false",  // mission_active based on buffer
                     (float)sensor_buffer_count / 256.0f * 100.0f,    // ram_usage_pct
                     150.0f,  // placeholder power_mw
                     (double)sample->vib_x,
                     (double)sample->vib_y,
                     (double)sample->vib_z,
                     25.0f,   // placeholder temp_c
                     60.0f);  // placeholder humidity_pct

  if ((written < 0) || ((uint32_t)written >= payload_size))
  {
    return -1;
  }

  offset = (uint32_t)written;
  *samples_built = actual_samples;

  return (int32_t)offset;"""

new_build = """  CriticalPayload coap_data = {0};
  strncpy(coap_data.shuttle_id, "STM32-Alpha", sizeof(coap_data.shuttle_id) - 1);
  coap_data.sequence_id = sample->sequence_id;
  coap_data.relative_tick_count = sample->relative_tick_count;
  coap_data.mission_active = (sensor_buffer_count < 179) ? 1 : 0;
  coap_data.ram_usage_pct = (float)sensor_buffer_count / 256.0f * 100.0f;
  coap_data.accel_x = sample->accel_x;
  coap_data.accel_y = sample->accel_y;
  coap_data.accel_z = sample->accel_z;
  coap_data.adc_power_mw = sample->adc_power_mw;

  memcpy(payload, &coap_data, sizeof(CriticalPayload));

  offset = (uint32_t)sizeof(CriticalPayload);
  *samples_built = actual_samples;

  return (int32_t)offset;"""

if old_build in content:
    content = content.replace(old_build, new_build)
else:
    print("Warning: old_build not found")

# 3. Content format change from JSON to application/octet-stream
old_format = """  packet[pos++] = 0x11U;  // Option delta=1 (Content-Format), length=1
  packet[pos++] = 50U;    // application/json"""

new_format = """  packet[pos++] = 0x11U;  // Option delta=1 (Content-Format), length=1
  packet[pos++] = 42U;    // application/octet-stream"""

if old_format in content:
    content = content.replace(old_format, new_format)
else:
    print("Warning: old_format not found")

# 4. Phase 2 update
old_phase2 = """		  // -----------------------------------------------------------------
		  // PHASE 2: RAM BUFFERING (only if sensor read was successful)
		  // -----------------------------------------------------------------
		  SensorSample_t sample = {0};

		  sample.packet_num = current_packet_num;
		  sample.tick_ms = HAL_GetTick();
		  sample.vib_x = vib_x;
		  sample.vib_y = vib_y;
		  sample.vib_z = vib_z;"""

new_phase2 = """		  // -----------------------------------------------------------------
		  // PHASE 2: RAM BUFFERING (only if sensor read was successful)
		  // -----------------------------------------------------------------
		  SensorSample_t sample = {0};

		  sample.sequence_id = current_packet_num;
		  sample.relative_tick_count = HAL_GetTick();
		  sample.adc_power_mw = 150.0f; // Placeholder until ADC is configured in .ioc
		  sample.accel_x = vib_x;
		  sample.accel_y = vib_y;
		  sample.accel_z = vib_z;"""

if old_phase2 in content:
    content = content.replace(old_phase2, new_phase2)
else:
    print("Warning: old_phase2 not found")

with open(main_c_path, "w") as f:
    f.write(content)

