# DSH Gemini Agent Bridge (v3.2)

> 🌐 **Ngôn ngữ:** [English](../README.md) | **Tiếng Việt** | [한국어](README.ko.md)  
> 📐 **Kiến trúc, Thuật toán & Nghiên cứu tham khảo:** [English](ARCHITECTURE.en.md) | [Tiếng Việt](ARCHITECTURE.vi.md) | [한국어](ARCHITECTURE.ko.md)

**DSH Gemini Agent Bridge** là cổng proxy tương thích chuẩn OpenAI (`/v1/models`, `/v1/chat/completions`, `/v1/responses`) và bộ điều phối đa luồng cho Coding Agent (DeepSeek Harness / `pi-ai`, IDE Agent, OpenAI SDK) chạy trên **Gemini Web**, hỗ trợ stream SSE thời gian thực, failover đa tài khoản, kho **Profile Vault tự nạp cookie ngầm không mở tab**, và cơ chế gọi tool fail-closed.

---

## Tính năng nổi bật

- **Cổng Frontend Gateway đa luồng (`:10000` → `:10001`, `:10002`, ...):** Ghim phiên hội thoại (Sticky Session), cân bằng tải theo ít request đang chạy nhất (Least-Inflight), định tuyến theo model mà worker hỗ trợ, lấy turn lock trước khi mở SSE để failover thật khi gặp HTTP `429`.
- **100% Model do người dùng chọn & tách biệt theo phiên:** Hiển thị toàn bộ model tài khoản Gemini thực có (không hardcode ưu tiên hay ẩn model). Mỗi `SessionSlot` giữ riêng model và workspace của mình; `FALLBACK_MODEL` mặc định tắt để không tự ý đổi model sau lưng người dùng.
- **Mở khóa thư mục dự án tự do:** Tự nhận diện thư mục workspace người dùng đang mở trong IDE/đoạn chat trên mọi ổ đĩa (`C:`, `D:`, `E:`, ...), chỉ chặn các đường dẫn/lệnh phá hệ điều hành (`System32`, UNC, `format`, `diskpart`, `IWR | IEX`...). Hỗ trợ `WORKSPACE_SANDBOX_MODE=client|workspace`.
- **Kho Profile Vault & Tự Nạp Cookie Ngầm (0 tab mở nền):** Quản lý không giới hạn N tài khoản Google (`acc1`, `acc2`, `work3`...) mà **không lưu mật khẩu** và **không cần mở tab Chrome liên tục**. Chỉ đăng nhập tay + 2FA đúng 1 lần trên profile Chrome/Edge tách biệt; mọi lần sau chạy ẩn `--headless=new` qua CDP WebSocket thuần Python trong ~2–4 giây, xuất đủ bộ cookie Google/Gemini (`OSID`, `__Secure-OSID`, `SID`, `__Secure-1PSID`, `__Secure-1PSIDTS`, `__Secure-1PSIDCC`, `NID`) và nạp nóng trực tiếp vào worker.
- **Gọi Tool Fail-Closed & Tự sửa nội bộ:** Streamer FSM $O(N)$ bảo vệ thẻ `<tool_call>` (không nuốt chữ khi nhắc `` `<tool_call>` `` trong văn bản), gỡ lỗi tự gắn link Markdown của Gemini Web (`[http://...](http://...)` → `http://...`), sửa JSON bằng `json-repair`, tự sửa kín 1 vòng trước khi báo `invalid_tool_call` và tự thử lại 1 lần nếu Gemini trả phản hồi trống.
- **Timeout rộng rãi cho model thinking & code dài:** Mặc định `GEMINI_HARD_TIMEOUT=180s` (3 phút), `GEMINI_CHUNK_IDLE_TIMEOUT=120s` (2 phút), `GEMINI_STREAM_OVERALL_TIMEOUT=420s` (7 phút), giữ kết nối bằng SSE keep-alive mỗi `2.5s`, kết hợp chốt chống deadlock `25s` lúc khởi tạo (`_safe_init_client`) và chia ngân sách công bằng cho nhiều `tool_result` song song (`MAX_TOTAL_TOOL_RESULTS_CHARS=36000`).
- **Dashboard Web trực tiếp (`/dashboard`):** Giao diện song ngữ Việt/Anh điều khiển từng luồng, đổi model, quản lý Kho Profile (`Đăng nhập 1 lần`, `Xong & Sync`, `Sync ngầm`, `Sync tất cả`), xem thông số & log trực tiếp.

---

## Báo Cáo Kỹ Thuật Tóm Tắt

### 1. Bài Toán & Các Cải Tiến Cốt Lõi (v1 → v3.2)

| Hạng mục | Hạn chế của wrapper web thuần | Cải tiến thực tế trong DSH Bridge v3.2 |
|---|---|---|
| **Phiên & Xác thực** | Phải copy cookie F12 thủ công; Google `osid=1` từ chối nếu chỉ gửi `1PSID+1PSIDTS`; deadlock `_lock` trong `gemini-webapi` khi gặp HTTP 400 | **Profile Vault + Headless CDP Sync** (`app/cookie_sync.py`): đăng nhập thật 1 lần, sync ngầm 2–4 giây lấy đủ bộ cookie (`OSID`, `SID`, `1PSID`, `1PSIDTS`, `1PSIDCC`, `NID`), **0 tab mở**, kèm chốt phá deadlock `25s` `_safe_init_client` |
| **Model & Cô lập phiên** | Hardcode model ưu tiên; đổi model làm trộn phiên giữa các agent song song | **100% Model do người dùng chọn & `SessionSlot` Pool** (`app/session_pool.py`): tách biệt `(cid/rid/rcid, model, workspace, FactLedger)` theo từng hội thoại; `FALLBACK_MODEL` chỉ bật khi người dùng chọn |
| **Độ tin cậy gọi Tool** | Gemini Web tự biến URL thành link Markdown (`[http://...](http://...)`), xuất JSON lỗi hoặc cắt cụt `<tool_call>` | **Cổng Fail-Closed + Tự sửa nội bộ** (`app/streaming.py`, `app/tool_parser.py`): Streamer FSM $O(N)$ chống nuốt backtick, đệ quy gỡ URL auto-link, `json-repair`, tự sửa kín 1 vòng và thử lại 1 lần khi trả rỗng |
| **Đa luồng & Model Thinking** | Mở SSE `200` trước khi lấy khóa (mất failover); đọc nhiều file song song làm model thinking bị cắt timeout sớm | **Lấy Turn Lock trước SSE (`429` failover thật)** + **Gateway Sticky/Least-Inflight** (`app/gateway_router.py`) + **ngân sách tool 36k** + **timeout 180s/120s/420s** ưu tiên làm xong trọn vẹn |
| **An toàn Workspace** | Khóa cứng ổ `D:\ABC`, chặn nhầm dự án trên ổ `C:`/`E:` | **Mở khóa Workspace động** (`app/security.py`): bám theo thư mục IDE trên mọi ổ đĩa, chỉ chặn đường dẫn/lệnh phá hệ điều hành |

### 2. Các Thuật Toán Chính Đã Áp Dụng
- **Streamer FSM 1 lượt duyệt (`app/streaming.py`):** Độ phức tạp $O(N)$ với bộ đệm nhìn trước tiền tố và theo dõi dấu `` ` `` để ẩn `<tool_call>` trong SSE mà không nuốt văn bản thường.
- **Phân tích Tool Call & Tự sửa có trần (`app/tool_parser.py`, `app/openai_adapter.py`):** Bộ quét ngoặc nhận biết chuỗi, xóa dấu phẩy thừa ngoài chuỗi, `json-repair`, kiểm định JSON Schema và tự sửa tối đa 1 vòng phản hồi lỗi.
- **Smart Truncate giữ vùng trọng yếu (`app/prompt_builder.py`):** Giữ Đầu (2/3) + Đuôi (1/3) + kéo các dòng lỗi/traceback ở giữa, chia ngân sách công bằng cho nhiều `tool_result` song song và tự bơm `Context Bootstrap` khi đổi phiên giữa chừng.
- **Cân bằng tải Sticky + Least-Inflight + Circuit Breaker (`app/gateway_router.py`):** Ghim phiên (`3600s`), chọn worker ít tải nhất, lọc theo model khả dụng và nghỉ `15s` khi lỗi.
- **Client CDP RFC 6455 thuần Python (`app/cookie_sync.py`):** Đóng/mở khung WebSocket chuẩn (7/16/64-bit, ghép frame phân mảnh, Ping/Pong) không cần thư viện ngoài, chạy tuần tự qua `asyncio.Semaphore(1)`.

### 3. Bài Nghiên Cứu & Tiêu Chuẩn Tham Khảo
- **ReAct** — Yao et al. (ICLR 2023), *Synergizing Reasoning and Acting in Language Models*.
- **Toolformer** — Schick et al. (NeurIPS 2023), *Language Models Can Teach Themselves to Use Tools*.
- **Reflexion & Self-Refine** — Shinn et al. (NeurIPS 2023) & Madaan et al. (NeurIPS 2023).
- **Lost in the Middle** — Liu et al. (TACL 2024), *How Language Models Use Long Contexts*.
- **Load Balancing & Resilience** — Mitzenmacher (2001), *The Power of Two Choices*; Nygard (2018), *Release It!* (Circuit Breaker & Bulkhead).
- **Tiêu chuẩn kỹ thuật:** RFC 6455 (WebSocket), RFC 8259 & JSON Schema Draft 2020-12, Chrome DevTools Protocol (CDP), WHATWG SSE.

### 4. Các Repo Mã Nguồn Mở Tham Gia & Điểm Đã Cải Tiến

| Repo / Dự án nguồn | Vai trò trong hệ thống | Cải tiến & mở rộng tại DSH Bridge |
|---|---|---|
| [`HanaokaYuzu/Gemini-API`](https://github.com/HanaokaYuzu/Gemini-API) (`gemini-webapi`) | Lõi kết nối Gemini Web (`ChatSession`, `batchexecute`, `RotateCookies`) | Bọc `_safe_init_client` phá deadlock HTTP 400, bơm đủ bộ cookie qua CDP (sửa lỗi `osid=1`), cô lập `GEMINI_COOKIE_PATH` theo từng worker |
| [`mangiucugna/json_repair`](https://github.com/mangiucugna/json_repair) | Phục hồi JSON bị lỗi cú pháp | Kết hợp bộ quét chuỗi $O(N)$, gỡ auto-link URL, chuẩn hóa alias tham số và kiểm định fail-closed theo JSON Schema |
| [`fastapi/fastapi`](https://github.com/fastapi/fastapi) · [`encode/uvicorn`](https://github.com/encode/uvicorn) · [`encode/httpx`](https://github.com/encode/httpx) | Máy chủ ASGI chuẩn OpenAI & proxy Gateway | Lấy turn lock trước SSE, pass-through không đệm (`read=None`), heartbeat `2.5s`, khóa bảo mật loopback/Origin |
| [`lexiforest/curl_cffi`](https://github.com/lexiforest/curl_cffi) | Giả lập TLS/HTTP2 trình duyệt (qua `gemini-webapi`) | Tinh chỉnh trần stream `420s` / watchdog `180s` cho các lượt thinking kéo dài |
| **DeepSeek Harness (`pi-ai`) & OpenAI SDK Clients** | Hệ sinh thái Coding Agent đích | Tương thích trọn vẹn `/v1/chat/completions` & `/v1/responses`, đặt tiêu đề cục bộ `<5ms`, điều phối song song nhiều subagent |

> 📄 Xem sơ đồ kiến trúc chi tiết và bảng độ phức tạp đầy đủ: [docs/ARCHITECTURE.vi.md](ARCHITECTURE.vi.md)

---

## Cấu trúc thư mục

```text
dsh-gemini-bridge/
├── app/                     # Lõi runtime, Gateway router, Profile Vault CDP, tool parser, bảo mật
├── docs/                    # Tài liệu 3 ngôn ngữ (EN/VI/KO): README & Kiến trúc / Thuật toán / Nghiên cứu
├── frontend/                # Dashboard SPA không cần build (/dashboard)
├── scripts/                 # Công cụ CLI (cookie_sync.py, live_smoke.py) & bộ launchers/ đơn lẻ
├── tests/                   # 230 bài kiểm thử đơn vị & tích hợp chạy 100% offline
├── main.py                  # Điểm khởi chạy FastAPI (Gateway hoặc Worker theo BRIDGE_INSTANCE)
├── requirements.txt         # Thư viện Python tối giản
├── run_all.bat              # Khởi động nhanh toàn hệ thống (Gateway :10000 + Worker :10001, :10002)
├── stop_all.bat             # Dừng sạch toàn bộ tiến trình trên cổng 10000..10002
└── .env.example             # Mẫu cấu hình đầy đủ
```

---

## Hướng dẫn chạy nhanh

### 1. Cài đặt thư viện
```powershell
pip install -r requirements.txt
```

### 2. Tạo file cấu hình
Copy `.env.example` thành `.env` (Gateway cổng `:10000`) và `.env.acc1`, `.env.acc2` (Worker cổng `:10001`, `:10002`):
- Đặt cùng một `LANE_ADMIN_TOKEN` cho cả `.env`, `.env.acc1`, `.env.acc2`.
- Trong `.env`, cấu hình `PEER_LANES="acc1=http://127.0.0.1:10001,acc2=http://127.0.0.1:10002"`.

### 3. Đăng nhập 1 lần vào Kho Profile (không cần copy F12)
```powershell
python scripts/cookie_sync.py login acc1 --lane acc1 --slot 0 --label "Work 1"
python scripts/cookie_sync.py login acc2 --lane acc2 --slot 0 --label "Work 2"
# Thêm tài khoản dự phòng (slot 1..4) hoặc luồng mới bất cứ lúc nào:
python scripts/cookie_sync.py login work3 --lane acc1 --slot 1 --label "Backup 1"
```
Làm mới toàn bộ tài khoản chạy ngầm (0 cửa sổ trình duyệt):
```powershell
python scripts/cookie_sync.py sync-all
python scripts/cookie_sync.py list
```

### 4. Khởi động & sử dụng
```powershell
.\run_all.bat
# Mở Dashboard: http://127.0.0.1:10000/dashboard
# OpenAI Base URL: http://127.0.0.1:10000/v1
# Dừng toàn bộ hệ thống:
.\stop_all.bat
```

---

## Kiểm thử & An toàn
```powershell
python -B -m unittest discover -s tests
ruff check --no-cache --select F,E9,B main.py app tests scripts
```
- Toàn bộ file bí mật (`.env*`, `.cookie_profiles/`, `.gemini_cookies*/`, `logs/`, `.bridge_*.json`) đều nằm trong `.gitignore`.
- Các route `/v1/profiles/*` chỉ chấp nhận truy cập từ máy cục bộ (`127.0.0.1`) và kiểm tra Origin.
