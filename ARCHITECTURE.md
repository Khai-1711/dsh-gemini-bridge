# System Architecture & Execution Pipeline (v3.1)

> 🌐 **Language:** **English** | [Tiếng Việt](ARCHITECTURE_VI.md)

This document breaks down the modular architecture, Gateway load-balancing pipeline, streaming timeout state machine, and fail-closed safety gates in **DSH Gemini Agent Bridge v3.1**.

---

## 1. End-to-End Request Lifecycle

```text
[DeepSeek Harness (pi-ai) / Coding Agent Client]
        │
        │ 1. POST /v1/chat/completions or /v1/responses (messages + tools + base64 images)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Frontend Gateway (:10000) — main.py + app/openai_adapter.py             │
│  ├─ Auth Middleware (API_KEY / X-Lane-Token / Allowed Hosts)            │
│  ├─ app/title_generation.py: Local DSH session-title fast path (<5ms)   │
│  └─ app/gateway_router.py (when GATEWAY is active):                     │
│       ├─ compute_session_key() + is_subagent_messages()                 │
│       ├─ Sticky binding (STICKY_TTL=3600s) + Least-Inflight selection   │
│       ├─ Role spread: Lead -> acc1, parallel Subagents -> acc2 / idle   │
│       └─ Zero-buffer SSE pass-through + 429/503/ConnectError failover   │
└──────────────┬───────────────────────────────────────┬──────────────────┘
               │ acc1 (:10001)                         │ acc2 (:10002)
               ▼                                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Worker OpenAI Adapter — app/openai_adapter.py                           │
│  ├─ Select / isolate SessionSlot (app/session_pool.py) & FactLedger     │
│  ├─ app/prompt_builder.py: Tool compiler (Turn 1), plain-text protocol  │
│  │  reminders (Turn 2+), Smart Truncation on role=="tool" outputs       │
│  ├─ app/vision.py: Base64 extract -> optional Pillow compress -> files  │
│  └─ StreamingResponse + 2.5s ": keep-alive\n\n" SSE heartbeat           │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Worker Execution Core — app/gemini_runtime.py (SingleChatManager)       │
│  ├─ SessionPoolMixin (app/session_pool.py): 16 slots, cid/rid/rcid lock │
│  ├─ AccountPoolMixin (app/account_pool.py): Failover & cooldowns        │
│  ├─ HandoffRunnerMixin (app/handoff_runner.py): Flash FactLedger handoff│
│  └─ Stream Deadlines:                                                   │
│       ├─ First activity (thoughts_delta OR text_delta): 50s fallback    │
│       ├─ Per-chunk idle timeout: 40s | Overall stream cap: 180s         │
│       └─ Ephemeral soft-fallback resets slot tool_header_sent + retries │
│          immediately with WORKSPACE TOOLS, then restores default_model  │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ 3. Parsed Response Text
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Fail-Closed Protocol Gate — app/streaming.py + app/tool_parser.py       │
│  ├─ ToolCallAwareStreamer suppresses <tool_call>...</tool_call> in SSE  │
│  ├─ Linear O(N) extraction + json-repair + 64 KB raw JSON fallback      │
│  ├─ Auto-Mapper normalizes arg aliases (path->file_path, cmd->command)  │
│  └─ Any broken/unclosed tag fails the ENTIRE batch (failure_traces*.jsonl)
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ 4. OpenAI tool_calls or assistant text
                                   ▼
                [DeepSeek Harness executes local tools on Drive D:]
```

---

## 2. Core Subsystems

### 2.1. Gateway Router (`app/gateway_router.py`)
* **Automatic Multi-Lane Fan-Out:** Activated in `main.py` `lifespan()` when `PORT == 10000`, `INSTANCE_ID` is empty/`default`/`frontend`/`gateway`, and `PEER_LANES` is configured (`acc1=10001`, `acc2=10002`).
* **Sticky + Least-Inflight Routing:** Computes the same `(conv_key, is_new_conv)` and `is_subagent` signals as workers. Subsequent turns of an active conversation stay pinned to their assigned worker (`STICKY_TTL=3600s`, `MAX_STICKY_SESSIONS=128`), while new Lead requests prefer `acc1` and new Subagents balance onto the lowest-inflight healthy peer (`acc2` first when tied with `acc1`).
* **Resilient Failover & Cleanup:** On `ConnectError` or HTTP `429`/`503`, marks the failing lane in cooldown (`15s`) and retries once on an alternate healthy worker. On SSE client disconnect or upstream close, decrements `_gateway_inflight` in `finally`.

### 2.2. Worker Runtime, Session Pool & Account Failover
* **`app/gemini_runtime.py` (`SingleChatManager`):** Orchestrates per-turn `asyncio.Lock` serialization, throttling (`MIN_REQUEST_INTERVAL`), retry/recovery, and unified streaming (`send_prepared_turn` delegates directly to `_send_prepared_turn_stream_inner`).
* **`app/session_pool.py` (`SessionPoolMixin`):** Maps each `conv_key` to an isolated `SessionSlot` (`SESSION_POOL_MAX_SIZE=16`, `SESSION_POOL_TTL=3600s`) so concurrent Lead and Subagent conversations never collide on `cid/rid/rcid` metadata. Lead persists `FactLedger` to `handoff/<id>/ledger.json`; Subagent slots use `FactLedger(..., persist=False)`.
* **`app/account_pool.py` & `app/handoff_runner.py`:** Rotates backup cookies (`BACKUP_1PSID`..`BACKUP_4PSID`) on quota/auth errors with `.bridge_accounts*.json` cooldown tracking and Flash-model `FactLedger` handoff summaries. When `ALLOW_GUEST_MODE=false`, unauthenticated guest sessions are blocked with HTTP 503 (`gemini_not_authenticated`).

### 2.3. Thoughts-Aware Deadlines & Ephemeral Soft-Fallback
* `_iterate_stream_until()` monitors both `thoughts_delta` and `text_delta` so reasoning models (`gemini-neon`) are never aborted while emitting thinking chunks.
* **Three-tier budget:** initial wait capped by `GEMINI_HARD_TIMEOUT` (`90s`) and `MODEL_FIRST_BYTE_FALLBACK_TIMEOUT` (`50s`); once `thoughts_delta` or `text_delta` starts, switches to `GEMINI_CHUNK_IDLE_TIMEOUT` (`40s`) per chunk under `GEMINI_STREAM_OVERALL_TIMEOUT` (`180s`).
* **Ephemeral Soft-Fallback:** If the first chunk times out before `text_delta` begins, `SingleChatManager` uses `model_override` to retry on the fallback model **for that turn only**, resets `tool_header_sent=False` on the active `SessionSlot` so `_ensure_header_in_prompt` immediately re-attaches `=== WORKSPACE TOOLS ===`, restores `default_model` before `_save_session()`, and records actual `model` + `fallback_from` to `logs/activity*.jsonl`.

### 2.4. Direct Vision & Smart Truncation (`app/vision.py`, `app/prompt_builder.py`)
* **Direct Native Vision:** Decodes Base64 images (optionally compressing large screenshots via `pillow`) and passes temporary files directly via Gemini Web's `files=[]` upload without local OCR layers. `_TEMP_FILES` cleanup runs in `finally`.
* **Neutral Tool Protocol:** Frames `<tool_call>{...}</tool_call>` as plain-text IDE markup with no session turn cap, avoiding adversarial system-override phrases while reinforcing tool-tag output on `[TOOL_RESULT]` and subsequent user turns.
* **Smart Truncation:** Applied strictly to `role == "tool"` messages exceeding `MAX_TOOL_RESULT_CHARS` (`12,000`), preserving head, tail, and error/traceback lines extracted from the middle.

### 2.5. Fail-Closed Parsing & Rail-Guard Security (`app/tool_parser.py`, `app/security.py`)
* **Batch Fail-Closed Parsing:** Extracts `<tool_call>` blocks sequentially, repairs minor JSON formatting issues via `json-repair`, supports raw JSON fallback up to `65,536` chars, and normalizes parameter aliases (`Auto-Mapper`). Any invalid or unclosed tag in a parallel batch aborts the entire batch (`HTTP 502` / SSE `invalid_tool_call`), logs raw output to `logs/failure_traces*.jsonl`, and executes zero tools.
* **Drive D Rail-Guard (`app/security.py`):** Developer-focused guard validating `os.path.realpath` relative to `DEFAULT_PROJECT_DIR` and sanitizing dangerous PowerShell / Drive `C:\` patterns.

### 2.6. Observability & On-Demand Lifecycle
* **Per-Instance Logging & Journals:** Rotating server logs (`logs/bridge*.log`), structured JSONL activity (`logs/activity*.jsonl`), and tool parse traces (`logs/failure_traces*.jsonl`).
* **HTTP Error Mapping (`app/errors.py`):**
  * `429` (`bridge_busy`): Worker turn lock timeout or Gateway upstream busy.
  * `502` (`gemini_upstream_error` / `invalid_tool_call`): Upstream failure or malformed tool batch.
  * `503` (`bridge_disabled` / `gemini_not_authenticated` / `no_healthy_lane`): Disabled switch, unauthenticated session, or no healthy Gateway worker.
  * `504` (`gemini_timeout`): Exceeded initial, idle, or overall stream timeout.
* **Graceful Draining & Origin Guard (`app/control.py`, `app/lane_security.py`):** Supports `off` → `starting` → `on` → `stopping` lifecycle with `DISABLE_DRAIN_TIMEOUT` draining and CSRF Origin checks on control endpoints.
