# Code Conventions

Coding style and conventions for all PLUDOS source files. These apply to
new code. Legacy code that pre-dates these conventions is grandfathered
but should be migrated when touched.

---

## C (STM32 Firmware)

### Memory

- **No `malloc`, `calloc`, `realloc`, `free`** in application code.
  Use static arrays or static pool allocators.
  ```c
  /* GOOD */
  static CriticalPayload_t sensor_buffer[SENSOR_BUFFER_SIZE];

  /* BAD */
  CriticalPayload_t *buf = malloc(sizeof(CriticalPayload_t) * 256);
  ```
- Declare all buffers at **file scope** (not stack-allocated inside functions).
- `cJSON` dynamic allocation is grandfathered for dev/diagnostic paths only.

### HAL

- Use HAL drivers only. Do not call LL (`LL_*`) functions in new code.
- CubeMX-generated UCPD/USB init that uses `LL_*` is grandfathered.
- Never edit `MX_*_Init`, `SystemClock_Config`, or `HAL_*_MspInit` directly.
  Route all peripheral changes through STM32CubeMX (see `pludos-stm32-cubemx` skill).

### CubeMX guards

All application code in CubeMX-generated files must be inside guards:
```c
/* USER CODE BEGIN <section-name> */
/* your code here */
/* USER CODE END <section-name> */
```
Code outside these guards is overwritten on next CubeMX regeneration.

### Naming

- Functions: `VerbNoun` or `verb_noun` — be consistent within a file.
- Constants: `SCREAMING_SNAKE_CASE` via `#define`.
- Types: `PascalCase_t` suffix (e.g., `SensorSample_t`, `SystemState_t`).
- No magic numbers — everything that isn't obvious goes in a `#define`.

### Comments

- One `/* ... */` comment line above each function explaining purpose or contract.
- Inline `//` for end-of-line clarifications.
- Comments explain WHY, not WHAT. Don't restate the code.
- Update comments when you change the logic they describe.

### Logs

Use bracketed module tag on every log line:
```c
printf("[NETWORK] WiFi connect returned: 0x%02X\r\n", status);
printf("[SENSOR] Buffer fill: %.1f%%\r\n", fill_pct);
printf("[BUFFER] Flushing %d samples to Jetson\r\n", count);
```

Defined tags: `[NETWORK]`, `[SENSOR]`, `[BUFFER]`, `[FSM]`, `[COAP]`, `[SYSTEM]`.

---

## Python (Gateway + Server)

### Style

- PEP 8. Line length 100. Use `black` formatter if available.
- Type hints where they clarify intent:
  ```python
  def flush_to_parquet(buffer: list[dict], path: Path) -> None: ...
  ```
- No type hints on obvious one-liners.

### Async

`data-engine.py` runs on `asyncio`. Never block the event loop:
- File I/O: PyArrow `to_parquet` is sync but fires only on flush (acceptable).
- DB writes: use async client or run in executor.
- Heavy CPU: belongs in `client.py` (separate process), not in the CoAP handler.

### Naming

- Functions and variables: `snake_case`.
- Classes: `PascalCase`.
- Constants: `SCREAMING_SNAKE_CASE` at module scope.
- No single-letter variables except loop counters (`i`, `j`) or coordinates (`x`, `y`, `z`).

### Comments

One `#` comment above every function and non-obvious block:
```python
# Compute NTP offset once per shuttle on first packet arrival.
def establish_offset(self, shuttle_id: str, tick_ms: int, receipt_ms: int) -> None: ...
```

### Imports

- Standard library first, then third-party, then project-local.
- No wildcard imports (`from x import *`).

### Configuration

- All tuneable values as module-level constants, not inline literals.
- Secrets (`INFLUXDB_TOKEN`, `TS_AUTHKEY`) from environment variables or `.env`
  files (gitignored). Never hardcode in source.

---

## Containers (Podman / Compose)

- Pin base images — no `:latest` tags except for the Tailscale sidecar
  (which updates frequently).
- One service per container. No supervisord.
- Named volumes, not host bind-mounts (except `/dev/net/tun` for Tailscale).
- GPU passthrough (`deploy.resources.reservations.devices`) for `ai-worker` only.
- Credentials in `.env` files (gitignored). Commit `.env.example` templates.
- Services that require Tailscale go behind `profiles: [vpn]`.

---

## Git

- One logical change per commit.
- Update relevant docs in the same commit as the code change.
- Commit messages: imperative mood, < 72 chars for subject line.
- Branch names: `feature/<short-description>` or `fix/<short-description>`.
- Never commit `pludos_venv/`, `__pycache__/`, `*.parquet`, `.env` files,
  `wifi_credentials.h`, or STM32 build artifacts (`Debug/`).
