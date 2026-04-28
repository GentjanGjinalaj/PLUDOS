# Working with Claude Code on PLUDOS

This is a workflow guide, not a reference. Read it once, refer back as
you build muscle memory. Companion to the CLAUDE.md, skill files, and
docs that already live in the repo.

## TL;DR — the four surfaces

You now have four places where Claude operates, with different cost
and context characteristics:

| Surface | Lives in | Always-loaded? | Use for |
| --- | --- | --- | --- |
| **claude.ai chat (Project)** | browser / desktop app | Project Instructions + RAG over Knowledge | one-off questions, long discussions, writing |
| **Claude Code (VS Code)** | VS Code extension | `CLAUDE.md` always loaded; skills lazy | day-to-day coding, file edits, runs |
| **Skills (`.claude/skills/*`)** | repo (committed) | lazy, triggered by description match | specialised review / workflow rules |
| **Subfolder `CLAUDE.md`** | repo (committed) | only when working in that subtree | tier-specific rules (firmware, gateway) |

Same brain, four context strategies. Pick the one whose
context window matches the work.

## VS Code setup

1. Install the **Claude Code** extension (publisher: Anthropic).
   Cmd/Ctrl+Shift+X, search "Claude Code", install. ~2M installs, the
   real one.
2. Sign in via the Spark icon in the activity bar.
3. Open the PLUDOS folder. The extension reads `CLAUDE.md` from the
   repo root automatically.
4. Subfolder `CLAUDE.md` files are also picked up — when you @-mention
   a file under `STM_Shuttles/`, the extension layers in
   `STM_Shuttles/CLAUDE.md`.
5. Skills: drop the skill folders under `.claude/skills/<skill-name>/`
   in the repo. Commit them so other agents (and your future self) get
   them automatically.

Optional: enable terminal mode if you prefer the CLI feel
(Settings → Extensions → Claude Code → Use Terminal).

## Plan mode is the most important habit

Before any non-trivial edit, switch to plan mode. The agent lays out
its approach without writing code. You approve, then it implements.

Why this matters more than it sounds:
- Catching a wrong assumption in plan mode costs 30 seconds of reading.
- Catching the same assumption in implemented code costs an hour of
  unwinding.
- For PLUDOS specifically, where many things are wired in subtle ways
  (the EXTI ISR routing, the per-shuttle NTP offset, the CubeMX
  boundary), plan mode gives you the chance to say "no, wait, that
  needs CubeMX" before the agent has spent tokens drafting the wrong fix.

Use it for: anything that touches more than one file, anything that
changes a struct, anything that adds a peripheral, any change to the
wire protocol.

Don't bother for: typo fixes, single-line edits, moving constants to
`#define`, renaming variables.

## Slash commands you'll actually use

- `/clear` — wipe context. Use between unrelated tasks. Cheaper than
  letting irrelevant history sit in every subsequent prompt.
- `/rewind` (or ESC ESC) — go back to last good state when the agent
  goes sideways. Better than asking it to fix its own mistake; the
  mistake stays in context and pollutes everything that follows.
- `/compact` — summarise long conversations into a compact form. Useful
  before a hard task at the end of a long session.
- `/model` — switch model. Use Sonnet for code, Opus for plan mode and
  hard reasoning. The price ratio is roughly 5x; don't burn Opus on
  rote edits.
- `/context` — see how full your context window is. Healthy when this
  is < 50%; nervous when > 80%.
- `/usage` — current plan limits and consumption.

## Token-saving patterns (honest version)

The "caveman method" you've seen on Reddit cuts ~14–21% of session
tokens in real benchmarks (the 75% headlines are output-only). It's
worth doing, but the bigger wins are structural:

### Highest-value patterns

1. **Skills > CLAUDE.md > Knowledge** for context cost. Skills are
   lazy-loaded only when triggered. CLAUDE.md is always in context but
   short. Knowledge is RAG, doesn't always retrieve what you need.
   Structure your project so the always-loaded CLAUDE.md is small and
   detailed rules live in skills.

2. **Don't paste files Claude can read.** If you say "review this file:
   `<paste>`," you've just spent 2x the tokens vs. saying "review
   `@path/to/file.c`." The agent has the read tool; let it use it.

3. **`/clear` between unrelated tasks.** The agent doesn't need to
   remember your morning's debugging session when you start an
   afternoon's documentation pass.

4. **Sonnet for code, Opus for planning.** Roughly 5x price difference.
   Opus is genuinely better at multi-step reasoning; Sonnet is more
   than enough for "implement this function" once the design is set.
   Switch with `/model`.

5. **Don't re-explain.** If the agent already knows something from the
   CLAUDE.md, don't paraphrase it in your prompt. "Review this against
   the conventions" is enough; you don't need to list them.

### The caveman bit

In the root CLAUDE.md you already have a brief "communication style"
section telling the agent to drop filler. That's enough. You don't need
the heavy 562-line skill version some people install — for technical
work the savings plateau quickly past the basics.

If you want to dial it up further, add this to the very top of your
prompts:

> Brief mode. No preamble, no postamble. Action then result.

That overrides for one prompt without re-baking it into CLAUDE.md.

### What NOT to do for token savings

- Don't try to keep the agent in a single long session for days. Long
  context = expensive context = slow context. Start a fresh session
  per logical work unit. The CLAUDE.md keeps it oriented.
- Don't disable thinking / reasoning to save tokens for hard tasks. The
  agent thinking is often what saves the rework cycle.
- Don't paste 5000-line files when the agent only needs to see a
  function. Use `@path/to/file.c#L120-L180` (line range mention) or
  just describe the function name.

## When to use which surface

### Use claude.ai chat (Project) for

- Architecture discussions, ADR-style decisions
- Reviewing prose (papers, thesis sections, READMEs)
- Brainstorming research directions (ADR-010, ADR-011)
- Asking "given my Knowledge files, what do you think about X?"
- Anything where you want a persistent thread you can come back to

### Use Claude Code (VS Code) for

- Writing and editing source files (C, Python, YAML, Markdown)
- Running commands (build, test, deploy via SSH to Jetson)
- Multi-file refactors
- Code review where the agent should also propose patches
- Anything with `git` operations (commits, branches, PRs)

### Use Skills (`.claude/skills/`) for

- Repeatable review workflows (e.g. `pludos-c-review` covers your
  STM32 conventions in one trigger)
- Workflow rules that should fire without you remembering to invoke
  them (e.g. `pludos-stm32-cubemx` catches when you're about to make
  the agent edit `.ioc`-territory code)
- Domain-specific knowledge the agent shouldn't always have loaded

### Use CLAUDE.md (root + subfolders) for

- Always-true facts about the project (stack, hardware, conventions)
- Pointers to other docs via `@docs/architecture.md`-style mentions
- Hard rules that apply to every interaction in this repo

## The PLUDOS-specific gotchas

These are the bumps you'll hit; pre-empt them.

### CubeMX-territory edits

If the agent starts editing `MX_*_Init`, `SystemClock_Config`,
`stm32u5xx_hal_msp.c`, `*.ioc`, or pins outside USER CODE blocks —
stop it and switch to the `pludos-stm32-cubemx` skill workflow. The
skill should auto-trigger but you may need to nudge ("This is a
CubeMX-side change, route via the skill").

### Long-running commands over SSH

Builds on the Jetson can take 5+ minutes; FL training rounds 10+ min;
container image rebuilds longer. Don't let the agent block on these.
Either:
- Background them and poll: `ssh jetson "cmd > /tmp/log 2>&1 &"`
- Tell the agent to advise you to run them in a tmux session, then
  paste back relevant logs.

### The "research vs engineering" boundary

ADR-010 (real federated XGBoost) and ADR-011 (real Alumet) are
research questions. If the agent proposes a 50-line patch for either,
push back. Both need a literature pass and an experimental design,
not a quick implementation. Save those for plan mode + Opus.

### The `.gitignore` / committed `.env` trap

Every time the agent generates compose YAML, double-check it isn't
inlining tokens. The pattern is: `${TOKEN}` in YAML, real value in
`.env`, `.env` in `.gitignore`, `.env.example` committed.

## Recommended folder layout

After applying these files, your repo should look like:

```
PLUDOS/
├── CLAUDE.md                              # NEW: replaces CLAUDE.MD
├── STM_Shuttles/
│   ├── CLAUDE.md                          # NEW: subfolder rules
│   └── PLUDOS_Edge_Node/
│       └── ...
├── client/
│   ├── CLAUDE.md                          # NEW: subfolder rules
│   ├── compose.yaml
│   └── ...
├── server/
│   └── ...
├── docs/
│   ├── architecture.md                    # MOVED from KB upload
│   ├── wire_protocol.md
│   ├── state_machine.md
│   ├── decisions.md
│   ├── conventions.md
│   ├── glossary.md
│   ├── hardware_refs.md
│   ├── current_problems.md
│   ├── next_steps.md
│   └── (existing docs/*.md you already have)
├── .claude/
│   └── skills/
│       ├── pludos-c-review/
│       │   └── SKILL.md
│       ├── pludos-stm32-cubemx/
│       │   └── SKILL.md
│       └── pludos-podman-jetson/
│           └── SKILL.md
├── pyproject.toml
├── requirements.txt
└── README.md
```

Key moves vs. what you have today:
- Delete the old `CLAUDE.MD` (case mismatch with `.gitignore` + drift).
- Drop the new root `CLAUDE.md` in. Commit it.
- Drop subfolder `CLAUDE.md` files in. Commit them.
- Commit the docs as `docs/*.md` so both Claude Code and the claude.ai
  Project Knowledge stay in sync (the chat side keeps reading from KB
  uploads; the agent side reads from the repo).
- Drop `.claude/skills/` skills in. Commit them.

When you fix things in the docs, both surfaces benefit. When the docs
drift, both surfaces drift. There's only one set of facts to maintain.

## A first-day checklist

Once everything is committed:

1. Open VS Code in the PLUDOS folder.
2. Open Claude Code (Spark icon).
3. Try this prompt: `What's the current state of CoAP retry logic?`
   - If it answers from `wire_protocol.md` and references the manual
     application-layer loop with the doc/code mismatch flagged, your
     CLAUDE.md and docs are wired correctly.
4. Try this prompt: `Review @STM_Shuttles/PLUDOS_Edge_Node/Core/Src/main.c
   against the PLUDOS conventions.`
   - The `pludos-c-review` skill should auto-trigger and produce the
     structured review output. If it doesn't trigger, check the skill
     description in `SKILL.md` matches your phrasing better.
5. Try this prompt: `I want to add ADC reading for the power sensor.`
   - The `pludos-stm32-cubemx` skill should auto-trigger and route you
     to the CubeMX UI rather than start writing `HAL_ADC_*` calls.
6. If any of those fail, the description in the relevant SKILL.md is
   the place to tune. Skills fire based on description match; if your
   actual phrasing doesn't match the description, the skill won't load.

## A note on the project Knowledge files

The `.md` files you already uploaded to the claude.ai Project's
Knowledge tab are the same content as the `docs/*.md` files you'll
commit to the repo. Don't worry about duplication — it's two surfaces
reading the same source. Update both when you change something.

The one file that doesn't belong in `docs/` is `project_instructions.md`
— that's just paste-text for the claude.ai Project Instructions field.
Don't commit it.
