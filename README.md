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
| **Model ID**     | `gemini-flash`                                                  |

---

## Management API Endpoints

| Method   | Endpoint                | Description                                                                          |
|----------|-------------------------|--------------------------------------------------------------------------------------|
| **POST** | `/v1/chat/completions`  | Main endpoint processing conversations, image analysis, and tool calls.              |
| **GET**  | `/v1/models`            | Dynamic list of Gemini models discovered on your Google account.                     |
| **GET**  | `/health`               | Telemetry endpoint: Uptime, total requests, error rates, cache metrics.              |
| **GET**  | `/dashboard`            | Web-based management console.                                                        |
| **POST** | `/v1/update-cookies`    | Hot-reload cookies at runtime without restarting the server.                         |
| **POST** | `/v1/switch-model`      | Switch the active Gemini model (`gemini-flash`, `gemini-pro`, etc.).                 |
| **POST** | `/v1/reset`             | Terminate current chat session on Gemini Web and start fresh.                        |
| **POST** | `/v1/cache/clear`       | Invalidate all cached prompt responses.                                              |
| **GET**  | `/v1/logs`              | Retrieve recent log lines from `logs/bridge.log`.                                    |

---

## Troubleshooting

1. **`[CONFIG ERROR] SECURE_1PSID is empty!` at startup:**
   * You haven't created your `.env` file yet. Copy `.env.example` to `.env` and populate your cookies.
2. **`COOKIE MAY BE EXPIRED!` Warnings in Logs:**
   * Your browser session cookies have expired. Get fresh cookies from `gemini.google.com` and paste them into the Dashboard UI or send via:
     ```bash
     curl -X POST http://127.0.0.1:10000/v1/update-cookies -H "Content-Type: application/json" -d "{\"SECURE_1PSID\":\"...\",\"SECURE_1PSIDTS\":\"...\"}"
     ```
3. **Agent tool fails with missing properties:**
   * The Auto-Mapper in `app/tool_parser.py` automatically normalizes common parameter variations (e.g. `path` → `file_path`, `cmd` → `command`). Extend `normalize_tool_arguments()` if adding custom tools.
