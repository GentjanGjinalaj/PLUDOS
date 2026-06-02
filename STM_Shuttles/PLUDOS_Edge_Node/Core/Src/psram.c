/* APS6408 Octal PSRAM bring-up for OCTOSPI1 (memory-mapped mode).
 *
 * CubeMX (MX_OCTOSPI1_Init) already configures the OCTOSPI peripheral, GPIO,
 * clock and a zeroed delay block on hospi1. What remains is device-side: tune
 * the delay block, program the APS6408 mode registers, and enter memory-mapped
 * mode. All command structures, opcodes, mode-register values and dummy-cycle
 * counts below are copied verbatim from the STM32CubeU5 v1.8.0 BSP for this
 * exact board so we do not invent any register values:
 *   Drivers/BSP/Components/aps6408/aps6408.c      (commands, opcodes)
 *   Drivers/BSP/Components/aps6408/aps6408_conf.h (dummy cycles 5R/4W)
 *   Drivers/BSP/B-U585I-IOT02A/b_u585i_iot02a_ospi.c
 *       (MR0=0x24, MR8=0x0B, DLYB calibration, MMP timeout 0x34)
 */

#include "psram.h"

/* OCTOSPI1 handle owned by CubeMX (declared non-static in main.c). */
extern OSPI_HandleTypeDef hospi1;

/* --- APS6408 opcodes (from aps6408.h) ------------------------------------ */
#define APS6408_READ_CMD        0x00U  /* synchronous read  */
#define APS6408_WRITE_CMD       0x80U  /* synchronous write */
#define APS6408_WRITE_REG_CMD   0xC0U  /* mode-register write */

/* --- APS6408 mode-register settings (from b_u585i_iot02a_ospi.c) ---------- */
#define APS6408_MR0_VALUE       0x24U  /* read latency code 4, full drive strength */
#define APS6408_MR8_VALUE       0x0BU  /* 2K wrap burst, row-boundary-crossing enable */

/* --- Memory-mapped dummy cycles (from aps6408_conf.h) -------------------- */
#define PSRAM_DUMMY_READ        5U
#define PSRAM_DUMMY_WRITE       4U

/* Self-test parameters: dense block for bit integrity, page stride for wiring. */
#define PSRAM_TEST_BLOCK_BYTES  (128U * 1024U)  /* contiguous data-integrity block */
#define PSRAM_TEST_PAGE_STRIDE  4096U           /* address-fault sweep granularity */

/* Tune the OCTOSPI1 delay block. Mirrors b_u585i_iot02a_ospi.c::OSPI_DLYB_Enable:
 * read the calibrated clock period, scale PhaseSel by 1/4 (ST empiric value),
 * apply and verify. Needed for reliable DTR read sampling at this prescaler. */
static int8_t psram_dlyb_enable(void)
{
  HAL_OSPI_DLYB_CfgTypeDef cfg, cfg_check;

  if (HAL_OSPI_DLYB_GetClockPeriod(&hospi1, &cfg) != HAL_OK)
  {
    return -1;
  }

  cfg.PhaseSel /= 4U;          /* empiric scaling from ST BSP */
  cfg_check = cfg;

  if (HAL_OSPI_DLYB_SetConfig(&hospi1, &cfg) != HAL_OK)
  {
    return -1;
  }
  if (HAL_OSPI_DLYB_GetConfig(&hospi1, &cfg) != HAL_OK)
  {
    return -1;
  }
  if ((cfg.PhaseSel != cfg_check.PhaseSel) || (cfg.Units != cfg_check.Units))
  {
    return -1;  /* delay block did not latch the requested setting */
  }
  return 0;
}

/* Write one APS6408 mode register. Mirrors aps6408.c::APS6408_WriteReg:
 * 8-line instruction/address/data, 32-bit DTR address, 2-byte data transfer
 * (OPI minimum), DQS disabled, no dummy cycles. */
static int8_t psram_write_reg(uint32_t reg_addr, uint8_t value)
{
  OSPI_RegularCmdTypeDef cmd = {0};
  uint8_t data[2] = { value, value };  /* duplicate to satisfy 2-byte DTR write */

  cmd.OperationType      = HAL_OSPI_OPTYPE_COMMON_CFG;
  cmd.InstructionMode    = HAL_OSPI_INSTRUCTION_8_LINES;
  cmd.InstructionSize    = HAL_OSPI_INSTRUCTION_8_BITS;
  cmd.InstructionDtrMode = HAL_OSPI_INSTRUCTION_DTR_DISABLE;
  cmd.Instruction        = APS6408_WRITE_REG_CMD;
  cmd.AddressMode        = HAL_OSPI_ADDRESS_8_LINES;
  cmd.AddressSize        = HAL_OSPI_ADDRESS_32_BITS;
  cmd.AddressDtrMode     = HAL_OSPI_ADDRESS_DTR_ENABLE;
  cmd.Address            = reg_addr;
  cmd.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  cmd.DataMode           = HAL_OSPI_DATA_8_LINES;
  cmd.DataDtrMode        = HAL_OSPI_DATA_DTR_ENABLE;
  cmd.NbData             = 2;
  cmd.DummyCycles        = 0;
  cmd.DQSMode            = HAL_OSPI_DQS_DISABLE;
  cmd.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;

  if (HAL_OSPI_Command(&hospi1, &cmd, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    return -1;
  }
  if (HAL_OSPI_Transmit(&hospi1, data, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    return -1;
  }
  return 0;
}

/* Program both read and write command configs and activate memory-mapped mode.
 * Mirrors aps6408.c::APS6408_EnableMemoryMappedMode (linear burst type = 1):
 * write uses APS6408_WRITE_CMD with 4 dummy cycles, read uses APS6408_READ_CMD
 * with 5 dummy cycles, both 8-line DTR with DQS. */
static int8_t psram_enable_memory_mapped(void)
{
  OSPI_RegularCmdTypeDef cmd = {0};
  OSPI_MemoryMappedTypeDef mmap = {0};

  /* Write command configuration */
  cmd.OperationType      = HAL_OSPI_OPTYPE_WRITE_CFG;
  cmd.FlashId            = HAL_OSPI_FLASH_ID_1;
  cmd.InstructionMode    = HAL_OSPI_INSTRUCTION_8_LINES;
  cmd.InstructionSize    = HAL_OSPI_INSTRUCTION_8_BITS;
  cmd.InstructionDtrMode = HAL_OSPI_INSTRUCTION_DTR_DISABLE;
  cmd.Instruction        = APS6408_WRITE_CMD;
  cmd.AddressMode        = HAL_OSPI_ADDRESS_8_LINES;
  cmd.AddressSize        = HAL_OSPI_ADDRESS_32_BITS;
  cmd.AddressDtrMode     = HAL_OSPI_ADDRESS_DTR_ENABLE;
  cmd.AlternateBytesMode = HAL_OSPI_ALTERNATE_BYTES_NONE;
  cmd.DataMode           = HAL_OSPI_DATA_8_LINES;
  cmd.DataDtrMode        = HAL_OSPI_DATA_DTR_ENABLE;
  cmd.DummyCycles        = PSRAM_DUMMY_WRITE;
  cmd.DQSMode            = HAL_OSPI_DQS_ENABLE;
  cmd.SIOOMode           = HAL_OSPI_SIOO_INST_EVERY_CMD;

  if (HAL_OSPI_Command(&hospi1, &cmd, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    return -1;
  }

  /* Read command configuration (same struct, only opcode/dummy/type differ) */
  cmd.OperationType = HAL_OSPI_OPTYPE_READ_CFG;
  cmd.Instruction   = APS6408_READ_CMD;
  cmd.DummyCycles   = PSRAM_DUMMY_READ;

  if (HAL_OSPI_Command(&hospi1, &cmd, HAL_OSPI_TIMEOUT_DEFAULT_VALUE) != HAL_OK)
  {
    return -1;
  }

  /* Activate memory-mapped mode (timeout counter value 0x34 from ST BSP) */
  mmap.TimeOutActivation = HAL_OSPI_TIMEOUT_COUNTER_ENABLE;
  mmap.TimeOutPeriod     = 0x34U;

  if (HAL_OSPI_MemoryMapped(&hospi1, &mmap) != HAL_OK)
  {
    return -1;
  }
  return 0;
}

/* Configure APS6408 mode registers and switch OCTOSPI1 to memory-mapped mode. */
int8_t PSRAM_Init(void)
{
  if (psram_dlyb_enable() != 0)
  {
    return -1;
  }
  /* MR0: read latency / drive strength; MR8: burst length / row-boundary crossing */
  if (psram_write_reg(0x00U, APS6408_MR0_VALUE) != 0)
  {
    return -1;
  }
  if (psram_write_reg(0x08U, APS6408_MR8_VALUE) != 0)
  {
    return -1;
  }
  if (psram_enable_memory_mapped() != 0)
  {
    return -1;
  }
  return 0;
}

/* Write/read-back self-test of the memory-mapped region. */
int8_t PSRAM_SelfTest(void)
{
  volatile uint32_t *ram = (volatile uint32_t *)PSRAM_BASE_ADDR;
  uint32_t i;

  /* 1. Dense data-integrity block: address-derived pattern catches stuck bits. */
  for (i = 0; i < (PSRAM_TEST_BLOCK_BYTES / 4U); i++)
  {
    ram[i] = i ^ 0xA5A5A5A5U;
  }
  for (i = 0; i < (PSRAM_TEST_BLOCK_BYTES / 4U); i++)
  {
    if (ram[i] != (i ^ 0xA5A5A5A5U))
    {
      return -1;
    }
  }

  /* 2. Address-wiring sweep: one unique word per page across the full 8 MB.
   *    A shorted/floating address line aliases pages and fails read-back. */
  for (i = 0; i < PSRAM_SIZE_BYTES; i += PSRAM_TEST_PAGE_STRIDE)
  {
    ram[i / 4U] = i;  /* word index = byte offset / 4 */
  }
  for (i = 0; i < PSRAM_SIZE_BYTES; i += PSRAM_TEST_PAGE_STRIDE)
  {
    if (ram[i / 4U] != i)
    {
      return -1;
    }
  }

  return 0;
}
