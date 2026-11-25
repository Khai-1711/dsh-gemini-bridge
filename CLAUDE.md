# CLAUDE.md — Context & Engineering Guide for `dsh_gemini_bridge`

> **For full technical details, user pain points, root causes, live telemetry, and handoff checklist, read:**
> `docs/AGENT_ENGINEERING_CASE_STUDY.md`

---

## 1. Project Overview

`dsh_gemini_bridge` is an OpenAI-compatible SSE proxy & multi-lane Gateway (`FastAPI` + `gemini_webapi`) that turns Google AI Pro (Gemini Web) into a stateful, data-safe Coding Agent backend for **DeepSeek Harness (DSH)** and OpenAI-compatible IDEs.

### Runtime Topology (Windows Host)
- **Frontend Gateway (`127.0.0.1:10000`):** `main.py` + `app/gateway_router.py` (`INSTANCE_ID=""`, `Frontend`) — performs sticky least-inflight routing and broadcasts `/v1/tool-profile` changes to worker lanes with `X-Lane-Token`.
- **Worker Lanes:**
  - `acc1` (`127.0.0.1:10001`, `BRIDGE_INSTANCE=acc1`)
  - `acc2` (`127.0.0.1:10002`, `BRIDGE_INSTANCE=acc2`)
- **DSH Desktop Integration:**
  - Profile override: `C:\Users\USER\.dsh\profiles\desktop\cordis.patch.yml` (points `gemini4` to `http://127.0.0.1:10000/v1`; `agent-default-model: gemini-carbon`; `subagent-model-selection-settings` allows `gemini-carbon` and `gemini-4-hydrogen`).
- **Zero-Screenshot Desktop Control (`Windows-MCP`):**
  - `windows-mcp==0.9.0` installed via `uv tool install --python 3.14 --compile-bytecode windows-mcp` (`C:\Users\USER\.local\bin\windows-mcp.exe serve`), registered in `C:\Users\USER\.gemini\config\mcp_config.json`.
  - Electron apps (`DeepSeek Harness.exe`) require `--force-renderer-accessibility` and `Snapshot(use_vision=false, use_dom=false)` to expose full UIA controls without screenshots.

---

## 2. Key Commands (Windows PowerShell)

Run from `d:\GG_API_TEST\gemini\dsh_gemini_bridge`:

```powershell
# Note: tests/ and scripts/live_smoke.py were intentionally removed per user preference
# to avoid mock/synthetic tests. Verify changes with ruff, py_compile, and live DSH runs.

# Run linter (syntax, undefined names, bugbear)
ruff check --no-cache --select F,E9,B main.py app scripts

# Quick Python bytecode syntax check
python -B -m py_compile main.py

# Start / stop live bridge (Gateway + acc1 + acc2)
.\run_all.bat
.\stop_all.bat

# Inspect or hot-swap active tool profile
Invoke-RestMethod http://127.0.0.1:10000/v1/tool-profile
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:10000/v1/tool-profile `
  -ContentType 'application/json' -Body '{"profile":"coding","persist":true}'
```

---

## 3. Core Code Map (`app/`)

| Module | Responsibility |
| :--- | :--- |
| `app/tool_profiles.py` | Pure profile filtering (`no_subagents`, `coding`, `coding_parallel`, `research`, `none`, `all`), `filter_tools`, `extract_forced_tool_names`. |
| `app/prompt_builder.py` | System/DSH boilerplate stripping, U-shaped prompt layout, natural greeting/chat detection (`is_plain_greeting_request`, `is_conversational_chat_request`), `compile_tools_prompt`, `smart_truncate`. |
| `app/tool_parser.py` | XML `<tool_call>` parser, prose fence exclusion (`_find_prose_code_fence_ranges`, `_scan_tool_call_blocks`), lossless heuristic repair guard (`_verify_lossless_heuristic_repair`), `.md`/`.rst` self-link preservation. |
| `app/streaming.py` | `ToolCallAwareStreamer` — suppresses live `<tool_call>` streaming while letting prose/code-fence examples stream safely. |
| `app/openai_adapter.py` | `/v1/chat/completions` endpoint, tool self-correction loop (`_attempt_tool_self_correction`), bounded SSE cancel wait (`3.0s`) + stuck turn-lock release. |
| `app/gemini_runtime.py` | `SingleChatManager` lifecycle, stream execution, `force_release_stuck_turn_lock` (`_turn_epoch`), transient non-auth `502` retry keeping `GeminiClient` & `saved_meta_before_send`, `FALLBACK_MODEL` soft fallback. |
| `app/session_pool.py` | Sticky multi-session pool (`compute_session_key`), same-`cid` model switches keeping `tool_header_sent`, lead session preservation across IDE compaction (`[Conversation Summary]`), atomic `.bridge_state/` persistence. |
| `app/gateway_router.py` | Sticky least-inflight routing across `acc1`/`acc2` and tool-profile header/API propagation. |
| `app/routes_admin.py` | Dashboard/control APIs including `GET/POST /v1/tool-profile`. |
| `app/cookie_sync.py` | Profile Vault CDP auto-sync for Gemini Web cookies. |

---

## 4. Hard Engineering Invariants (Do Not Regress)

1. **Lossless Data Integrity:**
   - Never run blind `json_repair` on mutation/exec tools (`write`, `edit`, `pwsh`, etc.): `_verify_lossless_heuristic_repair` must reject any repair that invents undeclared keys or drops alphanumeric tokens.
   - Preserve Markdown self-links `[https://...](https://...)` when `file_path` ends with `.md`, `.mdx`, `.markdown`, or `.rst`.
   - Keep `<tool_call>` inside prose fenced code blocks ignored symmetrically in both `app/streaming.py` and `app/tool_parser.py`.
2. **Turn-Lock & Retry Safety:**
   - Never block `sse_generator` `finally:` indefinitely on cancelled upstream streams; bump `_turn_epoch` and detach `chat_session` from `_last_safe_metadata` on forced release.
   - On `attempt == 0`, non-auth transient errors (`502`, connection resets) when `auth_ok=True` MUST keep `GeminiClient` alive and retry with `saved_meta_before_send` instead of calling `_recover_client_after_error`.
3. **Chat vs Work Separation & Session Continuity:**
   - Plain greetings and conceptual Q&A must defer `WORKSPACE TOOLS` (`tools_map={}` on greetings) and handle DSH multimodal text lists `[{"type": "text", "text": "..."}]`.
   - Switching models on an existing `cid` (`keep_chat=True`) must keep `tool_header_sent=True` when the header hash matches.
   - IDE context compaction must reuse the active lead `ChatSession` for the same `workspace_root`.
4. **Atomic Writes & Secret Hygiene:**
   - Persist `.bridge_state/*`, `.env*`, and external configs atomically (`tmp + os.replace`, UTF-8 **without BOM**).
   - Never log, commit, or expose `.env*`, `SECURE_1PSID*`, `API_KEY`, `LANE_ADMIN_TOKEN`, `vault.json`, or `cordis.patch.yml` provider secrets.
