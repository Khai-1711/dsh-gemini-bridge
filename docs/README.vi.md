# DSH Gemini Agent Bridge (v3.3)

> 🌐 **Ngôn ngữ:** [English](../README.md) | **Tiếng Việt** | [한국어](README.ko.md)  
> 📐 **Sơ đồ Kiến trúc & Thuật toán:** [English](ARCHITECTURE.en.md) | [Tiếng Việt](ARCHITECTURE.vi.md) | [한국어](ARCHITECTURE.ko.md)  
> 📋 **Tài liệu Nghiên cứu Thực chiến & Bàn giao cho Claude:** [AGENT_ENGINEERING_CASE_STUDY.md](AGENT_ENGINEERING_CASE_STUDY.md) · [CLAUDE.md](../CLAUDE.md)

**DSH Gemini Agent Bridge** là cổng proxy chuẩn OpenAI (`/v1/models`, `/v1/chat/completions`, `/v1/responses`) và bộ điều phối đa luồng biến tài khoản **Google AI Pro (Gemini Web)** thành backend cho **Coding Agent thực thụ** chạy trên **DeepSeek Harness (DSH)**, IDE Agent và các client OpenAI SDK — tối ưu đồng thời **toàn vẹn dữ liệu code**, **độ bền chạy nền chống bóp `1213`**, và **chi phí bằng 0 đồng API phát sinh**.

---

## Tổng hợp Toàn bộ Những Gì Đã Làm (v1 → v3.3)

### 1. Cổng Gateway Đa Luồng, Ghim Phiên Lead Động & Cô Lập Session
- **Frontend Gateway (`:10000` → `:10001` `acc1`, `:10002` `acc2`, ...):** Định tuyến giữ phiên (Sticky TTL `3600s`), lọc theo model hỗ trợ, cân bằng tải ít request đang chạy nhất (`app/gateway_router.py`).
- **Lead Sticky Lock Wait (`LEAD_STICKY_LOCK_WAIT=240s`):** Hội thoại Lead nằm trên luồng nào là động (không khóa cứng `acc1`), và khi luồng đang bận sẽ chờ tối đa `240s` thay vì nhảy luồng làm mất `ChatSession` giữa chừng; mã `429 bridge_busy` không làm luồng bị đánh dấu cooldown.
- **Session Pool bền vững khi nén ngữ cảnh (`app/session_pool.py`):** Cô lập `(cid/rid/rcid, model, workspace, FactLedger)`, giữ `tool_header_sent=True` khi đổi model trên cùng `cid`, tự giữ nguyên `ChatSession` của Lead khi IDE chạy context compaction (`[Conversation Summary]`), và gom file state vào `.bridge_state/` ghi nguyên tử (`tmp + os.replace`).

### 2. Xác Thực Profile Vault (0 Tab Mở Nền) & Chống CSRF Trình Duyệt
- **Profile Vault + Headless CDP Sync (`app/cookie_sync.py`):** Đăng nhập tay + 2FA đúng 1 lần trên Chrome/Edge profile tách biệt; mọi lần làm mới sau chạy ẩn `--headless=new` qua client WebSocket RFC 6455 thuần Python (~2–4 giây), lấy trọn cookie jar (`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID`) và nạp nóng vào worker.
- **Chốt phá Deadlock khởi tạo (`_safe_init_client`):** Chặn treo đệ quy `_lock` của `gemini-webapi` ở `25s`.
- **Chặn CSRF từ trình duyệt (`app/lane_security.py`, `main.py`):** Chặn mọi request không phải `GET`/`HEAD`/`OPTIONS` có `Origin` lạ / `"null"` hoặc `Sec-Fetch-Site: cross-site`, kể cả khi chưa đặt `API_KEY`, trong khi các CLI/IDE không gửi `Origin` vẫn chạy bình thường.

### 3. Lọc Tool Profile, Tách Bạch "Chat ra Chat — Làm ra Làm" & Chuẩn Hóa DSH
- **6 Tool Profiles + Đổi nóng Runtime (`app/tool_profiles.py`, `app/routes_admin.py`):** `no_subagents` (mặc định), `coding`, `coding_parallel`, `research`, `none`, `all`, hỗ trợ `ALLOWED_TOOLS`/`BLOCKED_TOOLS` và bảo toàn tool bị ép qua `tool_choice`. Đổi nóng tức thì qua `GET/POST /v1/tool-profile` đồng bộ Gateway → Worker qua `X-Lane-Token`. Ở chế độ `coding`, bộ 34 tool của DSH rút gọn còn **18 tool cốt lõi** (Turn 1 giảm từ `~7,115` xuống **`4,608` ký tự** phần tool).
- **Chat tự nhiên vs Làm việc workspace (`app/prompt_builder.py`, `app/openai_adapter.py`):** Nhận diện payload multimodal list của DSH (`[{"type": "text", "text": "hi"}]`), bỏ qua tin chào bị abort ở turn trước, tạm hoãn `WORKSPACE TOOLS` khi chỉ chào hỏi (`Chars=4`, `7.6s`, 0 tool) hoặc hỏi đáp kiến thức (`Chars=88`, `7.6s`), chỉ bơm tool khi có việc workspace thực sự trên cùng `ChatSession`.
- **Dọn nhiễu DSH & Bố cục U-shaped:** Gỡ metadata `[model changed: ...]`, `<system-reminder>`, boilerplate GUI `window.__DSH_BOOT__`; sửa lỗi cắt câu gây gãy ngoặc `(e.g. *.ts)`; đặt `[SYSTEM] + WORKSPACE TOOLS` lên đầu và `[USER REQUEST]` ở cuối.

### 4. Bảo Toàn Dữ Liệu Gọi Tool, Streaming FSM & Chống Deadlock Abort
- **Streaming FSM chống lộ/nhầm thẻ (`app/streaming.py`):** `ToolCallAwareStreamer` $O(N)$ 1 lượt duyệt ẩn `<tool_call>` thực thi nhưng vẫn cho các ví dụ `<tool_call>` nằm trong block code Markdown stream ra bình thường.
- **Lớp bảo vệ toàn vẹn ở Parser (`app/tool_parser.py`):**
  - `_find_prose_code_fence_ranges` & `_scan_tool_call_blocks`: loại trừ `<tool_call>` minh họa trong prose fence.
  - `_verify_lossless_heuristic_repair`: chặn `json_repair` trên tool ghi/sửa/chạy lệnh (`write`, `edit`, `pwsh`...) nếu bịa key mới hoặc làm rơi bất kỳ token chữ/số nào (chặn cắt cụt file âm thầm, vẫn cứu lỗi escape ngoặc sạch trong `<1ms`).
  - `_sanitize_tool_argument_values`: giữ nguyên self-link Markdown `[https://...](https://...)` trong file `.md`/`.mdx`/`.markdown`/`.rst` và chỉ bóc auto-link trên file code.
- **Chống treo Turn-Lock & Self-Correction (`app/openai_adapter.py`, `app/gemini_runtime.py`):** Giới hạn chờ hủy producer `3.0s` trong `sse_generator` + `force_release_stuck_turn_lock()` tăng `_turn_epoch` và tách object `chat_session` sạch từ `_last_safe_metadata`; khi `has_unclosed_tag=True` nhắc model tách file `>250` dòng thành bộ khung + `edit`; bổ sung nhắc `[INTEGRITY]` và báo rõ khoảng ký tự bị lược bỏ trong `smart_truncate()`.

### 5. Chế Độ Chạy Nền Bền Bỉ (Chống `1213`, Retry Giữa Stream, Nhắc Gọi Tool)
- **Vá lỗi Retry `502` treo 30s (`app/gemini_runtime.py`):** Khi gặp lỗi tạm thời không phải auth ở `attempt == 0` mà `auth_ok=True`, giữ nguyên `GeminiClient` đang đăng nhập và retry ngay trên `saved_meta_before_send` thay vì `close()/init()` mất `30s` timeout.
- **Thử lại giữa Stream (`MID_STREAM_RETRIES=2`, `MID_STREAM_RETRY_DELAY=20s`):** Khi Google cắt stream giữa chừng mà client **chưa thấy chữ nào hiển thị** (ví dụ đang giữ buffer `<tool_call>`), tự chờ giãn dần `20s` → `40s` rồi thử lại sạch (log lỗi đã che cookie).
- **Chống `1213` & Tự Nghỉ Luồng (`RATE_LIMIT_REST_AFTER=1`, `RATE_LIMIT_REST_MINUTES=45`):** Giãn cách mở chat mới `NEW_CHAT_MIN_INTERVAL=25s` + nhịp ngẫu nhiên `TURN_JITTER_MIN=3s`..`TURN_JITTER_MAX=8s`. Khi dính lỗi `1213`, worker tự nghỉ **45 phút** (`lane_resting_until` hiện trên `/status`) và Gateway tự né sang luồng khỏe.
- **Nhắc hành động (`CODE_DUMP_NUDGE=true`, `CODE_DUMP_MIN_LINES=20`):** Khi đang xử lý việc workspace mà Gemini lại dán `>=20` dòng code ra chat hoặc nói `"không có tool"`, bridge tự nhắc kín 1 lượt để model phát `<tool_call>` thực sự.
- **Soft Fallback (`FALLBACK_MODEL`):** Tùy chọn qua `.env*` (ví dụ `gemini-4-hydrogen`) khi model chính kẹt quá lâu không ra byte đầu.

### 6. Nghiệm Thu Thực Chiến & Tối Ưu DSH + `Windows-MCP`
- Cấu hình [cordis.patch.yml](file:///C:/Users/USER/.dsh/profiles/desktop/cordis.patch.yml) dùng `gemini-carbon` mặc định (tránh `gemini-4-neon` dễ timeout `180s`), và chạy test thực chiến DSH qua **`Windows-MCP` không chụp màn hình** (`use_vision=false`, `--force-renderer-accessibility`): DSH (`acc2`, `gemini-carbon`, `profile=coding`) tự tạo trọn bộ mini-game web và tự chạy **2 bước kiểm tra PowerShell (`node --check` + đếm ngoặc CSS)** trong **3 turns / 35 giây** (`21.8s` + `4.65s` + `5.49s`), 0 lỗi parse.
- Đã gỡ bỏ các file mock `tests/` và `scripts/live_smoke.py` theo yêu cầu chủ dự án để tránh test ảo; kiểm tra mã nguồn hiện tại bằng `ruff check --no-cache --select F,E9,B main.py app scripts` (**0 lỗi**) và `py_compile`.

---

## Đánh Giá Khách Quan So Với Một Coding Agent Thực Thụ

Nếu đặt **DSH + `dsh_gemini_bridge v3.3`** lên bàn cân với một Coding Agent dùng API native chính chủ (như **Claude Code**, **Cursor Agent**, **Codex CLI**, hoặc DSH chạy API thương mại):

| Tiêu chí | Agent API Native (Claude Code / Cursor) | DSH + `dsh_gemini_bridge v3.3` | Đánh giá thực tế |
| :--- | :--- | :--- | :--- |
| **Độ chính xác gọi Tool** | Native JSON `tool_calls` chuẩn API | XML `<tool_call>` giả lập + FSM + kiểm định schema + tự sửa 1 vòng + `CODE_DUMP_NUDGE` | **Gần tiệm cận (9/10)** — chạy trơn đa số turn, chậm hơn khi phải tự sửa 1 vòng |
| **Toàn vẹn dữ liệu Code** | Cao (tham số có cấu trúc) | **Rất cao (9.5/10)** — chặn cắt cụt `json_repair`, bảo vệ link `.md`, chống nhầm `<tool_call>` trong code block | **Đạt chuẩn production** |
| **Giữ ngữ cảnh dài** | Context window API lớn (128k–1M) gửi minh bạch | Gemini Web giữ state qua `cid/rid/rcid` + giữ lead khi IDE nén + ngân sách tool 36k | **Khá tốt (7.5/10)** — tiết kiệm token hơn gửi lại toàn bộ, nhưng bị phụ thuộc nén ngầm phía server Google nếu phiên quá dài |
| **Tốc độ & Nhịp chạy** | ~1–4s TTFT; bắn burst cao | ~5–22s/turn (`gemini-carbon`) + chủ động đệm jitter `3–8s` & giãn mở chat mới `25s` | **Đánh đổi (7/10)** — ưu tiên bền tài khoản sống lâu hơn tốc độ tối đa |
| **Độ bền & Chạy nền** | Hiếm rớt auth, chỉ vướng quota TPM/RPM | Tự nghỉ `45 phút` khi gặp `1213`, retry giữa stream `20s/40s`, giữ client khi `502`, CDP auto cookie sync | **Rất bền ở quy mô 2–4 luồng (8/10)** |
| **Khả năng Tự Kiểm Thử** | Terminal (`bash`/`pwsh`) + MCP/Browser | Đủ `read`/`write`/`edit`/`pwsh` + chạy server nền kiểm tra API/cú pháp; có thể gắn thêm Playwright/`Windows-MCP` để test UI | **8.5/10** (code/API), **9.5/10** nếu gắn thêm MCP browser |
| **Bảo mật cục bộ** | HTTPS + sandbox IDE | Bind loopback, chặn CSRF `Origin`/`Sec-Fetch-Site`, lọc lệnh phá OS, che cookie trong log | **Chắc chắn (8.5/10)** |
| **Hiệu quả Chi phí** | Trả tiền theo từng triệu token | Tận dụng gói **Google AI Pro** sẵn có qua nhiều tài khoản xoay vòng | **Vượt trội (10/10)** |

### Kết luận đánh giá Agent
- **Điểm đạt chuẩn "1 Agent thật":** Đủ khả năng tự đọc codebase, lập kế hoạch, gọi nhiều tool ghi/sửa song song, chạy `pwsh`/test thật đọc lỗi traceback và tự sửa vòng lặp nhiều turn, an toàn dữ liệu và sống dai khi chạy nền.
- **Điểm giới hạn bản chất (không thể né hoàn toàn vì đi qua Gemini Web):**
  1. Không thể bắn hàng loạt request mở chat mới dồn dập trong 1 giây (sẽ dính `1213`).
  2. Giao diện web Google có bộ lọc hậu kiểm UI đôi khi che tin nhắn chỉ chứa XML thô hoặc lọt mảnh tiền tố hiếm gặp.
  3. Tốc độ phản hồi từng lượt chậm hơn API thương mại do phải đợi thinking + jitter bảo vệ tài khoản.
- **Mô hình tối ưu nhất:** Dùng **Claude Code làm Kiến trúc sư (suy luận, chia việc, review `git diff`)** qua [CLAUDE.md](../CLAUDE.md), và đẩy các tác vụ code/sửa lặp xuống **DSH + `dsh_gemini_bridge`**.

---

## Cấu trúc thư mục & Vận hành nhanh

```text
dsh-gemini-bridge/
├── app/                     # Runtime, Gateway router, Profile Vault CDP, Tool Parser/Streaming/Profiles, Security
├── docs/                    # AGENT_ENGINEERING_CASE_STUDY.md & README/ARCHITECTURE (EN/VI/KO)
├── frontend/                # Dashboard Web (/dashboard)
├── scripts/                 # cookie_sync.py CLI & launchers/
├── CLAUDE.md                # Tài liệu nạp tự động cho Claude Code
├── main.py                  # Điểm khởi chạy FastAPI (Gateway hoặc Worker theo BRIDGE_INSTANCE)
├── run_all.bat / stop_all.bat
└── .env.example             # Mẫu cấu hình đầy đủ (v3.3)
```

```powershell
# Chạy / dừng toàn cụm (Gateway :10000 + acc1 :10001 + acc2 :10002)
.\run_all.bat
.\stop_all.bat

# Kiểm tra mã nguồn sau khi chỉnh sửa
ruff check --no-cache --select F,E9,B main.py app scripts
python -B -m py_compile main.py
```

---

## Biến Môi Trường Quan Trọng (v3.3)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TOOL_PROFILE` | `no_subagents` | `no_subagents` \| `coding` \| `coding_parallel` \| `research` \| `none` \| `all` |
| `FALLBACK_MODEL` | `""` | Model dự phòng khi quá thời gian byte đầu (vd `gemini-4-hydrogen`) |
| `MID_STREAM_RETRIES` / `MID_STREAM_RETRY_DELAY` | `2` / `20` | Thử lại khi đứt giữa stream trước khi client thấy chữ (`20s`, `40s`) |
| `RATE_LIMIT_REST_AFTER` / `RATE_LIMIT_REST_MINUTES` | `1` / `45` | Dính `1213` sẽ cho luồng tự nghỉ `45` phút, Gateway tự né |
| `NEW_CHAT_MIN_INTERVAL` | `25` | Giãn cách tối thiểu (giây) giữa 2 lần mở chat mới trên 1 tài khoản |
| `TURN_JITTER_MIN` / `TURN_JITTER_MAX` | `3` / `8` | Nhịp trễ ngẫu nhiên giống người giữa các lượt liên tiếp |
| `LEAD_STICKY_LOCK_WAIT` | `240` | Thời gian Gateway chờ luồng Lead đang ghim rảnh trước khi failover |
| `CODE_DUMP_NUDGE` / `CODE_DUMP_MIN_LINES` | `true` / `20` | Tự nhắc kín bắt gọi tool nếu Gemini in `>=20` dòng code ra chat |

---

## Tài Liệu Nghiên Cứu, Tiêu Chuẩn Kỹ Thuật & Nguồn Mở Tham Khảo

### Bài báo Khoa học & Nguyên lý Kiến trúc Áp dụng
- **ReAct** — Yao et al. (ICLR 2023), *Synergizing Reasoning and Acting in Language Models* (xen kẽ suy luận `thoughts` và hành động có cấu trúc).
- **Toolformer** — Schick et al. (NeurIPS 2023), *Language Models Can Teach Themselves to Use Tools* (đánh dấu thẻ gọi công cụ ngay trong luồng sinh văn bản).
- **Reflexion & Self-Refine** — Shinn et al. (NeurIPS 2023) & Madaan et al. (NeurIPS 2023) (tự phản hồi sửa lỗi có trần 1 vòng và cơ chế `CODE_DUMP_NUDGE`).
- **Lost in the Middle** — Liu et al. (TACL 2024), *How Language Models Use Long Contexts* (giữ Đầu/Đuôi + trích lỗi ở giữa trong `smart_truncate` và bố cục U-shaped `[SYSTEM] + TOOLS → [USER REQUEST]`).
- **The Power of Two Choices** — Mitzenmacher (2001) & **Release It!** — Nygard (2018) (cân bằng tải Least-Inflight, Circuit Breaker, cô lập khoang Bulkhead và tự nghỉ luồng khi bị bóp rate-limit).
- **Exponential Backoff and Jitter** — Marc Brooker, *AWS Architecture Blog* (giãn cách lũy tiến `20s/40s` kèm jitter ngẫu nhiên `3–8s` tránh bầy đàn & phát hiện bot).

### Tiêu chuẩn Kỹ thuật
- **RFC 6455** (WebSocket Protocol — client CDP không phụ thuộc thư viện ngoài trong `app/cookie_sync.py`)
- **RFC 8259 & JSON Schema Draft 2020-12** (kiểm định tham số fail-closed trong `app/tool_parser.py`)
- **W3C Fetch Metadata / CSRF Mitigation** (`Origin` & `Sec-Fetch-Site` trong `app/lane_security.py`)
- **Chrome DevTools Protocol (CDP)** · **WHATWG Server-Sent Events (SSE)** · **Model Context Protocol (MCP)** · **Microsoft UI Automation (UIA)**

### Các Dự án Mã Nguồn Mở Gốc & Điểm Cải Tiến Tại Đây

| Dự án nguồn | Vai trò | Điểm gia cố tại `dsh_gemini_bridge` |
|---|---|---|
| [`HanaokaYuzu/Gemini-API`](https://github.com/HanaokaYuzu/Gemini-API) (`gemini-webapi`) | Lõi giao tiếp Gemini Web | Phá deadlock `_safe_init_client`, nạp đủ cookie jar qua CDP (sửa lỗi `osid=1`), retry giữ client khi `502`, cô lập luồng `1213` & `_turn_epoch` |
| [`mangiucugna/json_repair`](https://github.com/mangiucugna/json_repair) | Phục hồi JSON lỗi cú pháp | Chốt bảo toàn `_verify_lossless_heuristic_repair` chống cắt cụt file, giữ link `.md`, lọc fence minh họa |
| [`fastapi/fastapi`](https://github.com/fastapi/fastapi) · [`encode/uvicorn`](https://github.com/encode/uvicorn) · [`encode/httpx`](https://github.com/encode/httpx) | Proxy ASGI chuẩn OpenAI | Lấy lock trước SSE, stream không đệm, heartbeat `2.5s`, chặn CSRF trình duyệt |
| [`lexiforest/curl_cffi`](https://github.com/lexiforest/curl_cffi) | Giả lập TLS/HTTP2 trình duyệt | Timeout `180s`/`120s`/`420s` kèm thử lại giữa stream chống cắt sớm |
| **DeepSeek Harness (`pi-ai`) & [`CursorTouch/Windows-MCP`](https://github.com/CursorTouch/Windows-MCP)** | Coding Agent đích & Driver UIA không ảnh | Profile lọc tool (`coding` 18/34), tách Chat vs Work, test thực chiến qua `--force-renderer-accessibility` |
