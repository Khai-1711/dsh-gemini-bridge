# System Architecture, Algorithms & Research References (v3.2)

> 🌐 **Language:** **English** | [Tiếng Việt](ARCHITECTURE.vi.md) | [한국어](ARCHITECTURE.ko.md)  
> 📘 **README:** [English](../README.md) | [Tiếng Việt](README.vi.md) | [한국어](README.ko.md)

---

## 1. End-to-End Request Lifecycle

```text
[Coding Agent Client (DeepSeek Harness / pi-ai / OpenAI SDK)]
        │  POST /v1/chat/completions or /v1/responses (+ optional X-Workspace-Dir)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Frontend Gateway (:10000) — main.py + app/gateway_router.py             │
│  ├─ Security Middleware: Host anti-DNS-rebinding, API_KEY, X-Lane-Token │
│  ├─ Local Fast Path: app/title_generation.py (<5ms session titles)      │
│  ├─ Routing: Sticky Affinity (3600s) + Least-Inflight + Model Filtering │
│  ├─ Pre-SSE Failover: X-Gateway-Failover-Ready: 1 (2s fast-fail -> 429) │
│  └─ Profile Vault Control Plane: /v1/profiles/* (loopback-only CDP)     │
└──────────────┬───────────────────────────────────────┬──────────────────┘
               │ acc1 (:10001)                         │ acc2 (:10002)
               ▼                                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Worker OpenAI Adapter — app/openai_adapter.py                           │
│  ├─ Read-only model resolution before acquiring turn lock               │
│  ├─ Acquire turn lock BEFORE StreamingResponse(200) -> true 429 retry   │
│  ├─ Activate isolated SessionSlot (model, workspace_root, FactLedger)   │
│  ├─ app/prompt_builder.py: Delta prompt, Context Bootstrap, fair-share  │
│  │  parallel tool-result budgeting (36k), Head+Tail+Error truncation    │
│  └─ StreamingResponse + 2.5s ": keep-alive\n\n" SSE heartbeat           │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Worker Execution Core — app/gemini_runtime.py + app/account_pool.py     │
│  ├─ Deadlock-safe _safe_init_client (25s init cap / 420s stream cap)    │
│  ├─ Auto Cookie Sync: headless CDP refresh on UNAUTHENTICATED           │
│  ├─ Thinking-aware deadlines: 180s first-byte / 120s idle / 420s total  │
│  └─ 1-round empty-reply retry + 1097 recovery with Context Bootstrap    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Fail-Closed Protocol Gate — app/streaming.py + app/tool_parser.py       │
│  ├─ O(N) FSM ToolCallAwareStreamer (backtick-safe inline prose pass)    │
│  ├─ Gemini Web cleanup: URL unlinkify ([url](url)->url), <Image> strip  │
│  ├─ Deterministic json-repair + JSON Schema validation (500k limit)     │
│  ├─ 1-round internal self-correction before emitting invalid_tool_call  │
│  └─ Workspace/OS path & command guard (app/security.py)                 │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
                  [Client executes tools in active workspace]
```

---

## 2. Core Subsystems

### 2.1. Gateway Router (`app/gateway_router.py`)
- Activated automatically when `PEER_LANES` is configured and `GATEWAY_MODE` is `auto` (with empty/`default`/`frontend`/`gateway` `BRIDGE_INSTANCE`) or `on`.
- Combines **Sticky Session Affinity** (`STICKY_TTL=3600s`, `MAX_STICKY_SESSIONS=128`), **Least-Inflight Selection**, **Model-Capability Filtering** (checks each worker's `available_models`), and **Cooldown Circuit Breaking** (`15s` cooldown on `429`/`503`/`ConnectError`).
- Workers acquire their turn lock *before* returning `StreamingResponse(200)` with a `2.0s` fast-fail when `X-Gateway-Failover-Ready: 1` is present, enabling genuine SSE failover onto idle workers.

### 2.2. Per-Slot Session Isolation (`app/session_pool.py`, `app/gemini_runtime.py`)
- Each conversation key maps to an isolated `SessionSlot` (`16` slots, `3600s` TTL) storing its own `(cid/rid/rcid, model, workspace_root, tool_header_hash, FactLedger)`.
- Interleaved sessions using different models on the same worker never overwrite `default_model` or corrupt sibling slots.

### 2.3. Profile Vault & Headless CDP Cookie Sync (`app/cookie_sync.py`)
- Decouples unlimited Google accounts (`acc1`, `acc2`, `work3`, ...) into isolated Chrome/Edge `--user-data-dir` folders under `.cookie_profiles/` (gitignored, never touching the user's personal browser).
- **1-time login:** launches clean Chrome/Edge with zero automation flags so Google's normal 2FA/Passkey login works unmodified.
- **Headless refresh:** spawns `--headless=new --remote-debugging-port=0 --remote-debugging-address=127.0.0.1`, connects via a zero-dependency **RFC 6455 WebSocket** client (`CdpConnection`), navigates to `gemini.google.com/app`, extracts the full `.google.com` + `gemini.google.com` cookie jar (`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID` — excluding `accounts.google.com` `LSID`/`__Host-GAPS`), persists atomically to `.gemini_cookies_<lane>/.cached_cookies_<PSID>.json` and `.env.<lane>`, and flushes cleanly via `Browser.close`.
- Serialized via `asyncio.Semaphore(1)` so syncing N accounts peaks at 1 transient browser process (~2–4s each) and drops back to 0 MB RAM.

---

## 3. Algorithms & Data Structures Used

| Component | Algorithm / Pattern | Complexity | File |
|---|---|---|---|
| **SSE Tag Suppression** | Single-pass Finite-State Machine (FSM) with partial-prefix lookahead & inline-backtick context guard | $O(N)$ time, $O(1)$ lookahead | `app/streaming.py` |
| **Tool-Call Extraction & Repair** | String-aware bracket/quote scanner, outside-string trailing-comma removal, `json-repair`, recursive URL unlinkify, JSON Schema check | $O(N)$ single pass | `app/tool_parser.py` |
| **Bounded Self-Correction** | 1-round diagnostic feedback retry before fail-closed error emission | $O(1)$ bounded round | `app/openai_adapter.py` |
| **Smart Salience Truncation** | Head (2/3) + Tail (1/3) + middle error/traceback keyword extraction + proportional fair-share split across parallel tool results | $O(L)$ over lines | `app/prompt_builder.py` |
| **Gateway Load Balancing** | Sticky hash-table binding + Least-Inflight min-selection + temporal cooldown circuit breaker | $O(P)$ per request ($P$ = peers) | `app/gateway_router.py` |
| **Session Slot Pool** | Bounded LRU/TTL dictionary (`16` slots) with re-entrant task-owned `asyncio.Lock` | $O(1)$ lookup, $O(S)$ evict | `app/session_pool.py` |
| **CDP Transport** | Pure-stdlib RFC 6455 WebSocket framing (7/16/64-bit lengths, continuation reassembly, Ping/Pong) + event ring buffer | $O(B)$ bytes | `app/cookie_sync.py` |
| **Deadlock-Safe Client Init** | `asyncio.wait_for` timeout isolation separating init cap (`25s`) from long stream/thinking timeout (`420s`) | $O(1)$ wrapper | `app/account_pool.py` |
| **Atomic Config Persistence** | Write-to-temp-file + POSIX/Win32 atomic `os.replace()` for `.env*`, `vault.json`, and cookie jars | $O(F)$ file size | `app/env_update.py`, `app/cookie_sync.py` |

---

## 4. Referenced Research Papers & Technical Standards

1. **ReAct:** Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., & Cao, Y. (2023). *ReAct: Synergizing Reasoning and Acting in Language Models.* ICLR 2023. — Foundations for interleaving model thoughts (`thoughts_delta`), structured tool invocations, and tool observation turns.
2. **Toolformer:** Schick, T., Dwivedi-Yu, J., Dessì, R., Raileanu, R., Lomeli, M., Zettlemoyer, L., Cancedda, N., & Scialom, T. (2023). *Toolformer: Language Models Can Teach Themselves to Use Tools.* NeurIPS 2023. — Basis for in-band text-delimited tool-call markup (`<tool_call>...</tool_call>`) over non-function-calling web endpoints.
3. **Reflexion & Self-Refine:** Shinn, N. et al. (2023), *Reflexion: Language Agents with Verbal Reinforcement Learning* (NeurIPS 2023); Madaan, A. et al. (2023), *Self-Refine: Iterative Refinement with Self-Feedback* (NeurIPS 2023). — Motivates our bounded 1-round diagnostic self-correction loop on malformed tool calls.
4. **Lost in the Middle:** Liu, N. F., Lin, K., Hewitt, J., Paranjape, A., Bevilacqua, M., Petroni, F., & Liang, P. (2024). *Lost in the Middle: How Language Models Use Long Contexts.* TACL 2024. — Motivates `smart_truncate()` preserving head, tail, and error-salient middle lines rather than naive tail-dropping.
5. **Load Balancing & Resilience Engineering:** Mitzenmacher, M. (2001), *The Power of Two Choices in Randomized Load Balancing* (IEEE TPDS); Nygard, M. (2018), *Release It!* (Circuit Breaker & Bulkhead patterns). — Motivates Least-Inflight routing, per-lane cooldowns, and isolated worker processes.
6. **Protocols & Standards:**
   - **RFC 6455** — *The WebSocket Protocol* (implemented directly in `app/cookie_sync.py` for zero-dependency local CDP).
   - **RFC 8259 / JSON Schema Draft 2020-12** — Strict tool argument validation (`app/tool_parser.py`).
   - **Chrome DevTools Protocol (CDP)** — `Storage.getCookies`, `Target.createTarget`, `Page.loadEventFired`, `Runtime.evaluate`, `Browser.close`.
   - **WHATWG Server-Sent Events (SSE)** — Chunked OpenAI streaming with `: keep-alive\n\n` heartbeats.
