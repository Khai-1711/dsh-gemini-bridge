# DSH Gemini Agent Bridge (v3.1 - Modular Gateway Edition)

> 🌐 **Language:** **English** | [Tiếng Việt](README_VI.md)

> **High-Reliability OpenAI-Compatible Reverse Proxy, Multi-Lane Gateway & Agent Execution Bridge for Gemini Web (`gemini_webapi`) on DeepSeek Harness (`pi-ai`), Cline, and Roo Code.**

Transforms **Google Gemini Web** into an OpenAI Chat Completions (`/v1/chat/completions`, `/v1/responses`) backend with **Function Calling (`<tool_call>`)**, **Sticky + Least-Inflight Multi-Lane Gateway (`:10000`)**, **Direct Multimodal Vision**, **Sticky Conversation Pools**, **Cookie Failover + Flash Handoff**, and a **W3C Custom-Elements Dashboard**.

---

## Technical Highlights (v3.1)

1. **Sticky Gateway Load Balancer (`app/gateway_router.py` on `:10000`):** Automatically distributes **Lead** and parallel **Subagents** across worker lanes (`acc1` `:10001`, `acc2` `:10002`) via sticky session keys (`STICKY_TTL=3600s`) and least-inflight selection, with zero-buffer SSE pass-through, automatic `429`/`503`/`ConnectError` failover, client-disconnect inflight cleanup, and local DSH session-title interception.
2. **Sticky Session Pool & Isolated Per-Slot Ledgers (`app/session_pool.py`, `app/handoff.py`):** Maintains isolated `ChatSession` slots (`cid`/`rid`/`rcid`) for the root Lead agent (disk-persisted `handoff/<id>/ledger.json`) and up to 16 concurrent Subagents (in-memory ledger, no disk overwrite collisions).
3. **Thoughts-Aware Stream Deadlines & Ephemeral Soft-Fallback (`app/gemini_runtime.py`):** Treats both `thoughts_delta` and `text_delta` as live upstream activity; applies `MODEL_FIRST_BYTE_FALLBACK_TIMEOUT` (`50s`), per-chunk `GEMINI_CHUNK_IDLE_TIMEOUT` (`40s`), and `GEMINI_STREAM_OVERALL_TIMEOUT` (`180s`). Timeout fallback applies **only for that turn** (never permanently downgrading `default_model`), resets `SessionSlot.tool_header_sent`, immediately re-attaches `=== WORKSPACE TOOLS ===` on retry, and records actual `model` + `fallback_from` in `logs/activity*.jsonl`.
4. **Fail-Closed Batch Tool Parser & Streaming Filter (`app/tool_parser.py`, `app/streaming.py`):** Linear `O(N)` `<tool_call>` extraction with `json-repair` and 64 KB raw JSON fallback (`MAX_RAW_JSON_FALLBACK_CHARS=65536`). Rejects an entire batch if any tag in a parallel batch is malformed or unclosed (reporting exact `thẻ #N` diagnostics and saving traces to `logs/failure_traces*.jsonl`) so partial broken tool batches are never executed.
5. **Direct Multimodal Image Upload (`app/vision.py`):** Extracts Base64 image parts, optimizes dimensions/JPEG via optional `pillow`, and attaches files directly to Gemini Web (`files=[]`) with temporary-file cleanup.
6. **Multi-Account Failover & Flash Handoff (`app/account_pool.py`, `app/handoff_runner.py`):** Rotates exhausted/unauthenticated accounts with per-slot cooldowns (`QUOTA_COOLDOWN_HOURS`, `AUTH_COOLDOWN_MINUTES`) and summarizes active `FactLedger` state via Flash handoff when switching accounts.
7. **Anti-Timeout SSE Heartbeat & Single-Lock Concurrency:** Emits `: keep-alive\n\n` every `2.5s` over SSE while serializing turns on each worker with `asyncio.Lock(timeout=LOCK_ACQUIRE_TIMEOUT)`.
8. **Hardened C-Drive Rail-Guard (`app/security.py`):** Normalizes paths with `os.path.realpath` relative to `DEFAULT_PROJECT_DIR` and blocks dangerous Drive `C:\` path traversals or PowerShell patterns (`$env:SystemDrive`, `Invoke-Expression`, encoded commands).
9. **Component-Driven Web Dashboard (`frontend/`):** Zero-build W3C Custom Elements + ES Modules console with EN/VI i18n, Dark/Light themes, multi-lane switching, cookie hot-reload, and live activity logs.

---

## Directory Structure

```text
dsh_gemini_bridge/
├── app/
│   ├── __init__.py
│   ├── accounts.py           # Account chain loader & overlap detection
│   ├── account_pool.py       # Account failover, cooldown & Flash handoff trigger
│   ├── activity.py           # Per-instance JSONL activity journal & failure traces
│   ├── config.py             # Instance paths, env loader, timeouts & rotating logger
│   ├── control.py            # API_START_STATE parser & turn/init state helpers
│   ├── env_update.py         # Runtime .env cookie updater (hot-reload)
│   ├── errors.py             # Structured BridgeError -> OpenAI HTTP status mapping
│   ├── gateway_router.py     # Sticky + least-inflight + failover Gateway proxy (:10000)
│   ├── gemini_runtime.py     # SingleChatManager core, unified stream loop & fallback
│   ├── gemini_session.py     # Compatibility facade exporting session_manager & config
│   ├── handoff.py            # Structured FactLedger (persistent Lead / in-memory Subagent)
│   ├── handoff_runner.py     # Flash summary builder on account failover
│   ├── lane_security.py      # Host, Origin, LAN bind & X-Lane-Token guards
│   ├── lanes.py              # LaneRegistry cross-worker status, proxy & spawn helpers
│   ├── openai_adapter.py     # /v1/models, /v1/chat/completions, /v1/responses router
│   ├── prompt_builder.py     # Compact tool compiler, Smart Truncation & delta prompts
│   ├── routes_admin.py       # /health, /dashboard, /v1/control/*, /v1/lanes/*, /v1/gateway/routes
│   ├── security.py           # Drive D rail-guard path & PowerShell sanitizer
│   ├── session_pool.py       # Sticky SessionSlot pool & .bridge_session*.json persistence
│   ├── status_view.py        # Diagnostic payload builder for /v1/status & /health
│   ├── streaming.py          # ToolCallAwareStreamer hiding <tool_call> tags from text SSE
│   ├── title_generation.py   # Zero-latency local DSH session title generator
│   ├── tool_parser.py        # Fail-closed batch <tool_call> parser + json-repair + Auto-Mapper
│   └── vision.py             # Direct Base64 -> temp image pipeline for Gemini Web
├── frontend/                 # Component-driven Web Dashboard (W3C Custom Elements)
├── scripts/
│   └── live_smoke.py         # Optional live smoke harness against a running bridge
├── tests/                    # Automated unittest + httpx integration test suite (201 tests)
├── main.py                   # Thin FastAPI entrypoint (~206 lines)
├── run_all.bat               # Launch Gateway (:10000) + acc1 (:10001) + acc2 (:10002)
├── run_acc1.bat / run_acc2.bat / run_frontend.bat / stop_all.bat
├── .env.example              # Environment configuration template
├── requirements.txt          # Python runtime dependencies
├── ARCHITECTURE.md / ARCHITECTURE_VI.md
└── README.md / README_VI.md
```

---

## Quick Start Guide

### 1. Install Dependencies
Requires Python `>= 3.10`. From `dsh_gemini_bridge/`:

```bash
pip install -r requirements.txt
```

*(Optional, for resizing/JPEG-compressing large Base64 screenshots before upload)*:
```bash
pip install pillow
```

### 2. Configure Environment (`.env` / `.env.acc1` / `.env.acc2`)
Copy `.env.example` to `.env` (and `.env.acc1`, `.env.acc2` for multi-lane Gateway mode) and populate your Gemini Web cookies from Chrome DevTools (`F12` → **Application** → **Cookies** → `https://gemini.google.com`):

```ini
SECURE_1PSID=g.a000...
SECURE_1PSIDTS=sidts-...
DEFAULT_PROJECT_DIR=D:\ABC
```

*(On Windows, grant normal write permissions on your Drive D workspace once: `icacls "D:\ABC" /grant "$($env:USERNAME):(OI)(CI)F" /T`.)*

### 3. Launch Gateway + Both Workers
- Double-click **`run_all.bat`** to start:
  - **Gateway Router & Dashboard:** `http://127.0.0.1:10000` (`/dashboard`, `/v1/*`)
  - **Worker `acc1`:** `http://127.0.0.1:10001`
  - **Worker `acc2`:** `http://127.0.0.1:10002`
- Stop all bridge processes cleanly anytime via **`stop_all.bat`** (or toggle individual lanes via the Dashboard On/Off switch for graceful in-flight draining).

---

## DeepSeek Harness (`pi-ai`) Integration

Point DSH directly at the **Gateway Router (`:10000`)** so Lead + Subagents automatically fan out in parallel across `acc1` and `acc2`:

| Field | Value |
|---|---|
| **API Protocol** | `OpenAI Chat Completions` |
| **Base URL** | `http://127.0.0.1:10000/v1` |
| **API Key** | Any string (or your `.env` `API_KEY` if set) |
| **Model ID** | Exact ID from `GET /v1/models` (e.g. `gemini-4-neon`, `gemini-4-hydrogen`) |

> For single-worker debugging, `http://127.0.0.1:10001/v1` (`acc1`) and `http://127.0.0.1:10002/v1` (`acc2`) remain independently reachable. Never share the same Google account cookies across `.env.acc1` and `.env.acc2` (verify `account_key` differs on `/v1/status`).

---

## API & Management Endpoints

| Method | Endpoint | Description |
|---|---|---|
| **POST** | `/v1/chat/completions`, `/v1/responses` | OpenAI chat/tool completions (proxied by Gateway when `:10000` is active, executed locally on workers). |
| **GET** | `/v1/models` | Live Gemini models discovered on worker accounts (aggregated on Gateway). |
| **GET** | `/v1/gateway/routes` | Inspect Gateway `active` flag, live `inflight`, `cooldowns`, and `sticky` session bindings. |
| **GET** | `/health`, `/v1/status` | Runtime telemetry, auth/account state, uptime, and error rates. |
| **GET** | `/dashboard` | Web management console. |
| **GET/POST** | `/v1/control`, `/v1/control/enable`, `/v1/control/disable` | Inspect or toggle Gemini connection with graceful drain. |
| **POST** | `/v1/update-cookies`, `/v1/accounts/{slot}/cookies` | Hot-reload primary or backup cookies without restarting. |
| **POST** | `/v1/switch-model`, `/v1/reset`, `/v1/accounts/clear-exhausted` | Switch model, reset active chat session, or clear account cooldowns. |
| **GET** | `/v1/logs`, `/v1/activity` | Recent rotating logs and structured JSONL activity (`model`, `fallback_from`, `tools`, `latency_ms`). |
| **GET/POST** | `/v1/lanes`, `/v1/lanes/{lane_id}/*` | Aggregate lane status and control remote worker lanes via `X-Lane-Token`. |
| **POST** | `/v1/cache/clear` | Compatibility no-op endpoint (`{"cleared_entries": 0}`). |

---

## Testing & Verification

Run the full automated unit + integration test suite:

```powershell
python -B -m unittest discover -s tests
```

Optional live smoke check against a running instance (`:10000` or `:10001`):

```powershell
python scripts/live_smoke.py --base-url http://127.0.0.1:10000
```

---

## Troubleshooting

1. **HTTP 503 `bridge_disabled` / `no_healthy_lane`:**
   Open `http://127.0.0.1:10000/dashboard` and make sure `acc1` / `acc2` are switched **ON** (green) and `API_START_STATE=on` in `.env*`.
2. **Cookies expired or unintended Guest fallback:**
   Refresh `__Secure-1PSID` and `__Secure-1PSIDTS` from `gemini.google.com` and paste them into **Cookie Manager (Hot-Reload)** on the dashboard or `POST /v1/update-cookies`. Set `ALLOW_GUEST_MODE=false` if you prefer strict blocking over Guest fallback.
3. **Gateway not reloading code after edits:**
   The Dashboard **OFF/ON** toggle only closes/reopens `GeminiClient` inside the running Python process. Run `stop_all.bat` then `run_all.bat` after code updates, and start a **New Session** in DSH if `.bridge_session*.json` was cleared.
4. **Missing required property on tool calls:**
   `normalize_tool_arguments()` in [app/tool_parser.py](app/tool_parser.py) maps common aliases (`path` → `file_path`, `cmd` → `command`, `filePath` ↔ `file_path`); malformed tags reject the entire batch fail-closed and record diagnostics in `logs/failure_traces*.jsonl`.
