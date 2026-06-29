/* PLUDOS OTA firmware update — STM32 receiver (ADR-019). See ota.h for the design.
 * Self-contained transport (own CRC32, frame structs) mirroring client/ota_server.py
 * and tools/mock_ota_stm.py; flash mechanics adapted from the in-tree STM32U5 HAL. */

#include "ota.h"
#include "main.h"
#include "mx_wifi.h"
#include <string.h>
#include <stdio.h>

/* huart1 is the CubeMX-owned debug UART (declared in main.c). */
extern UART_HandleTypeDef huart1;

/* ARM Cortex-M33 is little-endian; sin_port is network byte order. No stdlib htons
 * on bare metal — swap at compile time (same macro main.c uses). */
#define OTA_HTONS(x) ((uint16_t)(((uint16_t)(x) >> 8U) | ((uint16_t)(x) << 8U)))

/* Kick the hand-rolled IWDG (same reload key main.c::IWDG_Kick uses; that function
 * is file-static, so we drive the register directly — no-op before IWDG_Arm). The
 * slow erase/program loops below must keep the ~16 s watchdog fed. */
#define OTA_IWDG_KICK()  do { IWDG->KR = 0x0000AAAAU; } while (0)

/* ---- Frame layouts — must match wire_protocol.md §2b / ota_server.py --------- */
#pragma pack(push, 1)
typedef struct {
  uint32_t magic; uint8_t type; uint8_t shuttle_id;
  uint32_t fw_version; uint32_t image_size;
  uint16_t total_chunks; uint16_t chunk_size; uint32_t image_crc32;
} OtaBegin_t;                                   /* 22 bytes */

typedef struct {
  uint32_t magic; uint8_t type; uint8_t shuttle_id;
  uint16_t chunk_seq; uint16_t total_chunks; uint16_t payload_len; uint32_t crc32;
} OtaChunkHdr_t;                                /* 16 bytes, payload follows */

typedef struct {
  uint32_t magic; uint8_t type; uint8_t shuttle_id; uint32_t current_fw;
} OtaRequest_t;                                 /* 10 bytes */

typedef struct {
  uint32_t magic; uint8_t type; uint8_t shuttle_id; uint32_t fw_version;
} OtaAck_t;                                     /* 10 bytes */
#pragma pack(pop)

/* ---- Static buffers (no malloc) --------------------------------------------- */
static uint8_t ota_rx[OTA_CHUNK_SIZE + sizeof(OtaChunkHdr_t) + 16U]; /* one datagram */
static uint8_t ota_bitmap[(OTA_MAX_CHUNKS + 7U) / 8U];               /* received-chunk bits */
static uint8_t ota_nak[8U + (4U * 64U)];                            /* NAK hdr + up to 64 ranges */

/* Manifest captured from OTA_BEGIN. */
typedef struct {
  uint8_t  valid;
  uint32_t fw_version;
  uint32_t image_size;
  uint16_t total_chunks;
  uint16_t chunk_size;
  uint32_t image_crc32;
} OtaManifest_t;

/* =============================================================================
 * CRC32 — zlib/IEEE (poly 0xEDB88320), reflected. Same algorithm as the Jetson
 * (zlib.crc32) and the firmware drain path, recomputed here to keep the module
 * self-contained (no cross-file static dependency).
 * ===========================================================================*/
static uint32_t ota_crc32(const uint8_t *data, uint32_t len)
{
  uint32_t crc = 0xFFFFFFFFUL;
  for (uint32_t i = 0U; i < len; i++)
  {
    crc ^= data[i];
    for (uint8_t b = 0U; b < 8U; b++)
    {
      uint32_t mask = (uint32_t)(-(int32_t)(crc & 1U));
      crc = (crc >> 1) ^ (0xEDB88320UL & mask);
    }
  }
  return crc ^ 0xFFFFFFFFUL;
}

/* Short UART log helper (its own buffer; main.c's uart_buf is file-static). */
static void ota_log(const char *msg)
{
  HAL_UART_Transmit(&huart1, (uint8_t *)msg, (uint16_t)strlen(msg), 1000);
}

/* =============================================================================
 * Bitmap helpers
 * ===========================================================================*/
static inline void bm_set(uint16_t i)  { ota_bitmap[i >> 3] |= (uint8_t)(1U << (i & 7U)); }
static inline uint8_t bm_get(uint16_t i){ return (uint8_t)((ota_bitmap[i >> 3] >> (i & 7U)) & 1U); }

/* =============================================================================
 * Flash: dual-bank confirm-or-revert state record
 * -----------------------------------------------------------------------------
 * Runtime writes ALWAYS target the high-half (inactive bank) last page, never the
 * active bank — this avoids the read-while-write hazard of erasing the bank the
 * CPU is fetching from. Reads scan both copies and pick the highest valid seq.
 * NOTE: inactive-bank selection and SWAP_BANK semantics need an RM0456 cross-check
 * with an ST-Link attached before trusting in the field (see ota.h).
 * ===========================================================================*/

/* Current SWAP_BANK option-bit value (OB_SWAP_BANK_ENABLE/DISABLE). */
static uint32_t ota_cur_swap(void)
{
  FLASH_OBProgramInitTypeDef ob = {0};
  HAL_FLASHEx_OBGetConfig(&ob);
  return (ob.USERConfig & OB_SWAP_BANK_ENABLE) ? OB_SWAP_BANK_ENABLE : OB_SWAP_BANK_DISABLE;
}

/* Physical bank currently mapped to the high half (= inactive / not executing). */
static uint32_t ota_inactive_bank(void)
{
  /* SWAP disabled: bank1 active (low), bank2 inactive (high). Enabled: reversed. */
  return (ota_cur_swap() == OB_SWAP_BANK_DISABLE) ? FLASH_BANK_2 : FLASH_BANK_1;
}

/* Validate a state copy at addr; return 1 and fill *out if magic+CRC are good. */
static uint8_t ota_read_copy(uint32_t addr, OtaState_t *out)
{
  memcpy(out, (const void *)addr, sizeof(OtaState_t));
  if (out->magic != OTA_STATE_MAGIC) { return 0U; }
  uint32_t want = ota_crc32((const uint8_t *)out, 6U * sizeof(uint32_t));
  return (want == out->crc32) ? 1U : 0U;
}

/* Read the newest valid record across both bank copies. Returns 0-state if none. */
static void ota_read_state(OtaState_t *out)
{
  OtaState_t a = {0}, b = {0};
  uint8_t va = ota_read_copy(OTA_STATE_ADDR_A, &a);
  uint8_t vb = ota_read_copy(OTA_STATE_ADDR_B, &b);

  memset(out, 0, sizeof(*out));
  out->state = OTA_STATE_NONE;
  if (va && (!vb || a.seq >= b.seq)) { *out = a; }
  else if (vb)                       { *out = b; }
}

/* Erase + program a fresh record into the high-half (inactive) bank's last page.
 * The seq is taken from the caller (already incremented past the newest). */
static void ota_write_state(uint32_t state, uint32_t target_fw,
                            uint32_t prev_swap, uint32_t trial_boots, uint32_t seq)
{
  OtaState_t rec = {0};
  rec.magic = OTA_STATE_MAGIC;
  rec.seq = seq;
  rec.state = state;
  rec.target_fw = target_fw;
  rec.prev_swap = prev_swap;
  rec.trial_boots = trial_boots;
  rec.crc32 = ota_crc32((const uint8_t *)&rec, 6U * sizeof(uint32_t));

  FLASH_EraseInitTypeDef er = {0};
  uint32_t page_err = 0U;
  er.TypeErase = FLASH_TYPEERASE_PAGES;
  er.Banks = ota_inactive_bank();
  er.Page = OTA_STATE_PAGE;
  er.NbPages = 1U;

  HAL_FLASH_Unlock();
  OTA_IWDG_KICK();
  if (HAL_FLASHEx_Erase(&er, &page_err) == HAL_OK)
  {
    /* 32-byte record = two 128-bit quad-words at the high-half last page. */
    (void)HAL_FLASH_Program(FLASH_TYPEPROGRAM_QUADWORD, OTA_STATE_ADDR_B,
                            (uint32_t)(uintptr_t)&rec);
    (void)HAL_FLASH_Program(FLASH_TYPEPROGRAM_QUADWORD, OTA_STATE_ADDR_B + 16U,
                            (uint32_t)(uintptr_t)((uint8_t *)&rec + 16U));
  }
  HAL_FLASH_Lock();
}

/* Flip SWAP_BANK to new_swap and launch — this resets the MCU (never returns). */
static void ota_swap_and_reset(uint32_t new_swap)
{
  FLASH_OBProgramInitTypeDef ob = {0};
  ob.OptionType = OPTIONBYTE_USER;
  ob.USERType = OB_USER_SWAP_BANK;
  ob.USERConfig = new_swap;

  HAL_FLASH_Unlock();
  HAL_FLASH_OB_Unlock();
  (void)HAL_FLASHEx_OBProgram(&ob);
  (void)HAL_FLASH_OB_Launch();   /* applies option bytes + system reset */
  /* not reached */
  HAL_FLASH_OB_Lock();
  HAL_FLASH_Lock();
}

/* =============================================================================
 * Public: confirm-or-revert (called at boot, before WiFi)
 * ===========================================================================*/
void Ota_BootCheck(void)
{
  OtaState_t st;
  ota_read_state(&st);

  if (st.state != OTA_STATE_TRIAL) { return; } /* NONE/CONFIRMED → normal boot */

  uint32_t n = st.trial_boots + 1U;
  if (n > OTA_TRIAL_LIMIT)
  {
    /* New image never confirmed within its boot budget → roll back the swap.
     * Clear the record to NONE FIRST so the reverted (old) image boots normally
     * instead of seeing TRIAL again and reverting in a loop. */
    ota_log("[OTA] trial budget exhausted — reverting bank swap\r\n");
    ota_write_state(OTA_STATE_NONE, 0U, 0U, 0U, st.seq + 1U);
    ota_swap_and_reset(st.prev_swap);   /* resets into the old image */
    return;                              /* not reached */
  }

  /* Still in the trial window: persist the incremented boot count before running
   * the app, so an app hang + IWDG reset advances the counter on the next boot. */
  ota_write_state(OTA_STATE_TRIAL, st.target_fw, st.prev_swap, n, st.seq + 1U);
  char line[96];
  snprintf(line, sizeof(line),
           "[OTA] now running NEW firmware v%lu (trial boot %lu/%u) — awaiting self-confirm\r\n",
           (unsigned long)st.target_fw, (unsigned long)n, (unsigned)OTA_TRIAL_LIMIT);
  ota_log(line);
}

/* Public: mark the running trial image good (call once the loop is healthy). */
void Ota_Confirm(void)
{
  OtaState_t st;
  ota_read_state(&st);
  if (st.state != OTA_STATE_TRIAL) { return; }

  ota_write_state(OTA_STATE_CONFIRMED, st.target_fw, st.prev_swap, 0U, st.seq + 1U);
  char line[96];
  snprintf(line, sizeof(line),
           "[OTA] new firmware v%lu confirmed good — now the known-good image\r\n",
           (unsigned long)st.target_fw);
  ota_log(line);
}

/* =============================================================================
 * Transport: receive one burst, staging good chunks into PSRAM
 * -----------------------------------------------------------------------------
 * Drains datagrams until a quiet window (recv timeout). For each CHUNK: validate
 * the per-chunk CRC32, copy the payload into the PSRAM stage at seq*chunk_size,
 * set the received bit. Captures the manifest from any OTA_BEGIN. Returns the
 * number of newly-staged chunks; *man is updated if a BEGIN was seen.
 * ===========================================================================*/
static uint16_t ota_recv_burst(MX_WIFIObject_t *wifi, int32_t sock, OtaManifest_t *man)
{
  uint16_t staged = 0U;
  int32_t  to_ms = OTA_RECV_QUIET_MS;
  struct mx_sockaddr_in from;
  uint32_t fromlen;
  (void)MX_WIFI_Socket_setsockopt(wifi, sock, MX_SOL_SOCKET, MX_SO_RCVTIMEO,
                                  &to_ms, sizeof(to_ms));
  for (;;)
  {
    /* MX_WIFI_Socket_recvfrom rejects NULL FromAddr/FromAddrLen with PARAM_ERROR
     * (returns immediately, never reaching the module) — must pass real pointers,
     * and re-init fromlen each call because the BSP overwrites it on return. */
    from.sin_len = 0;
    fromlen = sizeof(from);
    int32_t n = MX_WIFI_Socket_recvfrom(wifi, sock, ota_rx, (int32_t)sizeof(ota_rx),
                                        0, (struct mx_sockaddr *)&from, &fromlen);
    OTA_IWDG_KICK();
    if (n <= 0) { break; }                       /* quiet window → burst done */
    if (n < 6) { continue; }

    uint32_t magic; uint8_t type;
    memcpy(&magic, ota_rx, 4);
    type = ota_rx[4];
    if (magic != OTA_MAGIC) { continue; }

    if (type == OTA_TYPE_BEGIN && n >= (int32_t)sizeof(OtaBegin_t))
    {
      OtaBegin_t b;
      memcpy(&b, ota_rx, sizeof(b));
      man->valid = 1U;
      man->fw_version = b.fw_version;
      man->image_size = b.image_size;
      man->total_chunks = b.total_chunks;
      man->chunk_size = b.chunk_size;
      man->image_crc32 = b.image_crc32;
    }
    else if (type == OTA_TYPE_CHUNK && n >= (int32_t)sizeof(OtaChunkHdr_t))
    {
      OtaChunkHdr_t h;
      memcpy(&h, ota_rx, sizeof(h));
      uint16_t plen = h.payload_len;
      if ((int32_t)(sizeof(OtaChunkHdr_t) + plen) != n) { continue; }
      if (!man->valid || h.chunk_seq >= man->total_chunks) { continue; }
      const uint8_t *payload = ota_rx + sizeof(OtaChunkHdr_t);
      /* Per-chunk integrity gate (reject corrupt-on-wire). */
      if (ota_crc32(payload, plen) != h.crc32) { continue; }
      if (bm_get(h.chunk_seq)) { continue; }     /* duplicate */
      uint32_t off = (uint32_t)h.chunk_seq * man->chunk_size;
      if (off + plen > OTA_STAGE_BYTES) { continue; }
      memcpy((void *)(OTA_STAGE_ADDR + off), payload, plen);
      bm_set(h.chunk_seq);
      staged++;
    }
    /* END / unknown: ignored — completeness is decided by the bitmap. */
  }
  return staged;
}

/* Count staged chunks via the bitmap. */
static uint16_t ota_have_count(uint16_t total)
{
  uint16_t have = 0U;
  for (uint16_t i = 0U; i < total; i++) { if (bm_get(i)) { have++; } }
  return have;
}

/* Send a NAK listing the still-missing chunk ranges (RLE, one datagram). */
static void ota_send_nak(MX_WIFIObject_t *wifi, int32_t sock, struct mx_sockaddr_in *dst,
                         uint8_t sid, uint16_t total)
{
  uint16_t n_ranges = 0U;
  uint32_t pos = 8U;                              /* after magic,type,sid,n_ranges */
  uint16_t seq = 0U;
  while (seq < total && n_ranges < 64U)
  {
    if (bm_get(seq)) { seq++; continue; }
    uint16_t start = seq;
    while (seq < total && !bm_get(seq)) { seq++; }
    uint16_t end = (uint16_t)(seq - 1U);
    memcpy(ota_nak + pos, &start, 2); pos += 2U;
    memcpy(ota_nak + pos, &end, 2);   pos += 2U;
    n_ranges++;
  }
  uint32_t magic = OTA_MAGIC;
  memcpy(ota_nak, &magic, 4);
  ota_nak[4] = OTA_TYPE_NAK;
  ota_nak[5] = sid;
  memcpy(ota_nak + 6, &n_ranges, 2);
  for (uint8_t r = 0U; r < OTA_CTRL_REPEAT; r++)
  {
    (void)MX_WIFI_Socket_sendto(wifi, sock, ota_nak, (int32_t)pos, 0,
                                (struct mx_sockaddr *)dst, sizeof(*dst));
  }
}

/* =============================================================================
 * Flash commit: erase inactive bank, program image from PSRAM, read-back verify.
 * Returns 0 on success, -1 on any flash error.
 * ===========================================================================*/
static int8_t ota_flash_image(uint32_t image_size, uint32_t image_crc32)
{
  uint32_t pages = (image_size + FLASH_PAGE_SIZE - 1U) / FLASH_PAGE_SIZE;
  FLASH_EraseInitTypeDef er = {0};
  uint32_t page_err = 0U;

  HAL_FLASH_Unlock();

  /* Erase the inactive bank page-by-page (kick the dog — erase is slow). */
  er.TypeErase = FLASH_TYPEERASE_PAGES;
  er.Banks = ota_inactive_bank();
  er.NbPages = 1U;
  for (uint32_t p = 0U; p < pages; p++)
  {
    OTA_IWDG_KICK();
    er.Page = p;
    if (HAL_FLASHEx_Erase(&er, &page_err) != HAL_OK) { HAL_FLASH_Lock(); return -1; }
  }

  /* Pad the staged image up to a 16-byte (quad-word) boundary with 0xFF. */
  uint32_t pad = (16U - (image_size & 15U)) & 15U;
  if (pad) { memset((void *)(OTA_STAGE_ADDR + image_size), 0xFF, pad); }
  uint32_t prog_len = image_size + pad;

  /* Program quad-words from PSRAM staging into the inactive (high-half) bank. */
  for (uint32_t off = 0U; off < prog_len; off += 16U)
  {
    OTA_IWDG_KICK();
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_QUADWORD, OTA_INACTIVE_ADDR + off,
                          (uint32_t)(uintptr_t)(OTA_STAGE_ADDR + off)) != HAL_OK)
    {
      HAL_FLASH_Lock();
      return -1;
    }
  }
  HAL_FLASH_Lock();

  /* Read-back integrity gate on the *flashed* bytes (catches write errors). */
  if (ota_crc32((const uint8_t *)OTA_INACTIVE_ADDR, image_size) != image_crc32)
  {
    return -1;
  }
  return 0;
}

/* =============================================================================
 * Public: run one OTA session. See ota.h for the return contract.
 * ===========================================================================*/
int8_t Ota_TryUpdate(MX_WIFIObject_t *wifi, int32_t sock, const char *jetson_ip,
                     uint8_t shuttle_id, uint32_t cur_fw, uint32_t offered_fw)
{
  char line[96];
  if (offered_fw <= cur_fw) { return 0; }       /* nothing newer on offer */

  struct mx_sockaddr_in dst = {0};
  dst.sin_len = sizeof(dst);
  dst.sin_family = MX_AF_INET;
  dst.sin_port = OTA_HTONS(OTA_PORT);
  dst.sin_addr.s_addr = (uint32_t)mx_aton_r((char *)jetson_ip);

  memset(ota_bitmap, 0, sizeof(ota_bitmap));
  OtaManifest_t man = {0};

  snprintf(line, sizeof(line), "[OTA] update offered: v%lu > v%lu — requesting\r\n",
           (unsigned long)offered_fw, (unsigned long)cur_fw);
  ota_log(line);

  /* Trigger the session: ONE REQUEST per attempt → ONE server serve task. Repeating
   * the REQUEST back-to-back would spawn concurrent serves whose interleaved chunk
   * floods overrun the EMW3080 RX buffer and lose the BEGIN (see ota.h OTA_REQ_ATTEMPTS).
   * The server repeats the BEGIN x3 inside one serve, so a single REQUEST is robust;
   * if the burst still yields no BEGIN, retry the whole request, bounded. */
  OtaRequest_t req = { OTA_MAGIC, OTA_TYPE_REQUEST, shuttle_id, cur_fw };
  for (uint8_t attempt = 0U; (attempt < OTA_REQ_ATTEMPTS) && (man.valid == 0U); attempt++)
  {
    (void)MX_WIFI_Socket_sendto(wifi, sock, (uint8_t *)&req, sizeof(req), 0,
                                (struct mx_sockaddr *)&dst, sizeof(dst));
    (void)ota_recv_burst(wifi, sock, &man);
  }
  if (!man.valid)
  {
    ota_log("[OTA] no OTA_BEGIN — server offers nothing, aborting\r\n");
    return -1;
  }
  if (man.image_size > OTA_STAGE_BYTES || man.total_chunks > OTA_MAX_CHUNKS)
  {
    ota_log("[OTA] image too large for staging region — aborting\r\n");
    return -1;
  }

  snprintf(line, sizeof(line), "[OTA] receiving image v%lu: %u chunks, %lu bytes (have %u after burst)\r\n",
           (unsigned long)man.fw_version, (unsigned)man.total_chunks,
           (unsigned long)man.image_size, (unsigned)ota_have_count(man.total_chunks));
  ota_log(line);

  /* NAK loop: ask only for the gaps, bounded by OTA_MAX_ROUNDS. */
  uint16_t rounds = 0U;
  while (ota_have_count(man.total_chunks) < man.total_chunks && rounds < OTA_MAX_ROUNDS)
  {
    rounds++;
    snprintf(line, sizeof(line), "[OTA] round %u: have %u/%u chunks — NAKing gaps\r\n",
             (unsigned)rounds, (unsigned)ota_have_count(man.total_chunks), (unsigned)man.total_chunks);
    ota_log(line);
    ota_send_nak(wifi, sock, &dst, shuttle_id, man.total_chunks);
    (void)ota_recv_burst(wifi, sock, &man);
  }

  if (ota_have_count(man.total_chunks) < man.total_chunks)
  {
    snprintf(line, sizeof(line), "[OTA] incomplete after %u rounds — aborting (old fw kept)\r\n",
             (unsigned)rounds);
    ota_log(line);
    return -1;
  }

  /* Whole-image CRC32 gate — the integrity decision. Flash is untouched on fail. */
  if (ota_crc32((const uint8_t *)OTA_STAGE_ADDR, man.image_size) != man.image_crc32)
  {
    ota_log("[OTA] whole-image CRC mismatch — NOT flashing, old fw kept\r\n");
    return -1;
  }
  ota_log("[OTA] image complete + CRC verified\r\n");

  /* Acknowledge before committing (mirrors the bench mock's ordering). */
  OtaAck_t ack = { OTA_MAGIC, OTA_TYPE_ACK, shuttle_id, man.fw_version };
  (void)MX_WIFI_Socket_sendto(wifi, sock, (uint8_t *)&ack, sizeof(ack), 0,
                              (struct mx_sockaddr *)&dst, sizeof(dst));

  /* Commit: flash the inactive bank, verify, record TRIAL, swap + reset. */
  snprintf(line, sizeof(line), "[OTA] installing: writing %lu bytes to inactive flash bank...\r\n",
           (unsigned long)man.image_size);
  ota_log(line);
  if (ota_flash_image(man.image_size, man.image_crc32) != 0)
  {
    ota_log("[OTA] flash/verify failed — NOT swapping, old fw kept\r\n");
    return -1;
  }

  uint32_t cur_swap = ota_cur_swap();
  uint32_t new_swap = (cur_swap == OB_SWAP_BANK_DISABLE) ? OB_SWAP_BANK_ENABLE
                                                         : OB_SWAP_BANK_DISABLE;
  OtaState_t st;
  ota_read_state(&st);
  ota_write_state(OTA_STATE_TRIAL, man.fw_version, cur_swap, 0U, st.seq + 1U);

  ota_log("[OTA] flashed inactive bank — swapping + resetting into new image\r\n");
  ota_swap_and_reset(new_swap);   /* resets the MCU — does not return */
  return 1;
}
