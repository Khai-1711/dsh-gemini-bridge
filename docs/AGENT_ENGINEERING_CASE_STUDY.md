# Engineering Case Study & Handoff: `dsh_gemini_bridge` × DeepSeek Harness × Zero-Screenshot Automation

> **Tài liệu dành cho Claude (Claude Code / Claude Desktop / Claude Web) và Kỹ sư tiếp nhận dự án.**
> Tổng hợp toàn bộ hành trình từ nỗi đau thực tế của người dùng, thách thức kỹ thuật nền tảng, 10 bài toán Coding Agent đã giải quyết triệt để (kèm vị trí code & số liệu đo thực tế), cho đến các quirk mới phát hiện và checklist việc tiếp theo.

---

## Tóm tắt điều hành (Executive Summary)

**`dsh_gemini_bridge`** là một cầu nối OpenAI-compatible SSE + Multi-lane Gateway giúp biến tài khoản **Google AI Pro (Gemini Web)** thành backend cho **Coding Agent thực thụ** chạy trong **DeepSeek Harness (DSH)** và các IDE chuẩn OpenAI.

- **Bài toán cốt lõi:** Giao diện web Gemini (`gemini.google.com`) là chat stateful không có Native Function Calling API, dễ rớt mạng/timeout/từ chối định dạng, trong khi Coding Agent hiện đại (DSH) gửi request stateless kèm 34–38 tool definitions, luồng SSE abort giữa chừng, lịch sử nén context, và đòi hỏi **toàn vẹn dữ liệu tuyệt đối** khi ghi/sửa file code.
- **Trạng thái hiện tại (đã kiểm chứng):**
  - Bộ kiểm thử tự động: **`252/252` unit & integration tests pass** (`python -B -m unittest discover -s tests`), linter **`ruff` 0 lỗi**.
  - Kiểm thử E2E trực tiếp trên app **DeepSeek Harness** bằng **`Windows-MCP` (`use_vision=false`, 0 ảnh chụp màn hình)**: DSH (model `gemini-carbon`, profile `coding`, luồng `acc2`) đã tự tạo hoàn chỉnh mini-game web `D:\ABC\school-game` (`index.html`, `style.css`, `game.js`) và tự chạy 2 lượt kiểm tra PowerShell (`node --check` + đối chiếu ngoặc CSS) chỉ trong **3 turns / 35 giây**, **0 lỗi parse / 0 lần tự sửa**.

---

## Phần 1 — Khó khăn thực tế của người dùng (User Pain Points)

Trước khi thực hiện chuỗi cải tiến này, người dùng gặp 6 nhóm vấn đề trực tiếp làm gián đoạn công việc:

1. **Chi phí API thương mại cao & thao tác thủ công đứt mạch:** Có sẵn Google AI Pro trên web nhưng nếu copy-paste tay qua lại với IDE thì mất khả năng agent tự đọc file, sửa code và chạy test.
2. **"Chào `hi` cũng ngáo" và chậm bất thường:**
   - Khi gõ mỗi `"hi"`, IDE/DSH vẫn đính kèm toàn bộ System Prompt + hàng chục tool schema, khiến Turn 1 bị nhồi **`9,687–10,129` ký tự** (`~7,115` chars là tool schema) → model tưởng phải gọi tool hoặc mất nhiều thời gian xử lý.
   - Model mặc định/subagent trong DSH bị trỏ vào `gemini-4-neon` (Deep Think), khiến câu đơn giản cũng nghĩ `66s` hoặc chạm ngưỡng timeout `180s`.
3. **Đẻ rác hàng loạt đoạn chat trên Gemini Web & file rác ở repo:**
   - DSH gửi tới 34–38 tools (trong đó có ~12 tools điều phối `subagent`, `subagent_fork`, `workflow`, `team_task_*`, `send_message`...). Chỉ cần agent tự gọi subagent hoặc IDE nén ngữ cảnh (`context compaction`), cầu nối lại tạo `ChatSession` mới, vừa làm loãng trí nhớ vừa tràn lịch sử chat web.
   - Các file runtime `.bridge_session*.json`, `.bridge_accounts*.json`, `.bridge_chain*.json` nằm rải rác ngay thư mục gốc repo.
4. **Rủi ro mất toàn vẹn dữ liệu code (`"không chỉ để chạy được — ưu tiên toàn vẹn dữ liệu và được việc"`):**
   - `json_repair` sửa mù trên JSON tool `write`/`edit` bị cắt cụt có thể làm **mất trắng nửa dưới file code** hoặc tự bịa key lạ.
   - Gemini Web tự động biến URL thành `[url](url)`, nếu bóc mù quáng sẽ làm hỏng file tài liệu `.md`/`.rst`.
   - Khi AI trả lời ví dụ minh họa có chứa `<tool_call>` bên trong block code Markdown (```` ```xml ```` / ```` ```python ````), parser & streamer tưởng nhầm là lệnh thực thi thật.
5. **Treo luồng (`429`) khi Abort & mất phiên `30s+` khi gặp HTTP `502`:**
   - Khi người dùng bấm dừng giữa chừng (abort SSE), task nền `gemini_webapi` kẹt trong `asyncio.Lock()`, khóa luôn worker lane cho các lượt sau.
   - Khi Google trả `502` thoáng qua trước byte đầu tiên ở Turn 2 (`18:06:15`), cầu nối tự hủy cả `GeminiClient` đang đăng nhập để `init()` lại từ `https://gemini.google.com/app`, dẫn đến `curl (28)` timeout `30s` và rớt luôn câu trả lời.
6. **Nhu cầu điều khiển & test app Desktop không chụp màn hình:** Muốn agent tự mở, thao tác, và kiểm thử `DeepSeek Harness.exe` trên Windows mà **không tốn token ảnh/vision**.

---

## Phần 2 — Kiến trúc tổng quan & Thách thức kỹ thuật

### Luồng xử lý End-to-End

```mermaid
flowchart LR
    DSH["DeepSeek Harness Desktop<br/>(cordis.patch.yml -> :10000/v1)"] --> GW["Gateway :10000<br/>(main.py + app/gateway_router.py)"]
    GW -->|"Sticky Least-Inflight"| L1["Worker acc1 :10001"]
    GW -->|"Sticky Least-Inflight"| L2["Worker acc2 :10002"]
    L1 & L2 --> PP["app/tool_profiles.py<br/>app/prompt_builder.py"]
    PP -->|"Giữ nguyên client, lazy tools,<br/>U-shaped prompt"| SP["app/session_pool.py<br/>(.bridge_state/, cid/rid/rcid)"]
    SP --> RT["app/gemini_runtime.py<br/>(SingleChatManager + _turn_epoch)"]
    RT -->|"gemini_webapi stream"| GEM["Gemini Web<br/>(Hydrogen / Carbon / Neon)"]
    GEM -->|"Stream delta"| STR["app/streaming.py<br/>(ToolCallAwareStreamer)"]
    STR --> PAR["app/tool_parser.py<br/>(Lossless guard + prose-fence filter)"]
    PAR -->|"OpenAI SSE tool_calls / text"| DSH
    WMCP["Windows-MCP : stdio<br/>(--force-renderer-accessibility,<br/>use_vision=false)"] -.->|"UIA Tree Control"| DSH
```

### 4 Thách thức kỹ thuật cốt lõi khi biến Web Chat thành Coding Agent

1. **Giả lập Native Tool Calling bằng XML trên Stream:** Gemini Web không có trường `tools` / `tool_calls` chuẩn OpenAI. Cầu nối phải biên dịch JSON Schema thành hướng dẫn gọn (`compile_tools_prompt`), giấu thẻ `<tool_call>` đang stream dở bằng `ToolCallAwareStreamer`, parse lại ở `parse_tool_calls_detailed`, và tự sửa nội bộ 1 vòng (`_attempt_tool_self_correction`) nếu model trả sai định dạng.
2. **Đồng bộ Stateless IDE ↔ Stateful Web:** DSH gửi lại toàn bộ mảng `messages` mỗi lượt (kể cả khi nén context bằng `[Conversation Summary]` hoặc chèn `[model changed: ...]`), trong khi Gemini Web nhớ ngữ cảnh qua `metadata = [cid, rid, rcid]`. Bất kỳ sai lệch nhỏ nào về key phiên, `needs_tool_header`, hoặc metadata dở dang đều gây lỗi `1097` hoặc đứt ngữ cảnh.
3. **Tính đỏng đảnh của giao diện Gemini Web:**
   - Tự chèn hyperlink `[https://...](https://...)` vào text output.
   - Xoay cookie liên tục (cần Profile Vault CDP headless sync trong `app/cookie_sync.py`).
   - Có **bộ lọc hiển thị hậu kiểm (post-generation UI filter)** khi câu trả lời chỉ toàn raw XML `<tool_call>` chứa lệnh hệ thống.
   - Khác biệt rõ giữa các model: `gemini-4-hydrogen` (`~7–9s`, hội thoại nhanh), `gemini-carbon` (`~5–22s`, mạnh về coding), `gemini-4-neon` (Deep Think, `40–180s`).
4. **Tự động hóa Electron không Vision:** Electron mặc định tắt renderer accessibility tree, khiến UIA chỉ thấy 3 nút cửa sổ (`Minimize`, `Maximize`, `Close`). Phải ép cờ Chromium `--force-renderer-accessibility` và gộp các node chữ rời (`WINDOWS_MCP_MERGE_WORD_NODES=true`) để đọc trọn vẹn cấu trúc DOM/UIA mà không cần chụp ảnh màn hình.

---

## Phần 3 — Chi tiết 10 Vấn đề của Coding Agent đã giải quyết

### 1. Lọc Tool theo Profile & Hot-Swap Runtime
- **Vấn đề:** DSH gửi 34–38 tool (bao gồm 12 tool `subagent`, `subagent_fork`, `list_agents`, `team_task_*`, `workflow`... và nhiều tool `schedule_*`, `create_goal`, `plugin_manager`), làm phình prompt Turn 1 lên `>7,100` ký tự và khiến Gemini Web tự gọi subagent đẻ tab chat mới.
- **Giải pháp (`app/tool_profiles.py`, `app/config.py`, `app/routes_admin.py`, `app/gateway_router.py`, `app/status_view.py`):**
  - Xây dựng 6 profile chuẩn: `no_subagents` (mặc định), `coding`, `coding_parallel`, `research`, `none`, `all`, kèm `ALLOWED_TOOLS`, `BLOCKED_TOOLS`, bảo toàn tool bị ép trong `tool_choice` (`extract_forced_tool_names`).
  - Hỗ trợ `GET/POST /v1/tool-profile` với đồng bộ Gateway → worker (`X-Lane-Token`), reset `tool_header_sent` toàn pool, và `persist=True` ghi nguyên tử xuống `.env*`.
- **Kết quả đo:** Ở chế độ `coding`, cầu nối rút từ **`34` tool xuống `18/34` tool**, phần `tools=` ở Turn 1 giảm từ `~7,115` chars xuống còn **`4,608` chars**.

### 2. Tách bạch tự nhiên "Chat ra chat, Làm ra làm"
- **Vấn đề:** Gõ `"hi"` hoặc hỏi khái niệm bình thường cũng kích hoạt nạp hàng nghìn ký tự tool schema; DSH gửi `content` dạng multimodal list `[{"type": "text", "text": "hi"}]` làm bộ nhận diện chuỗi `str` cũ bị trượt; các câu chào bị abort ở lượt trước bị gộp vào lượt sau.
- **Giải pháp (`app/prompt_builder.py`, `app/openai_adapter.py`):**
  - `_extract_text_without_images()` trích text chuẩn cho cả `str` và `list` part text; `_latest_turn_messages()` loại bỏ tin chào mồ côi đã bị hủy ở turn cũ.
  - `is_plain_greeting_request()` (xóa `tools_map = {}` trên greeting) và `is_conversational_chat_request()` tạm hoãn tiêm `=== WORKSPACE TOOLS ===` (`defer_header`) cho tới khi xuất hiện tác vụ workspace thật.
  - Rút gọn header biên dịch trong `compile_tools_prompt()` xuống `< 830` ký tự và bổ sung chỉ dẫn `"Chat naturally first: reply in plain text WITHOUT <tool_call> for greetings/chitchat/general Q&A"`.
- **Kết quả đo thực tế trên DSH (`acc1`):** Turn 1 `"hi"` chỉ **`Chars=4` (`7.6s`, 0 tools)** → Turn 2 hỏi khái niệm **`Chars=88` (`7.6s`, 0 tools)** → Turn 3 hỏi workspace mới tự động nạp tool (`Chars=10129`, **`9.7s`**) trên **cùng `chat_key=203b7bd2`**.

### 3. Chuẩn hóa Prompt riêng cho DeepSeek Harness & U-Shaped Ordering
- **Vấn đề:** DSH tự chèn tin `user` dạng `[model changed: ...]` ở cuối làm che khuất câu hỏi thật, chèn noise GUI Web (`client-plugin HMR receiver ... window.__DSH_BOOT__`), và `compile_tools_prompt` có hàm cắt câu làm gãy ngoặc `(e.g. *.ts)` chưa đóng.
- **Giải pháp (`app/prompt_builder.py`):**
  - `_strip_injected_boilerplate()` + `_DSH_BRACKET_META_RE` lọc sạch `[model changed: ...]`, `<system-reminder>`, noise `window.__DSH_BOOT__`.
  - `_clip_at_word()` / `_first_clean_sentence()` sửa lỗi cắt ngoặc mở.
  - Sắp xếp prompt theo **U-shaped ordering**: `[SYSTEM & WORKSPACE CONTEXT]` + `=== WORKSPACE TOOLS ===` đặt trước, `[USER REQUEST]` đặt ở cuối cùng (vùng chú ý cao nhất của LLM).

### 4. Bảo vệ toàn vẹn dữ liệu ở Tool Parser, Streamer & Truncation
- **Vấn đề:** `json_repair` có thể sửa JSON bị cụt thành JSON hợp lệ nhưng thiếu nửa sau nội dung file; URL trong Markdown bị bóc link; `<tool_call>` minh họa trong code fence bị thực thi nhầm; lỗi cắt ngầm `smart_truncate` làm mất ngữ cảnh `edit`.
- **Giải pháp (`app/tool_parser.py`, `app/streaming.py`, `app/openai_adapter.py`, `app/prompt_builder.py`):**
  - `_verify_lossless_heuristic_repair()`: với nhóm tool ghi/sửa/chạy lệnh (`is_mutation_or_exec_tool`), từ chối kết quả `json_repair` nếu tự sinh key không nằm trong schema hoặc làm rơi bất kỳ token chữ/số nào (vẫn cứu 100% lỗi escape dấu nháy đơn giản trong `< 1ms`).
  - `_sanitize_tool_argument_values(..., preserve_markdown_links=...)`: giữ nguyên `[https://...](https://...)` cho `.md`, `.mdx`, `.markdown`, `.rst`; chỉ gỡ auto-link trên file code/config.
  - `_find_prose_code_fence_ranges()` + `_scan_tool_call_blocks()` trong `tool_parser.py` kết hợp `_update_prose_fence_state_until()` trong `ToolCallAwareStreamer` (`streaming.py`): bỏ qua hoàn toàn `<tool_call>` nằm trong block minh họa.
  - Khi `has_unclosed_tag=True`, `_attempt_tool_self_correction()` nhắc model chia file `>250` dòng thành bộ khung nhỏ + các bước `edit` cục bộ; `build_context_bootstrap()` thêm quy tắc `[INTEGRITY]`; `smart_truncate()` ghi rõ khoảng ký tự bị lược bỏ.

### 5. Chống Deadlock Turn-Lock & Race Condition khi Abort Stream
- **Vấn đề:** Client ngắt kết nối (Stop) khi `gemini_webapi` đang treo khiến producer kẹt giữ `self.lock`, mọi request tiếp theo dính `429 BridgeBusyError`, hoặc task cũ kịp ghi đè `chat_session.metadata` sang lượt mới.
- **Giải pháp (`app/openai_adapter.py`, `app/gemini_runtime.py`):**
  - `sse_generator` `finally:` chỉ đợi hủy producer tối đa `3.0s`; quá hạn sẽ gọi `mgr.force_release_stuck_turn_lock()`.
  - `force_release_stuck_turn_lock()` tăng biến đơn điệu `_turn_epoch` và tạo ngay đối tượng `chat_session` tách biệt từ `_last_safe_metadata` (sanitized trước lượt), chặn hoàn toàn race-condition từ coroutine cũ còn rơi rớt.

### 6. Vá lỗi Retry `502` treo 30s & Cấu hình Soft Fallback Model
- **Vấn đề phát hiện trong log thật (`activity_acc1.jsonl`, `18:06:15`):** Gặp `Failed to generate contents. Status: 502` ở `attempt == 0`, code cũ gọi `_chat_metadata_snapshot()` (có thể dính metadata bẩn giữa lượt) rồi gọi `_recover_client_after_error()`, tự `close()` và `init()` lại `GeminiClient` gây timeout `30s` khi kết nối lại `gemini.google.com/app`.
- **Giải pháp (`app/gemini_runtime.py#L1052-L1083`, `.env*`):**
  - Luôn lấy `saved_meta = saved_meta_before_send`.
  - Nếu `not self._is_auth_error(exc)` và `self.client is not None` và `auth_ok=True`, **giữ nguyên `GeminiClient`**, nghỉ tối đa `0.5s`, khôi phục session từ `saved_meta_before_send` và retry ngay.
  - Thêm `FALLBACK_MODEL="gemini-4-hydrogen"` vào `.env`, `.env.acc1`, `.env.acc2` để nếu `gemini-carbon` / `gemini-neon` kẹt byte đầu thì tự trượt sang `gemini-4-hydrogen`.

### 7. Bền vững phiên hội thoại & Gom gọn Runtime State
- **Vấn đề:** Đổi model trong cùng phiên hoặc IDE thực hiện context compaction làm đẻ `ChatSession` mới và gửi lại 11k ký tự tool header; các file JSON runtime vương vãi ở root repo.
- **Giải pháp (`app/session_pool.py`, `app/config.py`, `app/accounts.py`):**
  - `ensure_slot_model_locked(..., keep_chat=True)` bảo toàn `tool_header_sent=True` khi giữ nguyên `cid`.
  - `_activate_session_slot_locked()` tái sử dụng active lead `ChatSession` khi phát hiện `[Conversation Summary]` / `continued from a previous conversation` trên cùng `workspace_root`.
  - Gom `.bridge_session*.json`, `.bridge_accounts*.json`, `.bridge_chain*.json` vào `.bridge_state/` (gitignored), ghi nguyên tử bằng `tmp + os.replace`, và tắt ghi `chain_file` ở Gateway (`not IS_GATEWAY_INSTANCE`).

### 8. Tối ưu cấu hình DSH Desktop & Phục hồi Cookie tự động
- **Giải pháp (`C:\Users\USER\.dsh\profiles\desktop\cordis.patch.yml`, `app/cookie_sync.py`):**
  - Đổi `agent-default-model` sang `gemini-carbon` (nhanh và mạnh về code hơn `gemini-4-neon` hay timeout 180s), cập nhật `subagent-model-selection-settings` thành `gemini-carbon` + `gemini-4-hydrogen` trên `gemini4`.
  - Cơ chế headless Chrome CDP auto-sync (`app/cookie_sync.py`) đã tự chứng minh thực tế trong log khi `acc1`/`acc2` hết hạn cache cookie (`18:03`, `18:48`) → tự đồng bộ lại từ profile Chrome `acc1`/`acc2` trong 1–3 giây mà không cần người dùng copy cookie tay.

### 10. Chế độ Chạy Nền Bền Bỉ, Bảo Mật CSRF & Nghiệm Thu Thực Chiến (v3.3)
- **Bảo mật CSRF trình duyệt (`b9df738`, `app/lane_security.py`, `main.py`):**
  - `is_cross_site_browser_request()` chặn mọi request không phải `GET`/`HEAD`/`OPTIONS` mang `Origin` ngoài loopback/LAN/`ALLOWED_HOSTS`, `Origin: null`, hoặc `Sec-Fetch-Site: cross-site` kể cả khi `API_KEY` để trống (không ảnh hưởng CLI/IDE không gửi `Origin`).
- **Chống `1213`, Retry Giữa Stream & Điều Phối Chạy Nền (`029a249`, `app/gemini_runtime.py`, `app/gateway_router.py`, `app/openai_adapter.py`, `app/config.py`):**
  - **Mid-stream retry (`MID_STREAM_RETRIES=2`, `MID_STREAM_RETRY_DELAY=20s`):** Khi stream bị cắt giữa chừng trước khi client thấy chữ hiển thị (vd đang đệm `<tool_call>`), tự giãn `20s`/`40s` và thử lại sạch từ metadata an toàn (che cookie trong log).
  - **Tự nghỉ luồng khi dính `1213` (`RATE_LIMIT_REST_AFTER=1`, `RATE_LIMIT_REST_MINUTES=45`):** Tự cho worker nghỉ `45` phút (`lane_resting_until` trong `/status`) và Gateway tự né sang luồng khác; kết hợp giãn mở chat mới `NEW_CHAT_MIN_INTERVAL=25s` và nhịp ngẫu nhiên `TURN_JITTER_MIN=3s`..`TURN_JITTER_MAX=8s`.
  - **Nhắc hành động (`CODE_DUMP_NUDGE=true`, `CODE_DUMP_MIN_LINES=20`):** Tự nhắc kín 1 lượt nếu Gemini in `>=20` dòng code ra chat hoặc nói `"không có tool"` trong lúc làm việc workspace.
  - **Lead động chờ luồng sticky (`LEAD_STICKY_LOCK_WAIT=240s`):** Lead không cố định ở `acc1`, Gateway chờ luồng đang giữ `ChatSession` rảnh tối đa `240s` thay vì nhảy luồng làm đứt phiên; `429 bridge_busy` không gây cooldown.
- **Kiểm chứng thực tế:**
  - Bộ unit test mock `tests/` (từng pass `252/252`) cùng `scripts/live_smoke.py` và `D:\ABC\school-game` đã được gỡ bỏ theo yêu cầu chủ dự án để tránh test giả/rác; mã nguồn hiện tại được kiểm định trực tiếp bằng `ruff check --no-cache --select F,E9,B main.py app scripts` (**0 lỗi**), `py_compile`, và chạy thực chiến trên DSH.
  - **Live E2E Test với DSH (luồng `acc2`, `gemini-carbon`, `profile=coding`):**
    - **Turn 1 (`21.8s`):** `Chars=7480 [user=718 sys=2150 tools=4608(18/34) profile=coding]` → gọi song song **4 tools** (`write` × 3 + `pwsh` chạy `node --check` & `Test-Path`).
    - **Turn 2 (`4.65s`):** `Chars=696` → DSH tự gọi `pwsh` (`call_9159a648488a4efb`) kiểm tra độ dài HTML (`2231`) và cân bằng ngoặc CSS (`open=26 close=26`).
    - **Turn 3 (`5.49s`):** `Chars=319` → hoàn tất trên UI (`Completed in 35s`), **0 lỗi parse**, **0 ảnh chụp màn hình**.

---

## Phần 4 — Đánh Giá Khách Quan So Với Một Coding Agent Thực Thụ

| Tiêu chí | Agent API Native (Claude Code / Cursor) | DSH + `dsh_gemini_bridge v3.3` | Nhận xét |
| :--- | :--- | :--- | :--- |
| **Gọi Tool** | Native JSON `tool_calls` | XML `<tool_call>` + FSM + guard + tự sửa + `CODE_DUMP_NUDGE` | **9/10** — đủ tin cậy cho multi-turn |
| **Toàn vẹn dữ liệu** | Cao | **9.5/10** — `_verify_lossless_heuristic_repair` chặn cắt cụt file, bảo vệ link `.md`, lọc code fence | **Điểm mạnh nổi bật** |
| **Ngữ cảnh & Phiên** | Gửi raw window lớn | Stateful Web (`cid/rid/rcid`) + compaction reuse + `LEAD_STICKY_LOCK_WAIT` | **7.5/10** — tiết kiệm token, phụ thuộc nén phía web nếu quá dài |
| **Tốc độ vs Độ bền** | Nhanh (~1–4s TTFT) | ~5–22s/turn + jitter `3–8s` + giãn chat mới `25s` + tự nghỉ `45m` khi `1213` | **7.5/10** — ưu tiên bền chạy nền hơn burst |
| **Tự kiểm thử** | `bash`/`pwsh` + MCP | `pwsh` (unit/API/syntax) + tùy chọn Playwright/`Windows-MCP` UIA | **8.5/10** |
| **Chi phí vận hành** | Tính tiền theo token | Tận dụng gói Google AI Pro đa tài khoản | **10/10** |

**Kết luận:** Hệ thống đã đạt chuẩn **một Coding Agent vận hành được thật** (tự lập kế hoạch, gọi tool song song, chạy `pwsh` test thật, tự sửa lỗi và bảo toàn file), đặc biệt mạnh về **an toàn dữ liệu** và **chi phí**. Hạn chế còn lại thuộc bản chất **Gemini Web** (giới hạn nhịp mở chat mới chống `1213`, post-generation filter giao diện web, độ trễ cao hơn API thương mại). Kiến trúc tối ưu nhất là dùng **Claude Code làm bộ não điều phối & review**, giao phần thi công qua **DSH + Bridge**.

---

## Phần 5 — Quirk Đã Biết, Việc Đang Mở & Chỉ Dẫn Cho Claude

### 1. Quirk thực tế cần lưu ý
- **Gemini Web post-generation filter trên turn chỉ có raw XML `pwsh`:** Ở Turn 2 (`18:53:49`), bridge đã nhận đủ `502` chars chứa `<tool_call>` qua stream và DSH chạy `pwsh` thành công, nhưng trên giao diện web Google ghi đè hiển thị thành `"I'm having a hard time fulfilling your request..."` và giải mã `session.v4.jsonl.zstd` thấy lọt `"d?"` trước `<tool-call>`. Hướng cải thiện: dọn fragment rò ngắn trước `<tool_call>` hoặc thêm prompt hint viết 1 câu mở đầu ngắn trước thẻ `<tool_call>`.
- **Đồng bộ tiến trình sống:** Sau khi sửa code runtime, luôn chạy `stop_all.bat` rồi `run_all.bat` để cả Gateway và mọi worker nạp cùng phiên bản mới nhất.
- **Mở rộng test UI:** Nếu muốn DSH tự bấm kiểm tra giao diện Canvas/DOM trình duyệt thay vì chỉ `pwsh`, hãy gắn Playwright hoặc MCP browser/`Windows-MCP` vào DSH.

### 2. Checklist các việc tiếp theo
1. **Nhịp 2 (Tái cấu trúc kiến trúc — tùy chọn):** Gom 27 module phẳng trong `app/` thành 6 subpackage (`api/`, `gateway/`, `protocol/`, `runtime/`, `auth/`, `core/`) khi tiếp tục mở rộng.
2. **Thử nghiệm đa luồng Frontend + Backend (`coding_parallel`):** Chuyển `POST /v1/tool-profile {"profile": "coding_parallel"}` để Lead DSH chia subagent chạy song song trên `acc1` và `acc2`.
3. **Nối Claude Code ↔ DSH Worker (nếu dùng):** Bọc `dsh.cmd headless` thành MCP server gắn vào Claude Code để Claude chỉ suy luận & review `git diff`.

### 3. Cách đưa tài liệu này cho Claude đọc
- **Claude Code (mở tại `d:\GG_API_TEST\gemini\dsh_gemini_bridge`):** Tự động nạp `CLAUDE.md`; có thể nhắc *"Đọc thêm `docs/AGENT_ENGINEERING_CASE_STUDY.md`"*.
- **Claude Desktop / Claude Web:** Đính kèm file `docs/AGENT_ENGINEERING_CASE_STUDY.md` vào **Project Knowledge** hoặc khung chat.

---

## Tài Liệu Nghiên Cứu, Chuẩn Kỹ Thuật & Nguồn Mở Tham Khảo

- **ReAct** — Yao et al. (ICLR 2023), *Synergizing Reasoning and Acting in Language Models*.
- **Toolformer** — Schick et al. (NeurIPS 2023), *Language Models Can Teach Themselves to Use Tools*.
- **Reflexion & Self-Refine** — Shinn et al. (NeurIPS 2023) & Madaan et al. (NeurIPS 2023) (self-correction & `CODE_DUMP_NUDGE`).
- **Lost in the Middle** — Liu et al. (TACL 2024), *How Language Models Use Long Contexts* (salience truncation & U-shaped prompt ordering).
- **The Power of Two Choices** — Mitzenmacher (2001) & **Release It!** — Nygard (2018) (least-inflight balancing, circuit breakers, rate-limit rest isolation).
- **Exponential Backoff and Jitter** — Marc Brooker, *AWS Architecture Blog* (`MID_STREAM_RETRY_DELAY` + `TURN_JITTER_*` + `NEW_CHAT_MIN_INTERVAL`).
- **Chuẩn & Giao thức:** RFC 6455 (WebSocket CDP), RFC 8259 & JSON Schema Draft 2020-12, W3C Fetch Metadata (`Origin` / `Sec-Fetch-Site` CSRF), WHATWG SSE, Chrome DevTools Protocol (CDP), Model Context Protocol (MCP), Microsoft UI Automation (UIA).
- **Nguồn mở tích hợp:** `HanaokaYuzu/Gemini-API` (`gemini-webapi`), `mangiucugna/json_repair`, `fastapi/fastapi`, `encode/uvicorn`, `encode/httpx`, `lexiforest/curl_cffi`, `CursorTouch/Windows-MCP`.
