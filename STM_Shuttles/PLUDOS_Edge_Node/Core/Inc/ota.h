#ifndef OTA_H
#define OTA_H

#include "stm32u5xx_hal.h"
#include "mx_wifi.h"
#include "psram.h"
#include <stdint.h>

/* =============================================================================
 * PLUDOS OTA firmware update — STM32 receiver side (ADR-019 test/bench tier)
 * -----------------------------------------------------------------------------
 * Mirror of the Jetson client/ota_server.py + wire_protocol.md §2b. The STM is
 * the ARQ authority: it requests an image, stages chunks in PSRAM, NAKs the gaps
 * until the received-bitmap is full, runs a whole-image CRC32 gate, and only then
 * flashes the *inactive* bank, read-back-verifies, swaps banks (SWAP_BANK option
 * bit) and resets into the new image. A confirm-or-revert record in flash auto-
 * reverts the swap if the new image fails to self-confirm within OTA_TRIAL_LIMIT
 * boots. Security (signing/encryption) is OUT of scope here — trusted bench LAN.
 *
 * NOTE (verify before flashing real hardware): the inactive-bank addressing and
 * SWAP_BANK semantics below follow the in-tree HAL macros and the active-bank-low
 * remap model. Confirm against RM0456 (Flash chapter, SWAP_BANK / bank remap)
 * with an ST-Link attached before trusting the bank-swap path in the field.
 * ===========================================================================*/

/* ---- Wire protocol (must match client/ota_server.py exactly) -------------- */
#define OTA_PORT            5685U
#define OTA_MAGIC           0x4F444C50UL   /* "PLDO" little-endian (wire bytes 50 4C 44 4F) */
#define OTA_TYPE_BEGIN      1U
#define OTA_TYPE_CHUNK      2U
#define OTA_TYPE_END        3U
#define OTA_TYPE_REQUEST    4U
#define OTA_TYPE_NAK        5U
#define OTA_TYPE_ACK        6U             /* OTA_ACK_COMPLETE */

#define OTA_CHUNK_SIZE      1400U          /* nominal payload; last chunk may be shorter */
#define OTA_MAX_ROUNDS      8U             /* NAK rounds before giving up */
#define OTA_RECV_QUIET_MS   400            /* per-recv timeout that ends a burst */
#define OTA_CTRL_REPEAT     3U             /* resend NAK control frames (NAK is small, no flood) */
/* REQUEST attempts: sent ONE-AT-A-TIME, each waiting a full recv burst for the BEGIN.
 * A REQUEST must NOT be repeated back-to-back: every REQUEST makes the server launch
 * an independent serve task, and N concurrent serves interleave their chunk bursts to
 * N times the paced rate, overrunning the EMW3080's small RX buffer so the BEGIN is
 * lost before the first recvfrom. One REQUEST = one serve (BEGIN is already repeated
 * x3 within that serve); retry the whole request only if no BEGIN arrives. */
#define OTA_REQ_ATTEMPTS    4U

/* ---- PSRAM staging budget ------------------------------------------------- */
/* Image is staged in the OTA carve-out (psram.h) before the CRC gate, so a failed
 * transfer can never half-write a bank. Max chunks bounds the static bitmap. */
#define OTA_STAGE_ADDR      PSRAM_OTA_STAGE_ADDR
#define OTA_STAGE_BYTES     PSRAM_OTA_STAGE_BYTES
#define OTA_MAX_CHUNKS      ((OTA_STAGE_BYTES / OTA_CHUNK_SIZE) + 1U)

/* ---- Dual-bank flash layout (STM32U585 2 MB, hardwired dual-bank) --------- */
/* Active bank is remapped to 0x08000000; the inactive bank is always addressable
 * at the high half. We flash the inactive (high-half) bank, then SWAP_BANK + reset
 * so the same .bin boots from either bank (linker length = one bank, no relink). */
#define OTA_BANK_SIZE       (FLASH_SIZE >> 1)             /* 1 MB per bank */
#define OTA_INACTIVE_ADDR   (FLASH_BASE + OTA_BANK_SIZE)  /* high half = inactive bank */

/* ---- Confirm-or-revert state record (anti-brick) -------------------------- */
/* Stored in the last 8 KB flash page of BOTH banks (the linker reserves it). On
 * boot we read both, pick the highest seq with a valid CRC — robust against a
 * torn write and against the swap relabelling which physical page is "active". */
#define OTA_STATE_MAGIC     0x4F544153UL   /* "OTAS" */
#define OTA_STATE_ADDR_A    (FLASH_BASE + OTA_BANK_SIZE - FLASH_PAGE_SIZE)   /* 0x080FE000 */
#define OTA_STATE_ADDR_B    (FLASH_BASE + FLASH_SIZE  - FLASH_PAGE_SIZE)     /* 0x081FE000 */
#define OTA_STATE_PAGE      (FLASH_PAGE_NB - 1U)          /* last page index of a bank */

#define OTA_STATE_NONE      0U
#define OTA_STATE_TRIAL     1U
#define OTA_STATE_CONFIRMED 2U
#define OTA_TRIAL_LIMIT     3U              /* boots a new image gets to self-confirm */
#define OTA_CONFIRM_UPTIME_MS 30000U        /* uptime a trial image must survive to self-confirm */

/* 32 bytes = 2 flash quad-words (U5 programs 128-bit at a time). crc32 covers the
 * first 6 words; _pad keeps the struct quad-word-sized. */
typedef struct
{
  uint32_t magic;        /* OTA_STATE_MAGIC when valid */
  uint32_t seq;          /* monotonic; higher = newer across the two copies */
  uint32_t state;        /* OTA_STATE_NONE / TRIAL / CONFIRMED */
  uint32_t target_fw;    /* fw version being trialed */
  uint32_t prev_swap;    /* OB_SWAP_BANK_* before the swap (revert target) */
  uint32_t trial_boots;  /* boots since the swap */
  uint32_t crc32;        /* CRC32 over the 6 words above */
  uint32_t _pad;         /* pad to 32 B (2 quad-words) */
} OtaState_t;

/* ---- Public API ----------------------------------------------------------- */

/* Confirm-or-revert check. Call FIRST in USER CODE BEGIN 2, before WiFi/drain so
 * it runs on essentially every boot. If a TRIAL image has used up its boot budget
 * without confirming, reverts the bank swap and resets into the old image. */
void Ota_BootCheck(void);

/* Mark the running TRIAL image good once the main loop is healthy (e.g. after one
 * clean IDLE cycle). No-op if the current state is not TRIAL. */
void Ota_Confirm(void);

/* Run one OTA session over an already-open UDP socket (IDLE window, radio on).
 * Returns 0 if no update was needed, 1 if it is about to reset into a new image
 * (does not return on success), -1 on a transfer/flash failure (old image kept).
 * offered_fw comes from the beacon ":fw=" token; only runs if offered_fw > cur_fw. */
int8_t Ota_TryUpdate(MX_WIFIObject_t *wifi, int32_t sock, const char *jetson_ip,
                     uint8_t shuttle_id, uint32_t cur_fw, uint32_t offered_fw);

#endif /* OTA_H */
