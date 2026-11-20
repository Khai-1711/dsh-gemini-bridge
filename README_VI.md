# DSH Gemini Agent Bridge (v3.1 - Bản Kiến Trúc Gateway Modular)

> 🌐 **Ngôn ngữ:** [English](README.md) | **Tiếng Việt**

> **Cổng Proxy Chuẩn OpenAI, Bộ Chia Tải Đa Luồng (Gateway `:10000`) & Cầu Nối Thực Thi Agent cho Gemini Web (`gemini_webapi`) trên DeepSeek Harness (`pi-ai`), Cline và Roo Code.**

Biến giao diện **Google Gemini Web** thành Backend chuẩn **OpenAI Chat Completions** (`/v1/chat/completions`, `/v1/responses`) hỗ trợ **Tool Calling (`<tool_call>`)**, **chia tải song song Lead + Subagent qua Gateway (`:10000`)**, **gửi ảnh trực tiếp**, **Sticky Session Pool**, **Cookie Failover + Flash Handoff** và **Web Dashboard W3C Custom Elements**.

---

## Điểm Nhấn Kỹ Thuật (v3.1)

1. **Bộ Định Tuyến Gateway Thông Minh (`app/gateway_router.py` trên `:10000`):** Tự động tách **Lead** và các **Subagent** chạy song song qua hai luồng worker (`acc1` `:10001` & `acc2` `:10002`) theo `session_key` bám dính (`STICKY_TTL=3600s`) + chọn luồng ít `inflight` nhất, stream SSE pass-through không buffer, tự failover khi gặp `429`/`503`/`ConnectError`, giải phóng `inflight` ngay khi client ngắt kết nối và sinh tiêu đề phiên cục bộ (`GET /v1/gateway/routes`).
2. **Sticky Session Pool & Tách Ledger Độc Lập (`app/session_pool.py`, `app/handoff.py`):** Cô lập từng ngăn phiên chat (`cid`/`rid`/`rcid`) cho **Lead** (`handoff/<id>/ledger.json` lưu đĩa) và tối đa **16 Subagent** đồng thời (`FactLedger` in-memory để không ghi đè lẫn nhau).
3. **Timeout Theo Hoạt Động (`thoughts_delta` + `text_delta`) & Soft-Fallback Tạm Thời (`app/gemini_runtime.py`):** Ghi nhận cả `thoughts_delta` lẫn `text_delta` là luồng còn sống; áp dụng `MODEL_FIRST_BYTE_FALLBACK_TIMEOUT` (`50s`), `GEMINI_CHUNK_IDLE_TIMEOUT` (`40s`/chunk) và `GEMINI_STREAM_OVERALL_TIMEOUT` (`180s`). Khi fallback do timeout: **chỉ đổi model cho riêng lượt đó** (không hạ cấp vĩnh viễn `default_model`), reset `tool_header_sent=False` trên đúng `SessionSlot` và **gắn lại ngay `=== WORKSPACE TOOLS ===`** ở lượt retry, đồng thời ghi đúng `model` thực tế + `fallback_from` vào `logs/activity*.jsonl`.
4. **Parser Batch Fail-Closed (`app/tool_parser.py`, `app/streaming.py`):** Tách `<tool_call>` tuyến tính `O(N)` kết hợp `json-repair` và fallback raw JSON tới `64 KB` (`MAX_RAW_JSON_FALLBACK_CHARS=65536`). Nếu trong batch có bất kỳ thẻ nào vỡ/thiếu thẻ đóng, hệ thống **từ chối cả batch** kèm chẩn đoán rõ (`thẻ #N`), lưu vết vào `logs/failure_traces*.jsonl` và tuyệt đối không chạy dở dang một nửa batch.
5. **Gửi Ảnh Trực Tiếp Không Qua OCR Cục Bộ (`app/vision.py`):** Trích xuất ảnh Base64, nén tối ưu JPEG (nếu có `pillow` tùy chọn) và upload thẳng lên Gemini Web qua `files=[]`.
6. **Multi-Account Failover & Flash Handoff (`app/account_pool.py`, `app/handoff_runner.py`):** Tự động xoay tài khoản khi cạn quota/lỗi auth với cooldown từng slot (`QUOTA_COOLDOWN_HOURS`, `AUTH_COOLDOWN_MINUTES`) và dùng Flash tóm tắt `FactLedger` bàn giao sang tài khoản kế tiếp.
7. **SSE Heartbeat & Khóa Tuần Tự Theo Worker:** Phát `: keep-alive\n\n` mỗi `2.5s` chống idle timeout và tuần tự hóa request trên mỗi worker bằng `asyncio.Lock(timeout=LOCK_ACQUIRE_TIMEOUT)`.
8. **Tường Lửa Ổ C (`app/security.py`):** Chuẩn hóa đường dẫn bằng `os.path.realpath` theo `DEFAULT_PROJECT_DIR` và chặn các truy cập/lệnh PowerShell nguy hiểm chạm tới ổ `C:\`.
9. **Dashboard Web Components (`frontend/`):** Giao diện quản trị thuần **W3C Custom Elements + ES Modules** (không cần Node.js/build), hỗ trợ EN/VI, Dark/Light, điều khiển đa luồng và nạp nóng cookie.

---

## Cấu Trúc Dự Án

```text
dsh_gemini_bridge/
├── app/
│   ├── __init__.py
│   ├── accounts.py           # Nạp chuỗi tài khoản & phát hiện trùng tài khoản giữa các luồng
│   ├── account_pool.py       # Failover tài khoản, quản lý cooldown & gọi Flash handoff
│   ├── activity.py           # Nhật ký activity*.jsonl & failure_traces*.jsonl từng instance
│   ├── config.py             # Đường dẫn theo instance, .env, cấu hình timeout & logger xoay vòng
│   ├── control.py            # Đọc API_START_STATE & chuẩn hóa trạng thái khởi động
│   ├── env_update.py         # Cập nhật nóng cookie vào file .env*
│   ├── errors.py             # Ánh xạ BridgeError -> mã HTTP & JSON chuẩn OpenAI
│   ├── gateway_router.py     # Bộ chia tải Gateway Sticky + Least-Inflight + Failover (:10000)
│   ├── gemini_runtime.py     # Lõi SingleChatManager, vòng stream thống nhất & fallback
│   ├── gemini_session.py     # Facade tương thích ngược xuất session_manager & hằng số
│   ├── handoff.py            # FactLedger ghi nhận mục tiêu/file/lỗi theo slot
│   ├── handoff_runner.py     # Sinh bản tóm tắt bàn giao khi chuyển tài khoản
│   ├── lane_security.py      # Kiểm tra Host, Origin, LAN bind & X-Lane-Token
│   ├── lanes.py              # LaneRegistry thăm dò trạng thái, proxy & spawn worker cục bộ
│   ├── openai_adapter.py     # Router /v1/models, /v1/chat/completions, /v1/responses
│   ├── prompt_builder.py     # Biên dịch tool schema, Smart Truncation & nhắc giao thức trung tính
│   ├── routes_admin.py       # /health, /dashboard, /v1/control/*, /v1/lanes/*, /v1/gateway/routes
│   ├── security.py           # Rail-guard ổ D & lọc lệnh PowerShell nguy hiểm
│   ├── session_pool.py       # Sticky SessionSlot pool & lưu/khôi phục .bridge_session*.json
│   ├── status_view.py        # Dựng payload trạng thái chẩn đoán cho /v1/status & /health
│   ├── streaming.py          # ToolCallAwareStreamer ẩn thẻ <tool_call> khỏi luồng chữ SSE
│   ├── title_generation.py   # Sinh tiêu đề hội thoại DSH cục bộ (< 5ms)
│   ├── tool_parser.py        # Parser batch fail-closed <tool_call> + json-repair + Auto-Mapper
│   └── vision.py             # Pipeline Base64 -> file ảnh tạm gửi thẳng Gemini Web
├── frontend/                 # Web Dashboard W3C Custom Elements
├── scripts/
│   └── live_smoke.py         # Kịch bản kiểm tra nhanh với server đang chạy
├── tests/                    # Bộ unit & integration test (201 tests)
├── main.py                   # Entrypoint FastAPI mỏng (~206 dòng)
├── run_all.bat               # Khởi động Gateway (:10000) + acc1 (:10001) + acc2 (:10002)
├── run_acc1.bat / run_acc2.bat / run_frontend.bat / stop_all.bat
├── .env.example              # File cấu hình mẫu
├── requirements.txt          # Thư viện Python phụ thuộc
├── ARCHITECTURE.md / ARCHITECTURE_VI.md
└── README.md / README_VI.md
```

---

## Hướng Dẫn Cài Đặt & Khởi Chạy

### 1. Cài đặt thư viện
Yêu cầu Python `>= 3.10`. Tại thư mục `dsh_gemini_bridge/`:

```bash
pip install -r requirements.txt
```

*(Tùy chọn: cài thêm `pillow` nếu muốn tự động nén/resize ảnh Base64 cỡ lớn trước khi gửi)*:
```bash
pip install pillow
```

### 2. Cấu hình môi trường (`.env`, `.env.acc1`, `.env.acc2`)
Copy `.env.example` ra `.env` (và `.env.acc1`, `.env.acc2` khi chạy 2 luồng qua Gateway), lấy cookie từ trình duyệt (`F12` → **Application** → **Cookies** → `https://gemini.google.com`) rồi điền vào:

```ini
SECURE_1PSID=g.a000...
SECURE_1PSIDTS=sidts-...
DEFAULT_PROJECT_DIR=D:\ABC
```

*(Trên Windows, cấp quyền ghi thư mục làm việc ổ D một lần: `icacls "D:\ABC" /grant "$($env:USERNAME):(OI)(CI)F" /T`.)*

### 3. Khởi động Gateway + 2 luồng worker (`run_all.bat`)
- Bấm đúp **`run_all.bat`** để bật đồng bộ:
  - **Cổng Gateway & Dashboard:** `http://127.0.0.1:10000`
  - **Worker `acc1`:** `http://127.0.0.1:10001`
  - **Worker `acc2`:** `http://127.0.0.1:10002`
- Tắt toàn bộ tiến trình qua **`stop_all.bat`** (hoặc dùng nút **BẬT/TẮT** trên Dashboard để drain an toàn request đang chạy).

---

## Tích Hợp Vào DeepSeek Harness (`pi-ai`)

Chỉ cần trỏ **Base URL** về cổng **Gateway `:10000`** để DSH tự chia tải song song cả Lead và các Subagent qua `acc1` + `acc2`:

| Mục cấu hình | Giá trị |
|---|---|
| **API Protocol** | `OpenAI Chat Completions` |
| **Base URL** | `http://127.0.0.1:10000/v1` |
| **API Key** | Chuỗi bất kỳ (hoặc khớp `API_KEY` trong `.env` nếu có đặt) |
| **Model ID** | ID chính xác từ `GET /v1/models` (vd `gemini-4-neon`, `gemini-4-hydrogen`) |

> Khi cần test riêng từng tài khoản vẫn có thể gọi thẳng `http://127.0.0.1:10001/v1` (`acc1`) hoặc `http://127.0.0.1:10002/v1` (`acc2`). Không dùng chung cookie giữa `.env.acc1` và `.env.acc2` (kiểm tra `account_key` khác nhau trên `/v1/status`).

---

## Các Endpoint Quản Trị Hỗ Trợ

| Phương thức | Đường dẫn | Mô tả |
|---|---|---|
| **POST** | `/v1/chat/completions`, `/v1/responses` | Xử lý hội thoại & tool calling (Gateway tự điều phối khi chạy `:10000`, worker xử lý trực tiếp). |
| **GET** | `/v1/models` | Danh sách model thực tế trên tài khoản (Gateway tổng hợp từ các worker đang chạy). |
| **GET** | `/v1/gateway/routes` | Xem trạng thái Gateway: `active`, `inflight`, `cooldowns` và bảng `sticky` theo session. |
| **GET** | `/health`, `/v1/status` | Thông số uptime, tổng request, lỗi, trạng thái cookie và slot tài khoản. |
| **GET** | `/dashboard` | Giao diện Web Dashboard quản trị. |
| **GET/POST** | `/v1/control`, `/v1/control/enable`, `/v1/control/disable` | Xem/bật/tắt kết nối Gemini Web có drain an toàn. |
| **POST** | `/v1/update-cookies`, `/v1/accounts/{slot}/cookies` | Nạp nóng cookie chính/dự phòng không cần khởi động lại. |
| **POST** | `/v1/switch-model`, `/v1/reset`, `/v1/accounts/clear-exhausted` | Đổi model, làm mới phiên chat hoặc xóa thời gian nghỉ cooldown tài khoản. |
| **GET** | `/v1/logs`, `/v1/activity` | Đọc log gần nhất và nhật ký JSONL (`model`, `fallback_from`, `tools`, `latency_ms`). |
| **GET/POST** | `/v1/lanes`, `/v1/lanes/{lane_id}/*` | Giám sát và điều khiển từng luồng worker thông qua `X-Lane-Token`. |
| **POST** | `/v1/cache/clear` | Endpoint tương thích ngược (`{"cleared_entries": 0}`). |

---

## Kiểm Thử & Vận Hành

Chạy toàn bộ 201 unit & integration test:

```powershell
python -B -m unittest discover -s tests
```

Chạy smoke test nhanh với bridge đang bật (`:10000` hoặc `:10001`):

```powershell
python scripts/live_smoke.py --base-url http://127.0.0.1:10000
```

---

## Xử Lý Sự Cố Thường Gặp

1. **Lỗi HTTP 503 `bridge_disabled` hoặc `no_healthy_lane`:**
   Mở `http://127.0.0.1:10000/dashboard`, đảm bảo `acc1` và `acc2` đang ở trạng thái **BẬT** (`auth_ok=true`), hoặc đặt `API_START_STATE=on` trong `.env*`.
2. **Cookie hết hạn hoặc rơi vào Guest Mode:**
   Lấy lại `__Secure-1PSID` và `__Secure-1PSIDTS` từ `gemini.google.com` rồi dán vào **Quản Lý Cookie (Hot-Reload)** trên Dashboard (hoặc `POST /v1/update-cookies`). Đặt `ALLOW_GUEST_MODE=false` nếu muốn chặn hẳn Guest Mode thay vì cho chạy dự phòng.
3. **Sửa code hoặc đổi cấu hình nhưng server vẫn chạy bản cũ:**
   Nút Tắt/Bật trên Dashboard chỉ đóng/mở `GeminiClient` trong tiến trình Python đang nằm trên RAM. Sau khi sửa code, chạy `stop_all.bat` → xóa `.bridge_session*.json` nếu cần → mở lại `run_all.bat` rồi tạo **New Session** mới trên DSH.
4. **Tool call báo lỗi cú pháp / thiếu thẻ đóng:**
   Parser fail-closed trong [app/tool_parser.py](app/tool_parser.py) chặn thực thi nửa vời cả batch và ghi raw reply vào `logs/failure_traces*.jsonl` để kiểm tra nguyên nhân.
