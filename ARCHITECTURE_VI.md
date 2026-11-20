# Kiến Trúc Hệ Thống & Luồng Thực Thi (System Architecture v3.1)

> 🌐 **Ngôn ngữ:** [English](ARCHITECTURE.md) | **Tiếng Việt**

Tài liệu này mô tả chi tiết kiến trúc module hóa, cơ chế điều phối Gateway đa luồng, máy trạng thái timeout cho streaming và các lớp kiểm duyệt fail-closed trong **DSH Gemini Agent Bridge v3.1**.

---

## 1. Sơ Đồ Luồng Dữ Liệu Tổng Thể (End-to-End Request Lifecycle)

```text
[DeepSeek Harness (pi-ai) / Coding Agent Client]
        │
        │ 1. POST /v1/chat/completions hoặc /v1/responses (messages + tools + base64 images)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Cổng Frontend Gateway (:10000) — main.py + app/openai_adapter.py        │
│  ├─ Middleware bảo mật (API_KEY / X-Lane-Token / Allowed Hosts)         │
│  ├─ app/title_generation.py: Sinh tiêu đề phiên cục bộ (<5ms)           │
│  └─ app/gateway_router.py (khi Gateway đang active):                    │
│       ├─ Tính session_key (compute_session_key) & phân loại subagent    │
│       ├─ Sticky binding (STICKY_TTL=3600s) + chọn luồng ít inflight nhất│
│       ├─ Lead ưu tiên acc1, Subagent song song tự đẩy sang acc2 / idle   │
│       └─ Stream SSE pass-through + tự failover khi 429/503/ConnectError │
└──────────────┬───────────────────────────────────────┬──────────────────┘
               │ acc1 (:10001)                         │ acc2 (:10002)
               ▼                                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Lớp Biên Dịch OpenAI Trên Worker — app/openai_adapter.py                │
│  ├─ Nạp/cô lập SessionSlot (app/session_pool.py) & FactLedger           │
│  ├─ app/prompt_builder.py: Tool schema (Turn 1), nhắc giao thức ở Turn  │
│  │  sau & Smart Truncation cho kết quả role=="tool"                     │
│  ├─ app/vision.py: Giải Base64 -> nén tùy chọn bằng Pillow -> files=[]  │
│  └─ StreamingResponse + nhịp tim ": keep-alive\n\n" mỗi 2.5s            │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Lõi Điều Phối Worker — app/gemini_runtime.py (SingleChatManager)        │
│  ├─ SessionPoolMixin (app/session_pool.py): 16 slot, cô lập cid/rid/rcid│
│  ├─ AccountPoolMixin (app/account_pool.py): Failover cookie & cooldown  │
│  ├─ HandoffRunnerMixin (app/handoff_runner.py): Tóm tắt Flash bàn giao  │
│  └─ Quản Lý Timeout Stream:                                             │
│       ├─ Hoạt động đầu tiên (thoughts_delta hoặc text_delta): 50s       │
│       ├─ Idle giữa các chunk: 40s | Trần tổng lượt stream: 180s         │
│       └─ Soft-fallback tạm 1 lượt: reset tool_header_sent trên slot,    │
│          gắn lại WORKSPACE TOOLS ngay, xong lượt trả về default_model   │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ 3. Văn bản phản hồi từ Gemini Web
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Tầng Bóc Tách Fail-Closed & Rail-Guard — streaming.py + tool_parser.py  │
│  ├─ ToolCallAwareStreamer ẩn thẻ <tool_call>...</tool_call> khỏi SSE    │
│  ├─ Tách tuyến tính O(N) + json-repair + raw JSON fallback tới 64 KB    │
│  ├─ Auto-Mapper chuẩn hóa tham số (path->file_path, cmd->command)      │
│  └─ Chỉ cần 1 thẻ lỗi/thiếu thẻ đóng -> HỦY CẢ BATCH (failure_traces*)  │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ 4. Trả về OpenAI tool_calls hoặc content
                                   ▼
                [DeepSeek Harness thực thi công cụ cục bộ trên ổ D:]
```

---

## 2. Phân Tích Các Thành Phần Cốt Lõi

### 2.1. Bộ Định Tuyến Gateway (`app/gateway_router.py`)
* **Tự động chia tải Lead & Subagent:** Kích hoạt trong `lifespan()` của [main.py](main.py) khi chạy ở cổng `10000` (`PORT=10000`, `INSTANCE_ID` rỗng/`default`/`frontend`/`gateway` và có `PEER_LANES`).
* **Sticky + Least-Inflight:** Dùng đúng `compute_session_key()` và `is_subagent_messages()` để giữ các lượt sau của cùng một hội thoại bám cố định vào worker đã nhận (`STICKY_TTL=3600s`, tối đa `128` phiên), còn phiên mới sẽ đẩy Lead vào `acc1` và đẩy các Subagent sang worker rảnh/ít `inflight` nhất (`acc2`).
* **Failover & Dọn `inflight` An Toàn:** Khi worker lỗi kết nối hoặc trả HTTP `429`/`503`, Gateway đưa lane đó vào cooldown (`15s`) và thử lại 1 lần sang worker khỏe còn lại. Khi client ngắt SSE giữa chừng, khối `finally` đóng stream upstream và trả `inflight` về `0`.

### 2.2. Lõi Thực Thi Worker, Sticky Session Pool & Account Failover
* **`app/gemini_runtime.py` (`SingleChatManager`):** Gom toàn bộ luồng non-stream và stream về một đường chạy duy nhất dưới `asyncio.Lock`, hỗ trợ giãn cách `MIN_REQUEST_INTERVAL`, phục hồi sau lỗi và tự động reset metadata dở dang.
* **`app/session_pool.py` (`SessionPoolMixin`):** Lưu tối đa `16` ngăn phiên (`SESSION_POOL_TTL=3600s`) tách biệt `cid/rid/rcid` giữa Lead và từng Subagent. Lead ghi `FactLedger` ra `handoff/<id>/ledger.json`, còn Subagent dùng `FactLedger(..., persist=False)` trong RAM để không đụng file sổ cái của Lead.
* **`app/account_pool.py` & `app/handoff_runner.py`:** Tự động chuyển sang tài khoản dự phòng (`BACKUP_1PSID`..`BACKUP_4PSID`) khi cạn quota hoặc lỗi auth kèm cooldown lưu trong `.bridge_accounts*.json`, và gọi Flash model tóm tắt `FactLedger` để bàn giao mạch làm việc sang tài khoản mới.

### 2.3. Timeout Nhận Diện `thoughts_delta` & Soft-Fallback Tạm Thời
* `_iterate_stream_until()` coi **cả `thoughts_delta` lẫn `text_delta`** là tín hiệu đang hoạt động, tránh cắt oan các model thinking như `gemini-neon` trong lúc suy nghĩ.
* **Ngân sách 3 lớp:** chờ hoạt động đầu tiên trong `min(GEMINI_HARD_TIMEOUT=90s, MODEL_FIRST_BYTE_FALLBACK_TIMEOUT=50s)`; khi đã có `thoughts_delta`/`text_delta` thì chuyển sang idle timeout `GEMINI_CHUNK_IDLE_TIMEOUT=40s` giữa mỗi chunk dưới trần tổng `GEMINI_STREAM_OVERALL_TIMEOUT=180s`.
* **Soft-Fallback theo lượt:** Nếu quá `50s` chưa có hoạt động (hoặc dừng idle ở pha thinking trước khi có `text_delta`), worker tạo phiên tạm bằng `model_override` cho riêng lượt đó, đặt lại `tool_header_sent=False` trên đúng `SessionSlot` để gắn lại `=== WORKSPACE TOOLS ===` ngay ở lượt retry, khôi phục `default_model` trước khi lưu `.bridge_session*.json` và ghi rõ `model` + `fallback_from` vào `logs/activity*.jsonl`.

### 2.4. Gửi Ảnh Trực Tiếp & Smart Truncation (`app/vision.py`, `app/prompt_builder.py`)
* **Gửi ảnh thẳng (`files=[]`):** Giải mã Base64, tùy chọn nén JPEG với `pillow` (tối đa `1600x1600`, `Image.MAX_IMAGE_PIXELS=50_000_000`) rồi gửi trực tiếp qua `send_message_stream(..., files=...)`; dọn `_TEMP_FILES` trong `finally`. Không còn các tầng OCR cục bộ.
* **Giao thức `<tool_call>` trung tính:** Trình bày `<tool_call>{...}</tool_call>` như plain-text IDE markup không giới hạn số lượt gọi (tránh wording gây refusal), kèm câu nhắc ngắn ở lượt `tool` và lượt `user` nối tiếp khi request có `tools`.
* **Smart Truncation:** Chỉ áp dụng với output `role == "tool"` vượt `MAX_TOOL_RESULT_CHARS` (`12,000`), ưu tiên giữ lại các dòng lỗi/stacktrace ở phần giữa.

### 2.5. Parser Fail-Closed & Rail-Guard Ổ C (`app/tool_parser.py`, `app/security.py`)
* **Fail-Closed Cả Batch:** Trích xuất `<tool_call>`, sửa JSON nhẹ qua `json-repair`, hỗ trợ raw JSON fallback tới `64 KB` và chuẩn hóa tham số qua `Auto-Mapper`. Chỉ cần 1 thẻ lỗi hoặc thiếu thẻ đóng `</tool_call>`, toàn bộ batch bị hủy (`HTTP 502` / SSE `invalid_tool_call`), không thực thi nửa vời, và lưu raw reply vào `logs/failure_traces*.jsonl`.
* **Rail-Guard Ổ D (`app/security.py`):** Kiểm tra `os.path.realpath` theo `DEFAULT_PROJECT_DIR` và chặn các mẫu PowerShell / đường dẫn can thiệp ổ `C:\`.

### 2.6. Giám Sát, Mã Lỗi OpenAI & Vòng Đời Kết Nối
* **Nhật ký & Telemetry:** Log xoay vòng (`logs/bridge*.log`), nhật ký JSONL (`logs/activity*.jsonl`), vết lỗi tool (`logs/failure_traces*.jsonl`), cùng `/health`, `/v1/status` và `/v1/gateway/routes`.
* **Ánh xạ mã lỗi chuẩn OpenAI (`app/errors.py`):**
  * `429` (`bridge_busy`): Quá thời gian chờ `LOCK_ACQUIRE_TIMEOUT`.
  * `502` (`gemini_upstream_error` / `invalid_tool_call`): Lỗi kết nối upstream hoặc batch tool call lỗi.
  * `503` (`bridge_disabled` / `gemini_not_authenticated` / `no_healthy_lane`): Bridge đang tắt, cookie hết hạn hoặc không còn worker khỏe.
  * `504` (`gemini_timeout`): Quá thời hạn chờ đầu/idle/tổng lượt.
* **Graceful Draining & Origin Guard (`app/control.py`, `app/lane_security.py`):** Vòng đời `off` → `starting` → `on` → `stopping` chờ request hoàn tất tới `DISABLE_DRAIN_TIMEOUT` trước khi ngắt client, và chặn request trình duyệt khác Origin.
