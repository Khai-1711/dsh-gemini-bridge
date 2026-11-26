# DSH Gemini Agent Bridge (v3.3)

> 🌐 **Language:** **English** | [Tiếng Việt](docs/README.vi.md) | [한국어](docs/README.ko.md)  
> 📐 **Architecture & System Diagrams:** [English](docs/ARCHITECTURE.en.md) | [Tiếng Việt](docs/ARCHITECTURE.vi.md) | [한국어](docs/ARCHITECTURE.ko.md)  
> 📋 **Claude / Engineer Case Study & Handoff:** [AGENT_ENGINEERING_CASE_STUDY.md](docs/AGENT_ENGINEERING_CASE_STUDY.md) · [CLAUDE.md](CLAUDE.md)

**DSH Gemini Agent Bridge** is a modular OpenAI-compatible (`/v1/models`, `/v1/chat/completions`, `/v1/responses`) reverse proxy and multi-lane execution gateway that turns **Google AI Pro (Gemini Web)** into a stateful, data-safe, rate-limit-resilient Coding Agent backend for **DeepSeek Harness (DSH)**, IDE agents, and OpenAI SDK clients.

---

## Summary of Everything Built (v1 → v3.3)

### 1. Multi-Lane Gateway, Sticky Routing & Dynamic Lead Continuity
- **Frontend Gateway (`:10000` → `:10001` `acc1`, `:10002` `acc2`, ...):** Sticky conversation affinity (`3600s` TTL), model-aware filtering, and least-inflight load balancing (`app/gateway_router.py`).
- **Dynamic Lead Sticky Lock Wait (`LEAD_STICKY_LOCK_WAIT=240s`):** Lead sessions stay pinned to whichever worker owns their live Gemini `ChatSession` (no hardcoded lane preference) and wait up to `240s` when busy rather than failing over mid-turn and losing chat context. HTTP `429 bridge_busy` responses do not trigger unhealthy lane cooldowns.
- **Compaction-Safe Session Pool (`app/session_pool.py`):** Per-slot isolation (`cid/rid/rcid`, model, workspace root, `FactLedger`), preservation of `tool_header_sent=True` across same-`cid` model switches, automatic lead session reuse across IDE context compaction (`[Conversation Summary]`), and atomic persistence (`tmp + os.replace`) inside gitignored `.bridge_state/`.

### 2. Auth, Profile Vault (0 Open Tabs) & Browser CSRF Security
- **Profile Vault + Headless CDP Auto-Sync (`app/cookie_sync.py`):** Sign in manually once inside an isolated Chrome/Edge profile; future cookie refreshes run silently in ~2–4s via `--headless=new` + a zero-dependency RFC 6455 WebSocket CDP client exporting the full cookie jar (`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID`) and hot-reloading workers.
- **Deadlock-Safe Client Init (`_safe_init_client`):** `25s` timeout guard around `gemini-webapi` recursive `_lock` deadlocks.
- **Cross-Site Browser Request Blocker (`app/lane_security.py`, `main.py`):** Blocks non-`GET`/`HEAD`/`OPTIONS` browser requests carrying foreign/null `Origin` or `Sec-Fetch-Site: cross-site` even when `API_KEY` is empty, without affecting CLI/non-browser OpenAI clients.

### 3. Tool Profiles, Natural Chat vs. Work Separation & DSH Normalization
- **6 Tool Profiles + Live Hot-Swap (`app/tool_profiles.py`, `app/routes_admin.py`):** `no_subagents` (default), `coding`, `coding_parallel`, `research`, `none`, `all` — plus `ALLOWED_TOOLS` / `BLOCKED_TOOLS` patterns and forced `tool_choice` preservation. Hot-swappable via `GET/POST /v1/tool-profile` with Gateway → worker `X-Lane-Token` broadcast. In `coding` mode, DSH's 34 tools trim cleanly to **18 core coding tools** (`7,115+` → **`4,608` tool chars** on Turn 1).
- **Chat-vs-Work Separation (`app/prompt_builder.py`, `app/openai_adapter.py`):** Handles DSH multimodal text payloads (`[{"type": "text", "text": "..."}]`), drops superseded aborted greetings, defers `WORKSPACE TOOLS` on plain greetings (`Chars=4`, `7.6s`, 0 tools) and conceptual Q&A (`Chars=88`, `7.6s`), and lazily injects tools only when a real workspace task begins — reusing the same `ChatSession`.
- **DSH Prompt Cleanup & U-Shaped Layout:** Strips `[model changed: ...]`, `<system-reminder>`, and Web GUI `window.__DSH_BOOT__` noise; fixes unbalanced-parenthesis clipping `(e.g. *.ts)`; places `[SYSTEM] + WORKSPACE TOOLS` first and `[USER REQUEST]` last.

### 4. Fail-Closed Tool Calling, Streaming & Data Integrity
- **Streaming Suppression FSM (`app/streaming.py`):** $O(N)$ single-pass state machine (`ToolCallAwareStreamer`) with partial-tag lookahead that withholds active `<tool_call>` blocks while letting prose fenced code examples stream verbatim.
- **Lossless Parser Guards (`app/tool_parser.py`):**
  - `_find_prose_code_fence_ranges` & `_scan_tool_call_blocks`: ignores illustrative `<tool_call>` tags inside Markdown code fences.
  - `_verify_lossless_heuristic_repair`: rejects heuristic `json_repair` outputs on mutation/execution tools (`write`, `edit`, `pwsh`, etc.) whenever extra keys are invented or any alphanumeric code tokens are dropped, preventing silent file truncation while still rescuing safe inner-quote glitches in `<1ms`.
  - `_sanitize_tool_argument_values`: preserves Markdown self-links `[https://...](https://...)` inside `.md`/`.mdx`/`.markdown`/`.rst` files while unwrapping Gemini auto-links in source code.
- **Turn-Lock Abort Protection & Self-Correction (`app/openai_adapter.py`, `app/gemini_runtime.py`):** Bounded `3.0s` cancel wait in `sse_generator` + `force_release_stuck_turn_lock()` (monotonic `_turn_epoch` + clean `chat_session` detachment from `_last_safe_metadata`); targeted chunked-write self-correction prompt when `<tool_call>` truncates (`>250`-line guidance); `[INTEGRITY]` re-read reminder; omitted char-range reporting in `smart_truncate()`.

### 5. Durable Background Mode (Anti-`1213`, Mid-Stream Retry, Self-Rest & Behavioral Nudges)
- **Transient Non-Auth Retry Without Teardown (`app/gemini_runtime.py`):** On `attempt == 0`, transient errors (`HTTP 502`, resets) when `auth_ok=True` keep the authenticated `GeminiClient` alive and retry immediately using `saved_meta_before_send` instead of wasting 30s re-initializing `gemini.google.com/app`.
- **Mid-Stream Cut Recovery (`MID_STREAM_RETRIES=2`, `MID_STREAM_RETRY_DELAY=20s`):** If Google cuts a stream mid-generation before any user-visible text was emitted (e.g., while buffering `<tool_call>`), waits with backoff (`20s`, `40s`) and retries cleanly from safe metadata (cookies masked in logs).
- **Anti-`1213` Rate-Limit Defense & Auto-Rest (`RATE_LIMIT_REST_AFTER=1`, `RATE_LIMIT_REST_MINUTES=45`):** Enforces `NEW_CHAT_MIN_INTERVAL=25s` between brand-new chats on the same account plus human-like jitter (`TURN_JITTER_MIN=3s`..`TURN_JITTER_MAX=8s`). When Google returns error `1213`, the affected worker automatically enters a 45-minute cool-down (`lane_resting_until` exposed in `/status`) and the Gateway routes around resting lanes.
- **Behavioral Action Nudges (`CODE_DUMP_NUDGE=true`, `CODE_DUMP_MIN_LINES=20`):** Automatically issues a private 1-round nudge if Gemini dumps `>=20` lines of code into chat prose or claims `"no tools"` during an active workspace task, paired with an action-oriented end-of-prompt reminder.
- **Opt-In Soft Fallback (`FALLBACK_MODEL`):** Default empty (`""`), or configure e.g. `FALLBACK_MODEL="gemini-4-hydrogen"` for automatic first-byte timeout fallback.

### 6. End-to-End Verification & Clean Workspace Hygiene
- Verified live via **DeepSeek Harness Desktop + Zero-Screenshot `Windows-MCP` (`use_vision=false`, `--force-renderer-accessibility`)**: `acc2` (`gemini-carbon`, `profile=coding`) built a 3-file web project cleanly in **3 turns / 35s** (`21.8s` + `4.65s` + `5.49s`, **4 parallel tool calls** in Turn 1, **2 `pwsh` verification commands**, 0 parse errors, 0 screenshots).
- Verified cross-browser CSRF protection (`app/lane_security.py`), `ruff check --no-cache --select F,E9,B main.py app scripts` (**0 errors**), and `py_compile` across all modules.
- Offline mock `tests/` and `scripts/live_smoke.py` were intentionally removed per owner preference to avoid synthetic/mock drift and keep verification grounded in static checks and live DSH runs.

---

## Honest Evaluation vs. a Native Production Coding Agent

How does **DSH + `dsh_gemini_bridge v3.3`** compare today against a first-party, native tool-calling Coding Agent (such as **Claude Code**, **Cursor Agent**, **Codex CLI**, or DSH on a native commercial API)?

| Dimension | Native API Agent (Claude Code / Cursor) | DSH + `dsh_gemini_bridge v3.3` | Verdict |
| :--- | :--- | :--- | :--- |
| **Tool-Call Protocol** | Native structured JSON `tool_calls` at API grammar level | Emulated XML `<tool_call>` over web stream + FSM + lossless schema gate + 1-round self-correction | **Near-Parity (9/10)** — reliable in practice, but slightly slower when repair/nudge triggers |
| **Data & File Integrity** | High (structured arguments) | **Very High (9.5/10)** — `_verify_lossless_heuristic_repair` blocks partial truncation; `.md` self-link guard; prose-fence protection | **Production-grade** |
| **Context Retention** | Large raw API window (128k–1M) with full explicit history | Stateful Gemini Web session (`cid/rid/rcid`) + compaction continuity + 36k tool budget | **Good (7.5/10)** — saves huge tokens vs resending, but opaque server-side eviction on very long sessions |
| **Latency & Pacing** | ~1–4s TTFT; high burst RPM | ~5–22s/turn (`gemini-carbon`) + intentional `3–8s` human jitter + `25s` new-chat spacing | **Tradeoff (7/10)** — tuned for account survival rather than rapid-fire bursts |
| **Concurrency / Subagents** | Parallel API calls with minimal rate contention | Multi-lane (`acc1`, `acc2`, ... `workN`) Gateway with dynamic Lead pinning + auto-rest on `1213` | **Strong for 2–4 lanes (8/10)** |
| **Auth & Ops Stability** | Static API key | Profile Vault headless CDP sync (2–4s, 0 open tabs) + auto cookie rotation recovery | **8/10** — handles cookie expiry cleanly, still bound by Google Web policies |
| **Interactive App / UI Testing** | Terminal (`bash`/`pwsh`) + optional MCP/Browser subagent | Full `read`/`write`/`edit`/`pwsh` + background servers in DSH; optional `Windows-MCP` UIA control for desktop/Electron | **8.5/10** with `pwsh`; **9.5/10** with Playwright/MCP attached |
| **Security & CSRF** | Cloud API TLS + IDE sandboxing | Localhost-bound, `Origin` + `Sec-Fetch-Site` CSRF blocking, OS command/path safety (`app/security.py`), masked logs | **Strong Local Posture (8.5/10)** |
| **Cost per Coding Session** | Metered per-token API billing | Uses existing **Google AI Pro** subscription across pooled accounts | **Unbeatable Value (10/10)** |

### Where It Truly Matches a Real Coding Agent (Strengths)
1. **Genuine autonomous loop:** Plans, executes multi-tool batches in parallel (`write` + `pwsh`), reads command outputs/tracebacks, edits targeted bugs, and delivers verified code.
2. **File integrity under failure:** Will not silently corrupt files on truncated output, aborted SSE streams, Markdown links, or code snippets mentioning `<tool_call>`.
3. **Resilient overnight/background survival:** Automatically pauses `1213`-throttled accounts (`45m`), retries transient `502`/mid-stream drops cleanly, and refreshes cookies via headless CDP without manual intervention.

### Honest Limitations Compared to Native API Agents (Gaps)
1. **Google Web output quirks:** Post-generation UI filters on `gemini.google.com` can replace raw XML-only replies visually in web history (even though SSE delivered all chunks cleanly), and rare stray prefix fragments (e.g. `d?`) can appear before `<tool_call>`.
2. **Strict pacing ceiling:** Cannot safely spam rapid parallel `new_chat` bursts without risking `1213` throttling — intentional `25s` new-chat cooldown + `3–8s` jitter prioritizes longevity over raw speed.
3. **No built-in browser DOM driver in default DSH:** Out of the box, DSH verifies syntax, unit tests, and API endpoints via `pwsh`; verifying live Canvas/DOM rendering requires Playwright or a browser/`Windows-MCP` attached to DSH.

> **Recommended Sweet Spot:** Use **Claude Code** as the high-level Architect/Coordinator & final reviewer (`CLAUDE.md`), and dispatch heavy coding/implementation loops through **DSH + `dsh_gemini_bridge`** (or run standalone DSH on `profile=coding`) for the best balance of reasoning quality, reliability, and zero marginal token cost.

---

## Repository Structure

```text
dsh-gemini-bridge/
├── app/
│   ├── gateway_router.py    # Sticky least-inflight router, resting-lane avoidance, lead sticky wait
│   ├── gemini_runtime.py    # SingleChatManager, _turn_epoch, transient 502 & mid-stream retry, 1213 rest
│   ├── session_pool.py      # Multi-session cid/rid/rcid pool, compaction reuse, atomic .bridge_state/
│   ├── tool_profiles.py     # 6 tool profiles (no_subagents, coding, coding_parallel, research, none, all)
│   ├── prompt_builder.py    # U-shaped prompts, chat-vs-work separation, DSH cleanup, smart_truncate
│   ├── streaming.py         # ToolCallAwareStreamer FSM (prose code-fence & partial-tag safe)
│   ├── tool_parser.py       # XML parser, lossless json_repair guard, .md link preservation
│   ├── openai_adapter.py    # OpenAI /v1/chat/completions, self-correction, CODE_DUMP_NUDGE, abort guard
│   ├── cookie_sync.py       # Profile Vault + zero-dep RFC 6455 CDP headless cookie sync
│   ├── lane_security.py     # Loopback/LAN Host & cross-site CSRF validation + peer lane parsing
│   ├── routes_admin.py      # /dashboard, lane control, /v1/tool-profile hot-swap, Vault routes
│   ├── security.py          # Workspace path & dangerous OS command guards
│   └── config.py, accounts.py, account_pool.py, activity.py, control.py, ...
├── docs/                    # AGENT_ENGINEERING_CASE_STUDY.md + EN/VI/KO README & Architecture docs
├── frontend/                # Zero-build Web Components Dashboard (/dashboard)
├── scripts/                 # cookie_sync.py CLI & standalone lane launchers/
├── CLAUDE.md                # Auto-loaded orientation & invariants for Claude Code
├── main.py                  # FastAPI entrypoint (Gateway or Worker via BRIDGE_INSTANCE)
├── run_all.bat / stop_all.bat
└── .env.example             # Full environment configuration reference
```

---

## Quick Start & Verification

```powershell
# 1. Install dependencies & configure .env, .env.acc1, .env.acc2 from .env.example
pip install -r requirements.txt

# 2. Sign in once per Google account into isolated Profile Vault (0 open tabs afterwards)
python scripts/cookie_sync.py login acc1 --lane acc1 --slot 0 --label "Work 1"
python scripts/cookie_sync.py login acc2 --lane acc2 --slot 0 --label "Work 2"

# 3. Start / Stop Gateway (:10000) + Workers (:10001, :10002)
.\run_all.bat
.\stop_all.bat

# 4. Hot-swap tool profile at runtime
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:10000/v1/tool-profile `
  -ContentType 'application/json' -Body '{"profile":"coding","persist":true}'

# 5. Static Verification (lint + bytecode syntax check)
ruff check --no-cache --select F,E9,B main.py app scripts
python -B -m py_compile main.py
```

---

## Key Environment Variables (v3.3)

| Variable | Default | Description |
|---|---|---|
| `TOOL_PROFILE` | `no_subagents` | `no_subagents` \| `coding` \| `coding_parallel` \| `research` \| `none` \| `all` |
| `FALLBACK_MODEL` | `""` | Opt-in first-byte fallback model (e.g. `gemini-4-hydrogen`); empty preserves exact choice |
| `WORKSPACE_SANDBOX_MODE` | `client` | `client` (follow active IDE dir) \| `workspace` (soft-lock to project dir) |
| `AUTO_COOKIE_SYNC` | `true` | Silent Profile Vault CDP cookie recovery on `UNAUTHENTICATED` |
| `MID_STREAM_RETRIES` / `MID_STREAM_RETRY_DELAY` | `2` / `20` | Backoff retries (`20s`, `40s`) when upstream cuts stream before any text reaches client |
| `RATE_LIMIT_REST_AFTER` / `RATE_LIMIT_REST_MINUTES` | `1` / `45` | Auto-rest lane for `45m` on `1213`; Gateway routes around resting lanes |
| `NEW_CHAT_MIN_INTERVAL` | `25` | Minimum seconds between brand-new chats on the same account |
| `TURN_JITTER_MIN` / `TURN_JITTER_MAX` | `3` / `8` | Random human-like spacing (seconds) between consecutive turns |
| `LEAD_STICKY_LOCK_WAIT` | `240` | Max seconds Gateway waits on sticky lead lane before failing over |
| `CODE_DUMP_NUDGE` / `CODE_DUMP_MIN_LINES` | `true` / `20` | Auto-nudge model to emit `<tool_call>` when it dumps `>=20` lines of raw code in chat |
| `GEMINI_HARD_TIMEOUT` / `GEMINI_CHUNK_IDLE_TIMEOUT` / `GEMINI_STREAM_OVERALL_TIMEOUT` | `180` / `120` / `420` | Generous deadlines tuned for deep-thinking models |

---

## Security Notes
- `.env*`, `.bridge_state/`, `.cookie_profiles/`, `.gemini_cookies*/`, `logs/`, `handoff/`, and `.bridge_*.json` are gitignored and never committed.
- Non-GET browser requests with cross-site `Origin`, `"null"` origin, or `Sec-Fetch-Site: cross-site` are rejected unconditionally (`app/lane_security.py`).
- Profile Vault (`/v1/profiles/*`) is restricted to loopback (`127.0.0.1`) + valid Origin; `HOST=0.0.0.0` is blocked at startup without `API_KEY`.

---

## Research Papers, Standards & Upstream References

### Core Academic Papers & Architecture Principles Applied
- **ReAct** — Yao et al. (ICLR 2023), *Synergizing Reasoning and Acting in Language Models* (interleaved reasoning traces + structured actions).
- **Toolformer** — Schick et al. (NeurIPS 2023), *Language Models Can Teach Themselves to Use Tools* (delimited in-band tool markup).
- **Reflexion & Self-Refine** — Shinn et al. (NeurIPS 2023) & Madaan et al. (NeurIPS 2023) (bounded 1-round verbal diagnostic self-correction and behavioral `CODE_DUMP_NUDGE`).
- **Lost in the Middle** — Liu et al. (TACL 2024), *How Language Models Use Long Contexts* (salience-preserving head/tail/error truncation + U-shaped `[SYSTEM] + TOOLS -> [USER REQUEST]` layout).
- **The Power of Two Choices** — Mitzenmacher (2001) & **Release It!** — Nygard (2018) (sticky least-inflight balancing, circuit breakers, rate-limit lane rest isolation, and bulkhead worker pools).
- **Exponential Backoff and Jitter** — Marc Brooker, *AWS Architecture Blog* (randomized turn jitter & progressive retry spacing to avoid synchronized bot/thundering-herd triggers).

### Technical Standards & Protocols
- **RFC 6455** (WebSocket Protocol — stdlib CDP client in `app/cookie_sync.py`)
- **RFC 8259 & JSON Schema Draft 2020-12** (`app/tool_parser.py` lossless validation)
- **W3C Fetch Metadata & Origin CSRF Mitigations** (`Sec-Fetch-Site` / `Origin` checks in `app/lane_security.py`)
- **Chrome DevTools Protocol (CDP)** · **WHATWG Server-Sent Events (SSE)** · **Model Context Protocol (MCP)** · **Microsoft UI Automation (UIA)**

### Upstream Open-Source Ecosystem & Hardening Applied Here

| Upstream Project / Ecosystem | Role | Hardening & Extensions Built in `dsh_gemini_bridge` |
|---|---|---|
| [`HanaokaYuzu/Gemini-API`](https://github.com/HanaokaYuzu/Gemini-API) (`gemini-webapi`) | Gemini Web `ChatSession`, `batchexecute`, `RotateCookies` | Deadlock-safe `_safe_init_client`, full Chrome CDP cookie-jar injection (`osid=1` fix), transient non-auth retry keeping client, `1213` rest isolation, `_turn_epoch` abort protection |
| [`mangiucugna/json_repair`](https://github.com/mangiucugna/json_repair) | Malformed JSON repair | Paired with `_verify_lossless_heuristic_repair` (zero dropped code tokens / invented keys), `.md` self-link guard, prose-fence exclusion, strict JSON Schema gate |
| [`fastapi/fastapi`](https://github.com/fastapi/fastapi) · [`encode/uvicorn`](https://github.com/encode/uvicorn) · [`encode/httpx`](https://github.com/encode/httpx) | Async OpenAI API & Gateway proxy | Pre-SSE turn-lock admission, zero-buffer SSE pass-through, `2.5s` heartbeats, CSRF middleware |
| [`lexiforest/curl_cffi`](https://github.com/lexiforest/curl_cffi) | Browser TLS/HTTP2 impersonation | Extended `180s` / `120s` / `420s` stream deadlines with mid-stream cut retries |
| **DeepSeek Harness (`pi-ai`) & [`CursorTouch/Windows-MCP`](https://github.com/CursorTouch/Windows-MCP)** | Target coding agent & zero-screenshot UIA test driver | Tool profile trimming (`coding` 18/34), chat-vs-work deferral, DSH boilerplate normalization, zero-vision Electron `--force-renderer-accessibility` verification |
