# Kiến Trúc Hệ Thống, Thuật Toán & Tài Liệu Nghiên Cứu Tham Khảo (v3.2)

> 🌐 **Ngôn ngữ:** [English](ARCHITECTURE.en.md) | **Tiếng Việt** | [한국어](ARCHITECTURE.ko.md)  
> 📘 **README:** [English](../README.md) | [Tiếng Việt](README.vi.md) | [한국어](README.ko.md)

---

## 1. Vòng Đời Request Từ Đầu Đến Cuối

```text
[Coding Agent Client (DeepSeek Harness / pi-ai / OpenAI SDK)]
        │  POST /v1/chat/completions hoặc /v1/responses (+ tùy chọn X-Workspace-Dir)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Frontend Gateway (:10000) — main.py + app/gateway_router.py             │
│  ├─ Middleware Bảo Mật: Chống DNS-rebinding, API_KEY, X-Lane-Token      │
│  ├─ Nhánh cục bộ siêu nhanh: app/title_generation.py (<5ms đặt tiêu đề) │
│  ├─ Định tuyến: Sticky Affinity (3600s) + Least-Inflight + Lọc Model    │
│  ├─ Failover SSE thật: X-Gateway-Failover-Ready: 1 (2s fast-fail -> 429)│
│  └─ Điều khiển Profile Vault: /v1/profiles/* (chỉ loopback 127.0.0.1)   │
└──────────────┬───────────────────────────────────────┬──────────────────┘
               │ acc1 (:10001)                         │ acc2 (:10002)
               ▼                                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Worker OpenAI Adapter — app/openai_adapter.py                           │
│  ├─ Xác thực model chỉ-đọc trước khi lấy turn lock                      │
│  ├─ Lấy turn lock TRƯỚC khi trả StreamingResponse(200) -> 429 failover  │
│  ├─ Kích hoạt SessionSlot độc lập (model, workspace_root, FactLedger)   │
│  ├─ app/prompt_builder.py: Delta prompt, Context Bootstrap, chia đều    │
│  │  ngân sách tool_result song song (36k), Smart Truncate giữ đầu/đuôi  │
│  └─ StreamingResponse + nhịp tim SSE ": keep-alive\n\n" mỗi 2.5 giây    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Lõi Thực Thi Worker — app/gemini_runtime.py + app/account_pool.py       │
│  ├─ _safe_init_client chống deadlock (25s init / 420s stream thinking)  │
│  ├─ Auto Cookie Sync: tự sync ngầm qua CDP khi gặp UNAUTHENTICATED      │
│  ├─ Timeout cho model thinking: 180s đầu tiên / 120s idle / 420s tổng   │
│  └─ Tự thử lại 1 lần khi phản hồi trống + phục hồi 1097 có Bootstrap    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Cổng Kiểm Soát Fail-Closed — app/streaming.py + app/tool_parser.py      │
│  ├─ Streamer FSM O(N) (không nuốt chữ khi nhắc `<tool_call>` trong text)│
│  ├─ Dọn rác Gemini Web: gỡ auto-link URL ([url](url)->url), lọc <Image> │
│  ├─ Sửa JSON bằng json-repair + kiểm tra JSON Schema (giới hạn 500k)    │
│  ├─ Tự sửa kín 1 vòng trước khi phát lỗi invalid_tool_call              │
│  └─ Bảo vệ đường dẫn/lệnh phá OS (app/security.py)                      │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
                 [Client thực thi tool trong workspace đang mở]
```

---

## 2. Các Phân Hệ Chính

### 2.1. Bộ Định Tuyến Gateway (`app/gateway_router.py`)
- Tự kích hoạt khi có `PEER_LANES` và `GATEWAY_MODE=auto|on`.
- Kết hợp **Sticky Session Affinity** (`3600s`, tối đa `128` phiên), **Least-Inflight** (chọn worker ít lượt đang chạy nhất), **lọc theo `available_models`**, và **Circuit Breaker cooldown** (`15s` khi gặp `429`/`503`/`ConnectError`).
- Worker lấy turn lock **trước** khi trả `StreamingResponse(200)` (chờ nhanh `2.0s` khi có `X-Gateway-Failover-Ready: 1`), giúp Gateway chuyển hướng SSE sang worker rảnh một cách thực sự.

### 2.2. Cô Lập Phiên & Model Theo Slot (`app/session_pool.py`, `app/gemini_runtime.py`)
- Mỗi hội thoại sở hữu 1 `SessionSlot` riêng (`16` slot, TTL `3600s`) chứa `cid/rid/rcid`, `model`, `workspace_root`, `tool_header_hash`, và `FactLedger`.
- Hai agent dùng 2 model khác nhau chạy xen kẽ trên cùng 1 worker không bao giờ ghi đè `default_model` hay làm lẫn phiên của nhau.

### 2.3. Kho Profile Vault & Đồng Bộ Cookie Ngầm qua CDP (`app/cookie_sync.py`)
- Tách N tài khoản Google vào các thư mục `--user-data-dir` độc lập dưới `.cookie_profiles/` (không chạm Chrome cá nhân, không lưu mật khẩu).
- **Đăng nhập 1 lần:** bật Chrome/Edge thật không có cờ automation để qua Google 2FA/Passkey bình thường.
- **Sync ngầm:** chạy `--headless=new --remote-debugging-port=0 --remote-debugging-address=127.0.0.1`, kết nối bằng client **RFC 6455 WebSocket thuần Python** (`CdpConnection`), trích xuất đủ bộ cookie `.google.com` + `gemini.google.com` (`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID` — loại trừ `LSID`/`__Host-GAPS` của `accounts.google.com`), ghi nguyên tử vào `.gemini_cookies_<lane>/.cached_cookies_<PSID>.json` và `.env.<lane>`, đóng sạch bằng `Browser.close`.
- Tuần tự hóa qua `asyncio.Semaphore(1)` nên sync hàng loạt 5–10 acc chỉ tốn 1 tiến trình ngắn rồi về 0 MB RAM.

---

## 3. Các Thuật Toán & Cấu Trúc Dữ Liệu Đã Sử Dụng

| Thành phần | Thuật toán / Mô hình | Độ phức tạp | File nguồn |
|---|---|---|---|
| **Ẩn thẻ `<tool_call>` khi stream SSE** | Máy trạng thái hữu hạn (FSM) 1 lượt duyệt kèm bộ đệm nhìn trước tiền tố & kiểm tra dấu backtick `` ` `` | $O(N)$ thời gian, $O(1)$ bộ đệm | `app/streaming.py` |
| **Phân tích & sửa lỗi Tool Call** | Bộ quét ngoặc/chuỗi nhận biết escape, xóa dấu phẩy thừa ngoài chuỗi, `json-repair`, đệ quy gỡ URL auto-link, đối chiếu JSON Schema | $O(N)$ 1 lượt | `app/tool_parser.py` |
| **Tự sửa nội bộ có giới hạn** | Vòng phản hồi lỗi cú pháp/schema tối đa 1 lượt trước khi fail-closed | $O(1)$ vòng lặp | `app/openai_adapter.py` |
| **Cắt ngắn thông minh (Smart Truncate)** | Giữ Đầu (2/3) + Đuôi (1/3) + trích các dòng lỗi/traceback ở giữa + chia ngân sách công bằng cho nhiều tool song song | $O(L)$ theo số dòng | `app/prompt_builder.py` |
| **Cân bằng tải Gateway** | Bảng băm Sticky Affinity + chọn min Least-Inflight + Circuit Breaker theo thời gian nghỉ | $O(P)$ mỗi request ($P$ = số peer) | `app/gateway_router.py` |
| **Hồ phiên (Session Pool)** | Bảng băm có trần LRU/TTL (`16` slot) + `asyncio.Lock` tái nhập theo Task | $O(1)$ tra cứu, $O(S)$ loại bỏ | `app/session_pool.py` |
| **Giao vận CDP cục bộ** | Client WebSocket RFC 6455 thuần stdlib (khung 7/16/64-bit, ghép frame phân mảnh, Ping/Pong) + hàng đợi sự kiện | $O(B)$ theo byte | `app/cookie_sync.py` |
| **Khởi tạo chống Deadlock** | Bọc `asyncio.wait_for` tách riêng trần init (`25s`) và trần stream thinking (`420s`) | $O(1)$ | `app/account_pool.py` |
| **Ghi cấu hình nguyên tử** | Ghi ra file tạm + `os.replace()` nguyên tử cho `.env*`, `vault.json`, và cookie cache | $O(F)$ theo kích thước file | `app/env_update.py`, `app/cookie_sync.py` |

---

## 4. Các Bài Nghiên Cứu & Tiêu Chuẩn Kỹ Thuật Tham Khảo

1. **ReAct:** Yao et al. (2023), *ReAct: Synergizing Reasoning and Acting in Language Models* (ICLR 2023). — Cơ sở cho vòng lặp xen kẽ suy luận (`thoughts_delta`), gọi hành động có cấu trúc và đọc kết quả quan sát từ môi trường.
2. **Toolformer:** Schick et al. (2023), *Toolformer: Language Models Can Teach Themselves to Use Tools* (NeurIPS 2023). — Nền tảng cho giao thức nhúng thẻ gọi công cụ ngay trong văn bản (`<tool_call>...</tool_call>`) trên kênh web không có function-calling native.
3. **Reflexion & Self-Refine:** Shinn et al. (2023), *Reflexion: Language Agents with Verbal Reinforcement Learning* (NeurIPS 2023); Madaan et al. (2023), *Self-Refine: Iterative Refinement with Self-Feedback* (NeurIPS 2023). — Cơ sở cho cơ chế tự sửa lỗi cú pháp/schema tối đa 1 vòng kín trước khi báo lỗi.
4. **Lost in the Middle:** Liu et al. (2024), *Lost in the Middle: How Language Models Use Long Contexts* (TACL 2024). — Lý giải vì sao `smart_truncate()` ưu tiên giữ đầu, giữ đuôi và kéo các dòng lỗi/stacktrace ở giữa ra vị trí dễ chú ý.
5. **Cân bằng tải & Kỹ thuật chịu lỗi:** Mitzenmacher (2001), *The Power of Two Choices in Randomized Load Balancing* (IEEE TPDS); Nygard (2018), *Release It!* (mẫu Circuit Breaker & Bulkhead). — Áp dụng cho định tuyến Least-Inflight, cooldown theo luồng và tách biệt tiến trình worker.
6. **Chuẩn giao thức:**
   - **RFC 6455** (The WebSocket Protocol — cài đặt thuần stdlib trong `app/cookie_sync.py`).
   - **RFC 8259 / JSON Schema Draft 2020-12** (kiểm định tham số tool trong `app/tool_parser.py`).
   - **Chrome DevTools Protocol (CDP)** (`Storage.getCookies`, `Target.createTarget`, `Page.loadEventFired`, `Runtime.evaluate`, `Browser.close`).
   - **WHATWG Server-Sent Events (SSE)** (stream chuẩn OpenAI kèm keep-alive heartbeat).
