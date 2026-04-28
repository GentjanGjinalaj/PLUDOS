# CLAUDE.md — PLUDOS root

You are the agent working on PLUDOS, a CIFRE PhD project (UGA + Savoye,
Grenoble) building an energy-aware federated learning system for warehouse
shuttles. The project is in active development. Many things are hardcoded
placeholders; the goal is to fix them while keeping the architecture stable.

## Communication style

Direct. No filler. No "Certainly!", no "Great question!", no restating my
prompt back at me. Action first. Explain only when asked. Drop articles
where it doesn't hurt readability. If I need detail I'll ask.

This is a token-saving discipline ("caveman mode" with sense). It does not
apply to code or to the docs you write — those still need to be clean and
well-commented for human readers.

## Repo layout

- `STM_Shuttles/PLUDOS_Edge_Node/` — STM32U585 firmware, CubeMX project
- `client/` — Jetson Orin Nano gateway: data-engine.py, client.py, Podman
- `server/` — central server: Flower ServerApp, InfluxDB+Grafana compose
- `docs/` — committed reference docs (architecture, wire protocol, etc.)
- `.claude/skills/` — project skills (loaded on demand by Claude Code)

## Authoritative reference docs

These are the source of truth for design decisions. Read them with `@`
mentions when relevant rather than re-deriving from code:

- `@docs/architecture.md` — three-tier system, current implementation status
- `@docs/wire_protocol.md` — exact byte layouts, CoAP framing, retry rules
- `@docs/state_machine.md` — STM32 idle/moving FSM with all thresholds
- `@docs/decisions.md` — ADRs (open: ADR-010 federation, ADR-011 Alumet)
- `@docs/conventions.md` — code style for C, Python, containers
- `@docs/glossary.md` — domain terms (shuttle, gateway, mission, etc.)
- `@docs/hardware_refs.md` — datasheets and external references
- `@docs/current_problems.md` — backlog (P0/P1/P2)
- `@docs/next_steps.md` — phased roadmap

When code disagrees with a doc, code wins for current behaviour but flag
the disagreement so the doc gets fixed in the same commit.

## Hardware (assume unless I say otherwise)

- **MCU:** STM32U585AII6Q on B-U585I-IOT02A. Cortex-M33 @ 160 MHz, TrustZone
  (used non-secure), 2 MB Flash, 786 KB SRAM total (768 KB main + 16 KB SRAM4).
  Wi-Fi via MXCHIP EMW3080 over SPI2 (2.4 GHz only).
- **Gateway:** Jetson Orin Nano Super Developer Kit. 8 GB LPDDR5,
  6-core Cortex-A78AE, Ampere GPU 1024 CUDA + 32 Tensor cores, 67 TOPS,
  7–25 W power envelope. JetPack BSP r35.x.
- **Central:** laptop for now; permanent server later.

## Stack (don't propose swapping these without justification)

- Embedded: STM32CubeIDE, HAL only for new code, no `malloc`, static buffers
- Gateway/server: Python with async/await, Podman containers (not Docker)
- FL: Flower (`flwr`), XGBoost
- Energy: Alumet target (currently a placeholder, see ADR-011)
- Network: CoAP CON for critical, raw UDP for non-critical, Tailscale overlay
- Storage: tmpfs on gateway, Parquet via PyArrow, InfluxDB on server

## Hard rules

1. **Don't edit `.ioc` files.** They are CubeMX project files. The IDE
   regenerates code from them. If a peripheral or pin needs changing,
   STOP and tell me to open `PLUDOS_Edge_Node.ioc` in STM32CubeMX/CubeIDE,
   make the change there, save, regenerate. Then I run the build and you
   resume in the regenerated source. Editing the `.ioc` directly will
   diverge from the CubeMX view and break the next regeneration.
2. **Preserve `USER CODE BEGIN/END` guards verbatim** in CubeMX-generated
   files. Anything outside these guards is overwritten on regeneration.
3. **No `malloc`/`calloc`/`realloc`/`free`** in STM32 application code. The
   vendored `cJSON` is grandfathered for dev paths only.
4. **No HAL/LL mixing** in new code. CubeMX-generated UCPD/USB init using
   `LL_*` is grandfathered.
5. **Don't invent numbers.** Datasheet values, power figures, benchmarks —
   if you don't have a primary source, say "unknown" and tell me what to
   measure. Cite by section, not by paraphrase.
6. **Don't claim novelty** without comparing against prior IoT / FL
   literature. The thesis is mine; the bullshit detector is mine too.
7. **Don't silently swap technologies.** If a swap is genuinely better,
   say so with the trade-off, then continue with the chosen stack.

## Workflow defaults

- For non-trivial changes: use plan mode first. Lay out the approach,
  let me approve, then implement. Correcting a plan is cheaper than
  unwinding a half-finished feature.
- For research questions: don't pretend they're solved. ADR-010
  (federated XGBoost aggregation) and ADR-011 (real Alumet integration)
  are open. Don't write code that claims to address them without saying
  "this is a placeholder for ADR-X."
- For code review: use the `pludos-c-review` skill on STM32 code.
  For Python code, walk through `@docs/conventions.md` checklist.
- For commits: one logical change per commit. Update relevant docs in
  the same commit if you changed behaviour they describe.

## Code style (applies to your output)

- Clean, minimal, well-commented. Comments explain *why*, not *what*.
- Static buffers declared at file scope in C; type hints in Python where
  they clarify intent.
- Logs: bracketed module tag, e.g. `[NETWORK]`, `[SENSOR]`, `[BUFFER]`.
- No magic numbers. Use `#define` (C) or module constants (Python).
- Prefer fewer lines that read clearly over clever one-liners.

## When you're stuck or uncertain

- Searching the web is fine for current STM32 errata, Flower API
  changes, Alumet docs.
- For project-internal questions, read the docs above before asking me.
- If you don't know something and it matters, say so and propose a
  measurement or a doc check. Don't fabricate.
