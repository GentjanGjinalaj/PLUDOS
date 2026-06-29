# Firmware update (OTA) — operator guide

How a new STM32U585 shuttle firmware build gets from your PC to a deployed
shuttle over WiFi, with no ST-Link trip. This is the **ADR-019 test/bench
tier**: integrity-checked and anti-brick, but **no security** (trusted LAN
only). Wire-level frame layouts live in `@docs/wire_protocol.md §2b`; the
design rationale is in `@docs/decisions.md` (ADR-019).

---

## The short version

```
build .bin (PC)  ──scp──►  Jetson client/firmware/  ──UDP 5685──►  STM32
                            (firmware.bin + manifest.json)          (pulls on
                                                                     next mission)
```

## The path, schematic

```
  YOUR PC                      JETSON GATEWAY                     STM32 SHUTTLE
  ───────                      ──────────────                     ─────────────
  bump FW_VERSION
  make + objcopy
     │ firmware.bin
     ▼
   scp ───────────────────►  client/firmware/
                             firmware.bin + manifest.json
                                   │ (auto-reload on mtime)
                                   ▼
                             ota_server caches image,
                             beacon advertises :fw=N ──────────►  mission ends,
                                                                  radio on, reads
                                                                  beacon: N > mine
                                   ◄────────────────────────────  OTA_REQUEST
                             OTA_BEGIN (manifest) ─────────────►  stage in PSRAM,
                             OTA_CHUNK × all ──────────────────►  track bitmap
                             OTA_END ──────────────────────────►
                                   ◄──────── OTA_NAK (gaps) ─────  scan bitmap
                             resend only missing ──────────────►  (repeat, bounded)
                                                                  ┌──────────────┐
                                                                  │ whole-image  │
                                                                  │ CRC32 GATE   │
                                                                  │ fail→abort,  │
                                                                  │ old fw kept  │
                                                                  └──────┬───────┘
                                   ◄──── OTA_ACK_COMPLETE ────────       │ pass
                                                                         ▼
                                                                  STM flashes the
                                                                  INACTIVE bank,
                                                                  read-back verify,
                                                                  SWAP_BANK + reset
                                                                         │
                                                                         ▼
                                                                  boots NEW image
                                                                  on TRIAL → self-
                                                                  confirms, or auto-
                                                                  reverts after N boots
```

**Does the STM install it itself? Yes.** The gateway is a dumb chunk server — it
only holds the image and resends whatever is asked for. The STM is the authority:
it decides to request, stages the image in its own PSRAM, runs the integrity gate,
**writes its own flash** (the inactive bank), swaps banks, and reboots itself into
the new image. Nothing is pushed to the board; the board pulls and self-flashes.
The host never touches STM flash (no ST-Link in the loop).

1. Bump `FW_VERSION`, build, export a raw `.bin`.
2. Drop `firmware.bin` + `manifest.json` into the Jetson's `client/firmware/`.
3. The `data-engine` container auto-reloads the image and starts advertising
   the new version in its beacon. **No container restart needed.**
4. The next time a shuttle finishes a mission and powers its radio on, it sees
   the offer, pulls the image, verifies it, flashes the spare bank, and reboots
   into the new firmware. If the new image misbehaves it auto-reverts.

You do nothing on the shuttle. It updates itself on its own schedule.

---

## Step 1 — build the image on your PC

The firmware version is a single compile-time constant. Bump it so shuttles
can tell the new build apart from what they're running.

`STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c`:

```c
#define FW_VERSION              2U      /* was 1U */
```

Build and extract a raw binary (the OTA path flashes a raw image, not the ELF):

```bash
cd STM_Shuttles/PLUDOS_Edge_Node/Debug
make all -j4
arm-none-eabi-objcopy -O binary PLUDOS_Edge_Node.elf firmware.bin
```

`firmware.bin` must fit in **one 1 MB bank** — the dual-bank scheme flashes the
image into the inactive bank and swaps. The linker already caps the image at
one bank (`STM32U585AIIXQ_FLASH.ld`), so a normal build is well within budget
(~104 KB today).

> The server computes the whole-image CRC32 itself from the `.bin`. You don't
> need to compute or record it by hand — but the same CRC is the integrity gate
> the STM enforces before it touches flash, so the byte-exact `.bin` you ship is
> the byte-exact image that boots.

## Step 2 — stage the image on the Jetson

The image lives in the gateway's firmware store, bind-mounted into the
`data-engine` container at `/app/firmware` (`OTA_FIRMWARE_DIR`). Two files:

- `firmware.bin` — the raw image from Step 1.
- `manifest.json` — one field: the version you just compiled.

  ```json
  { "fw_version": 2 }
  ```

Copy both up (the `.bin` is gitignored — see "Why scp" below):

```bash
# firmware dir on the Jetson host: ~/PLUDOS/client/firmware/
scp firmware.bin  warehouse1@<jetson>:~/PLUDOS/client/firmware/firmware.bin
scp manifest.json warehouse1@<jetson>:~/PLUDOS/client/firmware/manifest.json
```

The loader keys off file mtime, so overwriting the files is enough — the running
container reloads on the next read. Confirm it picked up the new image:

```bash
ssh warehouse1@<jetson> "podman logs --since 2m pludos-data-engine | grep OTA"
# [OTA] loaded firmware v2 | 104532 B | 75 chunks (1400 B) | crc32=0x6704a3ba
```

That log line is the gateway saying "I will now offer v2 to any shuttle still
on an older version."

### Why scp and not `git pull`

Project policy is to deploy gateway **code** by `git pull` only. A firmware
image is a build **artifact**, not source — and `*.bin` is gitignored on
purpose, so it can't ride a `git pull`. Hence scp. If you'd rather ship
firmware through git too, un-ignore just this path
(`!client/firmware/firmware.bin` in `.gitignore`) and commit the image; the
manifest (`*.json`) is already trackable. That's a deliberate choice, not the
default.

## Step 3 — the shuttle updates itself

Nothing to do here — this is what the firmware does automatically. Described so
you know what "good" looks like and where to watch.

**When it happens.** A shuttle only powers its radio on at a mission boundary:
after a MOVING run settles back to IDLE, it drains its captured data, and in
that same radio-on window it reads the gateway beacon. A parked shuttle that
never moves stays asleep and ships nothing (idle snapshots accumulate in PSRAM
and leave on the next real mission, or via a safety flush if the buffer fills).
So an update lands **on the shuttle's next completed mission**, not instantly.

**The exchange** (`@docs/wire_protocol.md §2b`, UDP port `5685`, magic `PLDO`):

1. The shuttle reads `:fw=2` in the beacon, sees 2 > its own version, and sends
   an `OTA_REQUEST`.
2. The gateway replies `OTA_BEGIN` (the manifest: size, chunk count, CRC), then
   blasts every `OTA_CHUNK`, then `OTA_END`.
3. The shuttle stages each chunk in PSRAM and tracks which it has. After the
   blast it `OTA_NAK`s the gaps; the gateway resends only those. This repeats
   (bounded) until the shuttle has every chunk.
4. **Integrity gate.** The shuttle CRC32s the whole staged image and compares it
   to the manifest. **Mismatch → abort, flash is never touched, the old firmware
   keeps running.** Nothing half-written, ever.
5. Match → the shuttle flashes the image into the **inactive** bank, reads it
   back and re-verifies, records a "trial" marker, swaps the active bank, and
   resets into the new image.

**Watch it from the Jetson:**

```bash
ssh warehouse1@<jetson> "podman logs -f pludos-data-engine | grep OTA"
# [OTA] shuttle 2 at v1 → serving v2 (75 chunks) to ('192.168.0.163', 5684)
# [OTA] shuttle 2 NAK → resending 4 chunks            (only if loss occurs)
# [OTA] shuttle 2 ACK_COMPLETE v2 — image received intact, committing
```

**Watch it on the shuttle** (serial console, 115200 8N1):

```
[OTA] update offered: v2 > v1 — requesting
[OTA] receiving image v2: 75 chunks, 104532 bytes (have 71 after burst)
[OTA] round 1: have 71/75 chunks — NAKing gaps        (only if loss occurs)
[OTA] image complete + CRC verified
[OTA] installing: writing 104532 bytes to inactive flash bank...
[OTA] flashed inactive bank — swapping + resetting into new image
... reset ...
[BOOT] firmware FW_VERSION=2
[OTA] now running NEW firmware v2 (trial boot 1/3) — awaiting self-confirm
[OTA] new firmware v2 confirmed good — now the known-good image   (after uptime window)
```

## Anti-brick — confirm-or-revert

The swap is on trial. A freshly-swapped image must prove itself or the shuttle
rolls back on its own — no ST-Link rescue needed.

- The new image boots with its OTA state marked `TRIAL`.
- It must reach a healthy main loop and survive a short window
  (`OTA_CONFIRM_UPTIME_MS`), then it marks itself `CONFIRMED`. From then on it's
  the known-good image, and the next OTA flips to the other bank.
- If it instead wedges and keeps resetting, the boot-time check counts the
  trial boots. After `OTA_TRIAL_LIMIT` boots without a confirm, it **reverts the
  bank swap** and resets back into the previous, known-good image.

**Residual risk (honest):** if a bad image hangs *before* the boot-time
confirm-or-revert check runs — i.e. inside the CubeMX-generated clock/HAL init —
auto-revert can't fire and an ST-Link is needed. This is near-impossible in
practice because both banks share identical generated init (same `.ioc`); the
app differences are all after the check. Keep an ST-Link on the bench anyway
(ADR-019 guidance).

---

## Bench validation procedure

To prove the whole path end-to-end with an ST-Link attached:

1. **Two builds.** Build `FW_VERSION=1` and `FW_VERSION=2` (add a visible boot
   log or LED to v2 so the swap is obvious). Flash v1 to the board with the
   ST-Link; stage v2 on the Jetson per Step 2.
2. **Happy path.** Trigger a mission (move the board so it enters MOVING, then
   set it down so it settles to IDLE). On the mission-end drain it should pull,
   verify, flash, swap, and boot v2. Confirm the gateway's `:fw=` token and the
   serial `[BOOT] FW_VERSION=2`.
3. **ARQ under loss.** Drop 10–30 % of packets on `5685`
   (`tc`/`iptables` on the Jetson) and repeat — NAK rounds should recover every
   chunk and the image should still verify.
4. **Integrity gate.** Corrupt one byte of `firmware.bin` server-side (without
   fixing the manifest CRC path) — the shuttle's whole-image CRC must fail, flash
   stays untouched, v1 keeps running.
5. **Rollback.** Ship a v2 that deliberately hangs in the main loop — after
   `OTA_TRIAL_LIMIT` resets the shuttle must auto-revert to v1.
6. **Ping-pong.** Run OTA twice and confirm it alternates banks with the same
   `.bin`.

---

## Tunables

| Where | Constant | Meaning |
|-------|----------|---------|
| `client/.env` | `OTA_PORT` (5685) | OTA chunk-server UDP port |
| `client/.env` | `OTA_CHUNK_SIZE` (1400) | per-chunk payload bytes (datagram < 1472 B) |
| `client/.env` | `OTA_CHUNK_PACING_MS` (2.0) | inter-chunk gap; curbs EMW3080 RX overrun |
| `client/.env` | `OTA_CONTROL_REPEAT` (3) | BEGIN/END repeat count |
| `Core/Inc/ota.h` | `OTA_MAX_ROUNDS` (8) | NAK rounds before giving up |
| `Core/Inc/ota.h` | `OTA_REQ_ATTEMPTS` (4) | REQUEST retries (sent one-at-a-time) |
| `Core/Inc/ota.h` | `OTA_TRIAL_LIMIT` (3) | trial boots before auto-revert |
| `Core/Inc/ota.h` | `OTA_CONFIRM_UPTIME_MS` (30000) | uptime a trial image must survive to self-confirm |

## Security

None, by design (ADR-019 bench tier — trusted LAN). No signing, no encryption,
no auth: any host on the LAN that can speak the protocol on `5685` can serve an
image. The `OTA_BEGIN` manifest leaves room for a signature; the production tier
(ST SBSFU / MCUboot, ECDSA + SHA256 + encrypted images) verifies it *after* the
CRC gate and *before* the bank swap. Do not run this tier on an untrusted
network.
