# System Architecture & Execution Pipeline (v3.0)

> 🌐 **Language:** **English** | [Tiếng Việt](ARCHITECTURE_VI.md)

This document provides a comprehensive technical breakdown of the internal design, data flow pipelines, fault tolerance mechanisms, and multi-layered defense architecture in **DSH Gemini Agent Bridge v3.0**.

---

## 1. End-to-End Request Lifecycle

```
[DeepSeek Harness (pi-ai) / Coding Agent Client]
        │
        │ 1. POST /v1/chat/completions (OpenAI Payload: messages + tools + base64 images)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ main.py (FastAPI Controller & SSE Multiplexer)                          │
│  ├─ [Optional] API Key Authentication Middleware                        │
│  ├─ Rút trích Delta Prompt & immediately release heavy payload from RAM │
│  ├─ SHA-256 Response Cache lookup (TTL 5 mins, returns in <1ms)         │
│  ├─ Immediate SSE stream initiation (First Chunk within 1ms)            │
│  └─ Background heartbeat ": keep-alive\n\n" every 2.5s                  │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ app/gemini_session.py (SingleChatManager - Stateful Session Engine)     │
│  ├─ asyncio.Lock(timeout=30s): Race condition & overload prevention     │
│  ├─ Rate-Limit Pacing: Safe 0.5s - 2.0s interval smoothing              │
│  ├─ Cookie Failover Pool: Auto-switches to standby backup on error      │
│  └─ Maintains EXACTLY ONE PERSISTENT THREAD on Gemini Web               │
└──────────────┬───────────────────────────────────────┬──────────────────┘
               │                                       ▲
               ▼ 2. Prompt Compilation & Vision        │ 6. Raw Response
┌──────────────────────────────┐             ┌─────────┴──────────────────┐
│ app/prompt_builder.py        │             │ Gemini Web (Google Server) │
│  + app/vision.py             │────────────►│  - Handled on 1 Session    │
│  ├─ Smart Truncation: Error  │ 3. Send via │  - Native image upload     │
│     and stacktrace protection│    files=[] │  - auto_refresh=True       │
│  ├─ Tier 1: Native Vision    │             └────────────────────────────┘
│  ├─ Tier 2: 3-Engine OCR (RapidOCR -> WinRT -> Tesseract Fallback)      │
│  └─ Sandwich Prompting: Strict Language Matching & Clarification First  │
└──────────────────────────────┘
               │
               ▼ 7. Extraction & Security Gate
┌─────────────────────────────────────────────────────────────────────────┐
│ app/tool_parser.py + app/security.py (Auto-Mapper & Hardened Sandbox)   │
│  ├─ Linear O(N) tag parsing for <tool_call> (Parallel Tools Supported)  │
│  ├─ Auto-Mapper: Key alignment (path->file_path, cmd->command, etc.)    │
│  ├─ Path Jail: Realpath traversal neutralization blocking C:\           │
│  └─ Shell Firewall: Drops Pwsh containing $env, backticks, IEX, C:\     │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   │ 8. Stream OpenAI tool_calls or content
                                   ▼
          [DeepSeek Harness executes local tools inside Drive D:]
```

---

## 2. In-Depth Component Analysis

### 2.1. Stateful Single-Chat Engine & Cookie Failover Pool
* **The Stateless Dilemma:** Standard OpenAI-compatible coding agents re-send the full conversation history on each tool step. When calling standard web APIs, this creates dozens of separate conversations, causing prompt token bloat and Google anti-spam triggers.
* **Stateful Delta Solution:**
  * `SingleChatManager` initializes a single persistent `ChatSession` (`self.client.start_chat()`).
  * On subsequent turns (`FirstTurn = False`), only the incremental delta (tool results `[TOOL_RESULT]` or new user feedback) is transmitted.
* **Authentication Verification & Guest Mode Interception:**
  * `gemini-webapi` 2.x silently drops into an unauthenticated Guest mode when cookies are invalid. The bridge explicitly verifies `client.account_status == AccountStatus.AVAILABLE` and `client._cookie_source != "Guest"` after every `init()`.
  * If unauthenticated, requests are blocked with HTTP 503 (`gemini_not_authenticated`) to preserve the persistent chat and Gemini Pro access.
* **Account-Bound Session Persistence (`account_key`):**
  * `.bridge_session.json` includes an `account_key` (short SHA-256 hash of active PSID). On startup or cookie switch, session restoration requires matching account keys, preventing conversation cross-contamination.
* **Failover Cookie Pool:**
  * Tracks consecutive authentication errors (`_consecutive_auth_errors >= 2`).
  * If consecutive auth errors occur, the system tears down the active client and re-initializes with the backup account (`BACKUP_1PSID`, `BACKUP_1PSIDTS`).
  * Endpoints like `POST /v1/update-cookies` allow hot-reloading cookies at runtime without restarting the process.
* **Queue Lock with Timeout:**
  * Enforces `asyncio.wait_for(self.lock.acquire(), timeout=30.0)` so requests return a clean 429 Busy response rather than freezing indefinitely under load.

### 2.2. Prompt Response Caching (Disabled by Default)
* An in-memory SHA-256 prompt response cache with a 5-minute TTL.
* **Disabled by default (`ENABLE_RESPONSE_CACHE=false`):** For coding agents, freshest code output is required. Only enable for static Q&A workloads via environment variable.

### 2.3. 3-Tier Multimodal Vision Pipeline
When screenshots are passed via Base64:
1. **Pillow Optimization & Alpha Compositing:** Image dimensions are capped at 1600x1600 and compressed with JPEG 85% to protect RAM. Transparent images (RGBA/LA/paletted with transparency) are composited onto a white background before JPEG conversion. `Image.MAX_IMAGE_PIXELS = 50_000_000` prevents decompression bomb attacks.
2. **Tier 1 (Native Vision Upload):** Directly streamed to Gemini Web's file attachment API. If transient errors occur, uploads are paused for 5 minutes (`vision_disabled_until`) rather than disabled permanently.
3. **Tier 2 (3-Engine OCR Fallback):** If upload is blocked or fails:
   * **Engine 1 - RapidOCR:** Lightweight ONNX Runtime OCR.
   * **Engine 2 - Windows 11 Native WinRT OCR:** Zero-Python-memory extraction via Windows PowerShell `Windows.Media.Ocr`.
   * **Engine 3 - Tesseract OCR:** Cross-platform fallback on Linux/macOS with automatic fallback from `-l eng+vie` to `-l eng`.
   Extracted text is appended into the prompt context for native text analysis.
4. **Safe Temporary File Lifecycle:** Only bridge-generated files in `_TEMP_FILES` are removed upon turn completion.

### 2.4. Smart Truncation Engine
* **Tool Result Only:** User and assistant conversation content is never truncated; smart truncation applies exclusively to `role == "tool"` outputs exceeding `MAX_TOOL_RESULT_CHARS` (default 12,000 characters).
* Scans middle output for critical keywords (`Traceback`, `Error:`, `Exception:`, `fatal:`, etc.), preserves these in a protected `KEY ERRORS/WARNINGS PRESERVED` block, and safely handles any tail offsets.

### 2.5. Rail-Guard Sandbox & Firewall
* **Design Scope:** `app/security.py` operates as an internal developer rail guard for a single user, preventing common AI hallucination blunders; it is not an OS-level sandbox replacement.
* **Path Traversal Neutralization:** Uses `os.path.realpath(os.path.abspath(path))` relative to `DEFAULT_PROJECT_DIR` to prevent traversal attacks (e.g. `..\..\Windows\System32`) aimed at Drive C:\ or system folders (`WINDOWS`, `PROGRAM FILES`, `PROGRAMDATA`).
* **PowerShell Injection Filter:** Drops dangerous disk operations (`format <drive>:`), system registry tampering, environment variables (`$env:SystemDrive`, `$env:windir`), backtick escaping, `Invoke-Expression`, and .NET file API calls before execution reaches the agent. Safe CLI flags such as `prettier --format` are permitted.

### 2.6. Standard HTTP Status Code Mapping (OpenAI API Standard)
All internal conditions map to standard HTTP errors:
* **HTTP 429 (`rate_limit_error / bridge_busy`):** Concurrency lock timeout.
* **HTTP 502 (`upstream_error / gemini_upstream_error`):** Gemini Web connection or upstream processing failure.
* **HTTP 503 (`authentication_error / gemini_not_authenticated`):** Unauthenticated session or expired cookies (guest mode detected).
* **HTTP 504 (`timeout_error / gemini_timeout`):** Gemini Web request timed out past `GEMINI_HARD_TIMEOUT`.

### 2.6. Observability & File Logging
* Dual logging to stdout and rotating log files (`logs/bridge.log`, 5MB × 3 backups).
* Telemetry exposed via `GET /health` (uptime, throughput, error rates, cache statistics).

### 2.7. Component-Driven Dashboard (`frontend/`)
* Modular W3C Custom Elements and ES Modules without Node.js or bundlers.
* Declarative skeleton in `frontend/index.html` (~30 lines).
* Reactive store in `frontend/js/state.js`, modular API client in `frontend/js/api.js`, and i18n dictionary in `frontend/js/i18n.js`.

### 2.8. On-Demand Connection Control & Draining
* **Safe Default Startup:** Bridge starts in the `off` state by default (`API_START_STATE=off`). FastAPI and Dashboard remain online, but Gemini Web is completely disconnected without background token refresh loops running.
* **4-State Lifecycle:** `off` → `starting` → `on` → `stopping` → `off`.
* **Graceful In-flight Draining:** When STOP is clicked on the Dashboard:
  1. Bridge enters `stopping` state and rejects new requests immediately with HTTP 503 `bridge_disabled`.
  2. In-flight requests are awaited up to `DISABLE_DRAIN_TIMEOUT` seconds (default: 120s).
  3. Once all active requests finish (`_inflight == 0`), session state is cleanly saved to disk and the client is closed.
  4. If draining times out, bridge aborts disabling, stays `on`, and returns HTTP 409 `bridge_control_conflict` without terminating running requests.
* **Origin Guard & LAN Support:** Protects state-mutation endpoints (`/v1/control/enable`, `/v1/control/disable`) against unauthorized browser origins while natively allowing localhost, loopback, and private LAN IP ranges (RFC 1918) for multi-computer setups.
