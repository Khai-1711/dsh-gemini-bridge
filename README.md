# DSH Gemini Agent Bridge (v3.0 - Enterprise Modular Edition)

> 🌐 **Language:** **English** | [Tiếng Việt](README_VI.md)

> **High-Reliability OpenAI-Compatible Reverse Proxy & Agent Execution Bridge for Gemini Web (`gemini_webapi`) on DeepSeek Harness (`pi-ai`), Cline, and Roo Code.**

This project provides an enterprise-grade middleware bridge transforming the **Google Gemini Web** interface into a production-ready **OpenAI Chat Completions API** backend with full support for **Function Calling (Tool Use)**, **Multimodal Vision (3-Tier Fallback)**, **Stateful Single-Session Execution**, **Cookie Failover Pool**, and an **Observability Web Dashboard**.

---

## Technical Highlights (v3.0)

1. **Zero-Spam Single ChatSession:** Consolidates the Agent's entire action cycle (Read → Glob → Edit → Pwsh) into **exactly one persistent chat thread** on Gemini Web using incremental Delta streaming. Reduces prompt token overhead by 90% and shields your Google account from anti-spam flagging.
2. **Anti-Timeout SSE Heartbeat:** Emits periodic `: keep-alive\n\n` pulses every 2.5 seconds over Server-Sent Events (SSE), eliminating client idle stream timeouts and infinite `[Recovery]` loops.
3. **Cookie Failover Pool & Hot-Reload:** Automatically detects primary cookie expiration and fails over seamlessly to a standby backup account (`BACKUP_1PSID`), or accepts new cookies at runtime via `POST /v1/update-cookies` without restarting the server.
4. **Queue Lock with Timeout:** Guards access with `asyncio.Lock(timeout=30s)` to prevent race collisions and server overload, returning an immediate busy status (HTTP 429) rather than hanging indefinitely.
5. **Prompt Response Cache:** In-memory SHA-256 response cache with a 5-minute TTL to serve identical prompts in under 1ms, saving quota and latency.
6. **Smart Truncation Engine:** Intelligently protects critical error messages, stack traces, and line references when tool execution results exceed character limits.
7. **3-Tier Multimodal Vision Pipeline:** Automatic Base64 image extraction, Pillow JPEG optimization, native Gemini file upload, and a 3-tier OCR fallback chain (`RapidOCR` → `Windows 11 Native WinRT OCR` → `Tesseract OCR`).
8. **Hardened C-Drive Firewall:** Complete path traversal neutralization via `os.path.realpath` and strict regex sanitization of dangerous PowerShell environment variables, escape tricks, and reflection attacks.
9. **Component-Driven Web Dashboard:** A modular observability console built with **W3C Custom Elements** and **ES Modules** (no Node.js or build tools required) with dark/light themes and bilingual EN/VI support.

---

## Directory Structure

```
dsh_gemini_bridge/
├── app/                          # Backend Core Modules
│   ├── __init__.py               # Core package initialization
│   ├── config.py                 # Central config, fail-fast startup validator & rotating logger
│   ├── security.py               # Drive sandbox jail (realpath traversal & shell firewall)
│   ├── vision.py                 # Multimodal pipeline, image compression & 3-engine OCR fallback
│   ├── prompt_builder.py         # Tool compiler, Smart Truncation & Language Matching
│   ├── tool_parser.py            # Linear O(N) <tool_call> parser & argument auto-mapper
│   └── gemini_session.py         # Stateful session manager, queue lock, failover & cache
├── frontend/                     # 🧩 Component-Driven Web Dashboard (W3C Standard)
│   ├── index.html                # Declarative semantic skeleton (~30 lines)
│   ├── css/
│   │   ├── variables.css         # Design tokens, color system & dark/light themes
│   │   ├── terminal.css          # Dedicated terminal console & syntax styling
│   │   └── main.css              # Main responsive layout, grid & animations
│   └── js/
│       ├── api.js                # HTTP Client service layer
│       ├── state.js              # Reactive state store & Event Bus
│       ├── i18n.js               # Internationalization dictionary (EN / VI)
│       ├── app.js                # Application bootstrap entrypoint
│       └── components/           # Autonomous Custom Elements
│           ├── BridgeNavbar.js        # <bridge-navbar> (Logo, ping, EN/VI & Dark/Light toggle)
│           ├── BridgeMetrics.js       # <bridge-metrics> (4 real-time telemetry cards)
│           ├── BridgeTerminal.js      # <bridge-terminal> (Log console with filters & search)
│           ├── BridgeModelSwitch.js   # <bridge-model-switch> (Runtime Gemini model switcher)
│           ├── BridgeCookieManager.js # <bridge-cookie-manager> (Cookie hot-reload & masked inputs)
│           ├── BridgeQuickActions.js  # <bridge-quick-actions> (Session reset & cache wipe)
│           └── BridgeToast.js         # <bridge-toast> (Floating notification system)
├── logs/                         # Auto-rotating server logs (5MB × 3 backups)
├── main.py                       # FastAPI entrypoint, auth middleware & static file hosting
├── .env.example                  # Environment configuration template
├── requirements.txt              # Python dependencies
├── ARCHITECTURE.md               # In-depth architectural design (English)
├── ARCHITECTURE_VI.md            # In-depth architectural design (Tiếng Việt)
├── README.md                     # Documentation (English)
└── README_VI.md                  # Documentation (Tiếng Việt)
```

---

## Quick Start Guide

### 1. Install Dependencies
Requires Python `>= 3.10`. In the project directory, run:

```bash
pip install -r requirements.txt
```

*(Optional for enhanced OCR and image compression)*:
```bash
pip install rapidocr-onnxruntime pillow
```

### 2. Configure Environment (`.env`)
Copy `.env.example` to `.env`:

```powershell
Copy-Item .env.example .env
```

Open `.env` and fill in your Gemini Web cookies (obtained from browser DevTools: F12 → **Application** → **Cookies** → `https://gemini.google.com`):

```ini
# [REQUIRED] Primary Account Cookies
SECURE_1PSID=g.a000...
SECURE_1PSIDTS=sidts-...

# [OPTIONAL] Backup Account Cookies (Automatic Failover)
BACKUP_1PSID=
BACKUP_1PSIDTS=

# [OPTIONAL] API Key Protection (leave blank for local-only trust)
API_KEY=

# Working directory on Drive D
DEFAULT_PROJECT_DIR=D:\ABC
```

### 3. Grant Directory Permissions on Drive D
Allow the Agent's sandboxed `Pwsh` tool to execute without administrator privileges:

```powershell
icacls "D:\ABC" /grant "$($env:USERNAME):(OI)(CI)F" /T
```

### 4. Start the Bridge Server
```bash
python main.py
```

Upon successful initialization:
```
2026-11-14 12:00:00 | INFO    | [DSH-Bridge] Starting Gemini WebClient...
2026-11-14 12:00:02 | INFO    | [DSH-Bridge] Detected Models: ['unspecified', 'gemini-flash']
2026-11-14 12:00:02 | INFO    | [DSH-Bridge] SingleChatManager ready! Active: [gemini-flash]
INFO:     Uvicorn running on http://127.0.0.1:10000 (Press CTRL+C to quit)
```

Open your browser to access the Web Dashboard:
👉 **`http://127.0.0.1:10000/dashboard`**

---

## DeepSeek Harness (pi-ai) Integration

In DeepSeek Harness, navigate to **Settings → Model Providers**:

| Field            | Setting Value                                                   |
|------------------|-----------------------------------------------------------------|
| **API Protocol** | `OpenAI Chat Completions`                                       |
| **Base URL**     | `http://127.0.0.1:10000/v1`                                     |
| **API Key**      | Any string (or matching your `API_KEY` from `.env`)             |
| **Model ID**     | An exact ID returned by the `/v1/models` endpoint                |

---

## Multi-Instance Isolation (Run 2 Concurrent Agents)

The bridge supports running up to 2 completely isolated DSH agents from the **same repository** on separate ports, each bound to its own Google account and state files:

### Instance Identification Matrix

| ID | Account | Port | DSH Agent | Config File | Session State | Process Log | Activity Journal |
|---|---|---|---|---|---|---|---|
| `acc1` | Google A | 10001 | DSH 1 (project 1) | `.env.acc1` | `.bridge_session_acc1.json` | `logs/bridge_acc1.log` | `logs/activity_acc1.jsonl` |
| `acc2` | Google B | 10002 | DSH 2 (project 2) | `.env.acc2` | `.bridge_session_acc2.json` | `logs/bridge_acc2.log` | `logs/activity_acc2.jsonl` |
| *(default/gateway)* | - | 10000 | Dashboard / gateway | `.env` | `.bridge_session.json` | `logs/bridge.log` | `logs/activity.jsonl` |

### Configuration & Startup

1. **Create Configuration Files:**
   - Copy `.env.example` to `.env.acc1` (`PORT=10001`, Google Account A cookies, `INSTANCE_LABEL="Agent-1"`).
   - Copy `.env.example` to `.env.acc2` (`PORT=10002`, Google Account B cookies, `INSTANCE_LABEL="Agent-2"`).
2. **Launch Instances:**
   - Double-click `run_acc1.bat` to launch `acc1` on port 10001.
   - Double-click `run_acc2.bat` to launch `acc2` on port 10002.
3. **Connect DeepSeek Harness:**
   - Agent 1: Base URL `http://127.0.0.1:10001/v1`
   - Agent 2: Base URL `http://127.0.0.1:10002/v1`
   - Use an exact model ID returned by that worker's `GET /v1/models`. Port 10000 is the gateway/dashboard, not a worker chat endpoint.
4. **Monitor Activity Separately:**
   - Activities: `http://127.0.0.1:10001/v1/activity` and `http://127.0.0.1:10002/v1/activity`
   - Dashboard: `http://127.0.0.1:10000/dashboard` (gateway) or the relevant worker port.
5. **CRITICAL ACCOUNT NOTE:**
   - Do NOT share cookies between instances. Log in to each Google account in separate Chrome profiles or Incognito windows, copy the cookies, and close the browser. Verify `account_key` differs at `/v1/status` on ports 10001 and 10002.

---

## Management API Endpoints

| Method   | Endpoint                | Description                                                                          |
|----------|-------------------------|--------------------------------------------------------------------------------------|
| **POST** | `/v1/chat/completions`  | Main endpoint processing conversations, image analysis, and tool calls.              |
| **GET**  | `/v1/models`            | Dynamic list of Gemini models discovered on your Google account.                     |
| **GET**  | `/health`               | Telemetry endpoint: Uptime, total requests, error rates, cache metrics.              |
| **GET**  | `/dashboard`            | Web-based management console.                                                        |
| **GET**  | `/v1/control`           | Get API switch state (`off`, `starting`, `on`, `stopping`, `drain_timeout`).          |
| **POST** | `/v1/control/enable`    | Connect to Gemini Web and enable requests.                                           |
| **POST** | `/v1/control/disable`   | Gracefully drain in-flight requests and disconnect Gemini Web.                        |
| **POST** | `/v1/update-cookies`    | Hot-reload cookies at runtime without restarting the server.                         |
| **POST** | `/v1/switch-model`      | Switch the active Gemini model (`gemini-flash`, `gemini-pro`, etc.).                 |
| **POST** | `/v1/reset`             | Terminate current chat session on Gemini Web and start fresh.                        |
| **POST** | `/v1/cache/clear`       | Invalidate all cached prompt responses.                                              |
| **GET**  | `/v1/logs`              | Retrieve recent log lines from the instance's log file.                              |
| **GET**  | `/v1/activity`          | Dedicated JSONL activity journal for this instance.                                  |

---

## Dashboard API Control Switch (On/Off)

The bridge features an on-demand connection switch:
- **Starts in OFF state by default:** The web server and dashboard start immediately, but **NO** connection to Gemini Web is established and **NO** background cookie refresh tasks run until explicitly enabled.
- **Independent switches per instance:** Instance `acc1` (`:10001/dashboard`) and `acc2` (`:10002/dashboard`) maintain completely independent control switches. The gateway dashboard at `:10000/dashboard` can control both workers. Control state is memory-only and initialized on startup based on `API_START_STATE`.

### State Lifecycle Matrix

| State | Gemini Connection | `/v1/chat/completions` | Dashboard / `/health` |
|---|---|---|---|
| `off` | NOT initialized | HTTP 503 (`code: "bridge_disabled"`) | Fully accessible |
| `starting` | Initializing client | HTTP 503 (`code: "bridge_disabled"`) | Fully accessible |
| `on` | Initialized & Ready | Processed normally | Fully accessible |
| `stopping` | Draining / Disconnecting | HTTP 503 (`code: "bridge_disabled"`) | Fully accessible |

### Configuration Variables (`.env`)

```ini
# Startup state: off (default) = wait for manual enable on dashboard; on = auto-connect
API_START_STATE=off

# Maximum seconds to wait for in-flight requests to drain when disabling (default: 120s)
DISABLE_DRAIN_TIMEOUT=120

# Total deadline per image request, including upload, OCR fallback, and response (default: 90s, maximum: 120s).
# Direct image processing uses at most half this budget, preserving time for OCR/text fallback.
IMAGE_REQUEST_TIMEOUT=90
```

### LAN / Dual-Computer Support
When hosting the bridge on one computer and accessing the dashboard from another on your local network:
- Configure `HOST=0.0.0.0` in `.env` to bind on all network interfaces.
- The control origin guard automatically permits private RFC 1918 LAN IP ranges (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`), preventing HTTP 403 `origin_not_allowed` errors while still rejecting external malicious websites.

---

## Troubleshooting

0. **Agent receives HTTP 503 `bridge_disabled`:**
   * Open the dashboard (`http://127.0.0.1:10000/dashboard` or `:10001/dashboard`) and click **START / BẬT**. Once the green **ON / BẬT** badge appears, retry your request.

1. **Handling Guest Mode (Unauthenticated):**
   * **Symptoms:** Dashboard displays a red `GUEST (UNAUTHENTICATED)` badge, and API requests return HTTP 503 with error code `{"error": {"code": "gemini_not_authenticated", "message": "Gemini chưa đăng nhập..."}}`.
   * **Root Cause:** Cookie `SECURE_1PSID` or `SECURE_1PSIDTS` has expired or was revoked by Google. `gemini-webapi` 2.x silently falls back to an unauthenticated Guest session, but the bridge explicitly intercepts and blocks guest mode to protect your chat history and ensure Gemini Pro features remain functional.
   * **Resolution:**
     1. Open Chrome → go to [https://gemini.google.com](https://gemini.google.com) and ensure you are logged in.
     2. Press `F12` → **Application** tab → **Storage** → **Cookies** → `https://gemini.google.com`.
     3. Copy values for `__Secure-1PSID` and `__Secure-1PSIDTS`.
     4. Open Bridge Dashboard (`http://127.0.0.1:10000/dashboard`) → paste them into **Cookie Manager (Hot-Reload)** → click **Save & Update**. Alternatively send via `POST /v1/update-cookies`. No restart required.

2. **`[CONFIG ERROR] SECURE_1PSID is empty!` at startup:**
   * You haven't created your `.env` file yet. Copy `.env.example` to `.env` and populate your cookies.
3. **`COOKIE MAY BE EXPIRED!` Warnings in Logs:**
   * Your browser session cookies have expired. Get fresh cookies from `gemini.google.com` and paste them into the Dashboard UI or send via:
     ```bash
     curl -X POST http://127.0.0.1:10000/v1/update-cookies -H "Content-Type: application/json" -d "{\"SECURE_1PSID\":\"...\",\"SECURE_1PSIDTS\":\"...\"}"
     ```
4. **Agent tool fails with missing properties:**
   * The Auto-Mapper in `app/tool_parser.py` automatically normalizes common parameter variations (e.g. `path` → `file_path`, `cmd` → `command`, `filePath` ↔ `file_path`). Extend `normalize_tool_arguments()` if adding custom tools.

---

## Recent Updates

- **Smart Fallback & Anti-Hang Recovery (`app/gemini_session.py`, `main.py`):**
  - Prioritizes `gemini-4-hydrogen` and prevents `MODEL_FALLBACK` from looping back onto the same failing model.
  - On Error `1097` or if a stream produces no first byte within `35s`, automatically falls back to `unspecified` (`model=None`) and validates `_evaluate_auth()` so expired cookies fail loudly instead of silently dropping into Guest Mode.
  - Sanitizes and rolls back incomplete `[cid, rid, None]` metadata on timeouts/cancellations and auto-resets `ChatSession` when DSH starts a new conversation.
- **External Text Tool Protocol & Active Local AI Repair (`app/prompt_builder.py`, `app/tool_repair.py`, `app/tool_parser.py`):**
  - Compacted tool schema instructions and explicitly framed `<tool_call>` as an external client plain-text XML protocol so Gemini Web continues emitting tools across multi-turn `[TOOL_RESULT]` loops.
  - Enabled active JSON syntax repair via Local AI (`Nemotron`, `TOOL_REPAIR_ENABLED=true`, up to `16,000` chars / `4,096` output tokens) while preserving valid sibling calls in parallel tool-call responses.
- **Windows Multi-Lane & Testing (`app/lanes.py`, `run_all.bat`, `tests/`):**
  - Switched local lane spawning to `CREATE_NO_WINDOW` for Python 3.14 compatibility and fixed `run_all.bat` environment variable spacing.
  - Verified with **198 unit tests**: `python -m unittest discover -s tests -q`.
