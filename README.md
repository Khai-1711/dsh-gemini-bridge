# DSH Gemini Agent Bridge (v3.2)

> 🌐 **Language:** **English** | [Tiếng Việt](docs/README.vi.md) | [한국어](docs/README.ko.md)  
> 📐 **Architecture, Algorithms & Research References:** [English](docs/ARCHITECTURE.en.md) | [Tiếng Việt](docs/ARCHITECTURE.vi.md) | [한국어](docs/ARCHITECTURE.ko.md)

**DSH Gemini Agent Bridge** is a modular, OpenAI-compatible (`/v1/models`, `/v1/chat/completions`, `/v1/responses`) reverse proxy and multi-lane execution engine that connects coding agents (DeepSeek Harness / `pi-ai`, IDE agents, OpenAI SDK clients) to **Gemini Web** with real SSE streaming, multi-account failover, isolated browser Profile Vault auto-sync, and fail-closed tool calling.

---

## Key Capabilities

- **Multi-Lane Frontend Gateway (`:10000` → `:10001`, `:10002`, ...):** Sticky conversation affinity, least-inflight routing, model-aware lane selection, pre-SSE turn-lock admission control (`HTTP 429` real failover before opening `200 text/event-stream`), and zero-buffer SSE pass-through.
- **100% User-Chosen Models & Per-Slot Isolation:** Exposes every model available on your Gemini account with zero hardcoded preferences or hidden models. Each `SessionSlot` owns its own model and workspace context; `FALLBACK_MODEL` is strictly opt-in.
- **Open Workspace Support:** Automatically follows whatever project directory is open in your IDE/chat (`X-Workspace-Dir` or prompt context) across any drive (`C:`, `D:`, `E:`, ...), blocking only destructive OS-level paths/commands (`System32`, UNC, `format`, `diskpart`, `IWR | IEX`, etc.). Supports `WORKSPACE_SANDBOX_MODE=client|workspace`.
- **Profile Vault & Headless Auto Cookie Sync (0 Open Tabs):** Manage unlimited Google accounts (`acc1`, `acc2`, `work3`, ..., `workN`) without storing passwords or keeping browser tabs open. Sign in manually once in a clean isolated Chrome/Edge profile; future refreshes run silently via `--headless=new` + stdlib RFC 6455 CDP WebSocket in ~2–4s, exporting the full Google/Gemini cookie jar (`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID`) and hot-reloading workers automatically.
- **Fail-Closed Tool Calling & Self-Correction:** $O(N)$ prefix-holding SSE streamer, inline-backtick prose safety, Gemini Web URL auto-linkification reversal (`[http://...](http://...)` → `http://...`), deterministic `json-repair`, 1-round internal self-correction before emitting `invalid_tool_call`, and 1-round empty-reply retry.
- **Completion-First Thinking Budgets:** Generous defaults (`GEMINI_HARD_TIMEOUT=180s`, `GEMINI_CHUNK_IDLE_TIMEOUT=120s`, `GEMINI_STREAM_OVERALL_TIMEOUT=420s`, `2.5s` SSE keep-alive) paired with a deadlock-safe `25s` init guard (`_safe_init_client`) and proportional fair-share budgeting across parallel tool results (`MAX_TOTAL_TOOL_RESULTS_CHARS=36000`).
- **Live Web Dashboard (`/dashboard`):** Bilingual VI/EN SPA with real-time lane switches, model picker, Profile Vault manager (`Login Once`, `Done & Sync`, `Sync`, `Sync All`), live metrics, and tail logs.

---

## Repository Structure

```text
dsh-gemini-bridge/
├── app/                     # Core runtime, Gateway router, CDP Profile Vault, tool parser, security
├── docs/                    # Multilingual READMEs & Architecture / Algorithms / Research docs (EN/VI/KO)
├── frontend/                # Zero-build Web Components Dashboard (/dashboard)
├── scripts/                 # CLI tools (cookie_sync.py, live_smoke.py) & single-lane launchers/
├── tests/                   # 230 offline-isolated unit & integration tests
├── main.py                  # FastAPI entrypoint (Gateway or Worker depending on BRIDGE_INSTANCE)
├── requirements.txt         # Minimal Python dependencies
├── run_all.bat              # One-click Windows launcher (Gateway :10000 + Workers :10001, :10002)
├── stop_all.bat             # One-click Windows stopper (ports 10000..10002)
└── .env.example             # Full configuration reference
```

---

## Quick Start

### 1. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 2. Configure Environment Files
Copy `.env.example` into `.env` (Gateway on `:10000`) and `.env.acc1`, `.env.acc2` (Workers on `:10001`, `:10002`):
- Set a shared `LANE_ADMIN_TOKEN` across `.env`, `.env.acc1`, and `.env.acc2`.
- In `.env`, configure `PEER_LANES="acc1=http://127.0.0.1:10001,acc2=http://127.0.0.1:10002"`.

### 3. Sign In Once per Google Account (Profile Vault)
No DevTools copy-paste or permanently open tabs required:
```powershell
python scripts/cookie_sync.py login acc1 --lane acc1 --slot 0 --label "Work 1"
python scripts/cookie_sync.py login acc2 --lane acc2 --slot 0 --label "Work 2"
# Add backup failover accounts (slots 1..4) or extra lanes anytime:
python scripts/cookie_sync.py login work3 --lane acc1 --slot 1 --label "Backup 1"
```
Refresh all accounts silently anytime (0 visible browser windows):
```powershell
python scripts/cookie_sync.py sync-all
python scripts/cookie_sync.py list
```

### 4. Start & Monitor
```powershell
.\run_all.bat
# Open Dashboard: http://127.0.0.1:10000/dashboard
# OpenAI Base URL: http://127.0.0.1:10000/v1
# Stop all processes:
.\stop_all.bat
```

---

## Key Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GATEWAY_MODE` | `auto` | `auto` \| `on` \| `off` — enables Frontend Gateway when `PEER_LANES` is set |
| `DEFAULT_MODEL` | `""` | Optional default model; empty keeps account/Dashboard selection |
| `FALLBACK_MODEL` | `""` | Opt-in fallback model; empty never silently switches away from user choice |
| `WORKSPACE_SANDBOX_MODE` | `client` | `client` (follow IDE workspace on any drive) \| `workspace` (soft-lock to chat dir) |
| `AUTO_COOKIE_SYNC` | `true` | Auto-recover expired worker cookies silently from Profile Vault |
| `GEMINI_HARD_TIMEOUT` | `180` | Max seconds waiting for first thought/text chunk |
| `GEMINI_CHUNK_IDLE_TIMEOUT` | `120` | Max seconds between consecutive chunks during deep thinking |
| `GEMINI_STREAM_OVERALL_TIMEOUT` | `420` | Overall turn ceiling (7 minutes) |
| `MAX_TOTAL_TOOL_RESULTS_CHARS` | `36000` | Fair-share character budget across parallel `role: "tool"` batches |
| `TOOL_PARSE_SELF_CORRECTION` | `true` | 1-round internal self-correction on malformed `<tool_call>` |

---

## Verification & Quality Gates

```powershell
# Run all 230 offline-isolated unit & integration tests
python -B -m unittest discover -s tests

# Static lint check (Pyflakes + Syntax + Bugbear)
ruff check --no-cache --select F,E9,B main.py app tests scripts
```

---

## Security Notes
- `.env*`, `.cookie_profiles/`, `.gemini_cookies*/`, `logs/`, and `.bridge_*.json` are strictly gitignored.
- Profile Vault endpoints (`/v1/profiles/*`) enforce both Origin validation and loopback-only (`127.0.0.1`) access.
- `HOST` defaults to `127.0.0.1`; binding to `0.0.0.0` is refused at startup unless `API_KEY` is configured.
