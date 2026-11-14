# DSH Gemini Agent Bridge (v3.0 - Bản Doanh Nghiệp Chuẩn Modular)

> 🌐 **Ngôn ngữ:** [English](README.md) | **Tiếng Việt**

> **Cổng Proxy Tương Thích Chuẩn OpenAI & Bộ Điều Phối Coding Agent cho Gemini Web (`gemini_webapi`) trên DeepSeek Harness (`pi-ai`), Cline, và Roo Code.**

Dự án này cung cấp một lớp trung gian (Middleware Bridge) biến giao diện **Google Gemini Web** thành một Backend chuẩn **OpenAI Chat Completions API** có đầy đủ khả năng **Function Calling (Tool Use)**, **Multimodal Vision (Đọc hiểu hình ảnh 3 tầng)**, **Stateful Single-Session**, cơ chế **Failover tự động**, và giao diện **Web Dashboard giám sát trực quan**.

---

## Điểm Nhấn Kỹ Thuật (v3.0)

1. **Zero-Spam Single ChatSession:** Gom toàn bộ chuỗi hành động của Agent (Read → Glob → Edit → Pwsh) vào **đúng 1 đoạn chat duy nhất** trên Gemini Web thông qua cơ chế truyền tải Delta gia tăng. Giảm 90% dung lượng prompt và bảo vệ tài khoản khỏi bộ lọc chống spam của Google.
2. **Anti-Timeout SSE Heartbeat:** Triển khai cơ chế phát xung `: keep-alive\n\n` định kỳ mỗi 2.5 giây trên luồng Server-Sent Events, triệt tiêu hoàn toàn lỗi client idle timeout và vòng lặp treo `[Recovery]`.
3. **Cookie Failover Pool & Hot-Reload:** Tự động phát hiện khi cookie chính bị hết hạn và chuyển đổi tức thì sang tài khoản Google dự phòng (`BACKUP_1PSID`), hoặc nạp cookie mới qua API `POST /v1/update-cookies` mà không cần tắt server.
4. **Hàng Đợi Có Giới Hạn Thời Gian (Lock Timeout):** Cơ chế `asyncio.Lock` với giới hạn chờ 30 giây, trả về thông báo bận (HTTP 429) ngay lập tức thay vì để tác vụ treo vô hạn khi nhiều luồng cùng gọi.
5. **Bộ Nhớ Đệm Phản Hồi (Prompt Cache):** Tự động lưu cache các phản hồi lặp lại (TTL 5 phút) theo mã băm SHA-256 của prompt, giảm thiểu độ trễ xuống dưới 1ms cho các truy vấn trùng lặp.
6. **Smart Truncation (Rút ngắn ngữ cảnh thông minh):** Ưu tiên bảo vệ và giữ nguyên các khối thông báo lỗi, stack trace quan trọng khi kết quả chạy lệnh vượt quá giới hạn ký tự.
7. **Tầng Thị Giác Đa Tầng (3-Tier Vision Pipeline):** Tự động nén ảnh Base64 (Pillow JPEG), thử nghiệm gửi trực tiếp Native Gemini Web, và tự động chuyển tiếp qua bộ ba OCR (`RapidOCR` → `Windows 11 Native WinRT OCR` → `Tesseract OCR`) nếu cổng upload gặp sự cố.
8. **Tường Lửa Ổ C Cứng Rắn (Strict Path & Shell Firewall):** Triệt tiêu hoàn toàn rủi ro vượt ngục đường dẫn (`os.path.realpath` chặn `..\..\..\Windows`) và lọc sạch các biến môi trường, kỹ thuật escape PowerShell can thiệp vào ổ hệ thống (`C:\`).
9. **Dashboard Theo Chuẩn Web Components:** Giao diện điều khiển module hóa xây dựng bằng **W3C Custom Elements** và **ES Modules** (không cần Node.js, không cần build), hỗ trợ đầy đủ Dark/Light theme và chuyển ngữ EN/VI.

---

## Cấu Trúc Dự Án

```
dsh_gemini_bridge/
├── app/                          # Backend Core Modules
│   ├── __init__.py               # Khởi tạo gói core v3.0
│   ├── config.py                 # Cấu hình tập trung, kiểm tra .env khi khởi động & File Logger
│   ├── security.py               # Tường lửa ổ C:\ (Path Jail & PowerShell Filter)
│   ├── vision.py                 # Pipeline xử lý ảnh Base64, nén ảnh & 3-Engine OCR Fallback
│   ├── prompt_builder.py         # Nạp tool schema, Smart Truncation & Language Matching
│   ├── tool_parser.py            # Bóc tách thẻ XML <tool_call> (Hỗ trợ Parallel) & Auto-Mapper
│   └── gemini_session.py         # Quản lý Stateful Session, Lock Timeout, Failover & Cache
├── frontend/                     # 🧩 Frontend Modular (W3C Web Components)
│   ├── index.html                # Skeleton ngữ nghĩa (chỉ ~30 dòng gọn gàng)
│   ├── css/
│   │   ├── variables.css         # Design Tokens, Colors, Dark/Light Themes
│   │   ├── terminal.css          # CSS riêng cho Log Console & Syntax
│   │   └── main.css              # Main Stylesheet tổng hợp & Layout
│   └── js/
│       ├── api.js                # API Client Service Layer
│       ├── state.js              # Reactive State Store & Event Bus
│       ├── i18n.js               # Từ điển đa ngôn ngữ (EN / VI)
│       ├── app.js                # Bootstrap Entrypoint
│       └── components/           # Các Custom Elements độc lập
│           ├── BridgeNavbar.js        # <bridge-navbar> (Logo, ping, EN/VI & Dark/Light toggle)
│           ├── BridgeMetrics.js       # <bridge-metrics> (4 Telemetry Cards)
│           ├── BridgeTerminal.js      # <bridge-terminal> (Console lọc nhật ký & Search)
│           ├── BridgeModelSwitch.js   # <bridge-model-switch> (Chuyển đổi model Gemini)
│           ├── BridgeCookieManager.js # <bridge-cookie-manager> (Nạp nóng cookie & ẩn mật khẩu)
│           ├── BridgeQuickActions.js  # <bridge-quick-actions> (Reset session & Xóa cache)
│           └── BridgeToast.js         # <bridge-toast> (Hệ thống thông báo nổi)
├── logs/                         # Thư mục lưu log tự động (xoay vòng 5MB x 3 bản)
├── main.py                       # FastAPI Controller, Middleware Auth & Static Mounting
├── .env.example                  # Tệp cấu hình mẫu
├── requirements.txt              # Danh sách thư viện phụ thuộc
├── ARCHITECTURE.md               # Tài liệu thiết kế kiến trúc chi tiết (English)
├── ARCHITECTURE_VI.md            # Tài liệu thiết kế kiến trúc chi tiết (Tiếng Việt)
├── README.md                     # Tài liệu hướng dẫn triển khai & vận hành (English)
└── README_VI.md                  # Tài liệu hướng dẫn triển khai & vận hành (Tiếng Việt)
```

---

## Hướng Dẫn Cài Đặt & Khởi Chạy

### 1. Cài đặt thư viện phụ thuộc
Yêu cầu Python `>= 3.10`. Mở Terminal tại thư mục `dsh_gemini_bridge/` và chạy:

```bash
pip install -r requirements.txt
```

*(Tùy chọn nâng cao cho nhận diện ảnh và nén ảnh)*:
```bash
pip install rapidocr-onnxruntime pillow
```

### 2. Thiết lập cấu hình biến môi trường (`.env`)
Sao chép tệp cấu hình mẫu `.env.example` thành `.env`:

```powershell
Copy-Item .env.example .env
```

Mở tệp `.env` và điền thông tin cookie từ trình duyệt đang đăng nhập tài khoản Google Gemini (F12 → **Application** → **Cookies** → `https://gemini.google.com`):

```ini
# [BẮT BUỘC] Cookie tài khoản chính
SECURE_1PSID=g.a000...
SECURE_1PSIDTS=sidts-...

# [TÙY CHỌN] Cookie tài khoản dự phòng (tự động chuyển đổi nếu tài khoản chính lỗi)
BACKUP_1PSID=
BACKUP_1PSIDTS=

# [TÙY CHỌN] Mã khóa bảo vệ API (để trống nếu chỉ dùng trong mạng nội bộ)
API_KEY=

# Thư mục làm việc an toàn
DEFAULT_PROJECT_DIR=D:\ABC
```

### 3. Cấp quyền truy cập cho thư mục làm việc trên ổ D
Để công cụ `Pwsh` của Agent có thể thao tác (tạo thư mục, ghi file, chạy node/python) mà không cần chạy DeepSeek Harness ở quyền Administrator (nhằm bảo vệ ổ C:\), hãy mở PowerShell và chạy lệnh sau một lần:

```powershell
icacls "D:\ABC" /grant "$($env:USERNAME):(OI)(CI)F" /T
```

### 4. Khởi động Bridge Server
Tại thư mục `dsh_gemini_bridge/`, khởi chạy:

```bash
python main.py
```

Khi khởi động thành công, Terminal sẽ hiển thị:

```
2026-11-14 12:00:00 | INFO    | [DSH-Bridge] Đang khởi tạo Gemini WebClient...
2026-11-14 12:00:02 | INFO    | [DSH-Bridge] Model khả dụng: ['unspecified', 'gemini-flash']
2026-11-14 12:00:02 | INFO    | [DSH-Bridge] SingleChatManager sẵn sàng! Model: [gemini-flash]
INFO:     Uvicorn running on http://127.0.0.1:10000 (Press CTRL+C to quit)
```

Mở trình duyệt truy cập Dashboard:
👉 **`http://127.0.0.1:10000/dashboard`**

---

## Tích Hợp Vào DeepSeek Harness (pi-ai)

Vào giao diện **Settings → Model Providers** trên ứng dụng DeepSeek Harness:

| Mục cấu hình     | Giá trị điền                                                    |
|------------------|-----------------------------------------------------------------|
| **API Protocol** | `OpenAI Chat Completions`                                       |
| **Base URL**     | `http://127.0.0.1:10000/v1`                                     |
| **API Key**      | Nhập chuỗi bất kỳ (hoặc khớp với biến `API_KEY` trong `.env`)   |
| **Model ID**     | `gemini-flash`                                                  |

---

## Danh Sách Các Endpoint Quản Trị Hỗ Trợ

| Phương thức | Đường dẫn               | Mô tả chức năng                                                                            |
|------------|-------------------------|---------------------------------------------------------------------------------------------|
| **POST**   | `/v1/chat/completions`  | Endpoint chính xử lý hội thoại, hình ảnh và điều phối Tool Calling.                        |
| **GET**    | `/v1/models`            | Trả về danh sách model thực tế được phát hiện trên tài khoản Google.                       |
| **GET**    | `/health`               | Báo cáo chi tiết: Uptime, tổng số request, tỷ lệ lỗi, trạng thái cookie, dung lượng cache.  |
| **GET**    | `/dashboard`            | Giao diện Web Dashboard điều khiển trực quan.                                              |
| **POST**   | `/v1/update-cookies`    | Cập nhật `SECURE_1PSID` và `SECURE_1PSIDTS` mới ngay khi server đang chạy (không cần restart).|
| **POST**   | `/v1/switch-model`      | Chuyển đổi linh hoạt giữa các model Gemini khả dụng (`gemini-flash`, v.v.).                |
| **POST**   | `/v1/reset`             | Đóng đoạn chat hiện tại trên Gemini Web và tạo một đoạn chat mới tinh.                      |
| **POST**   | `/v1/cache/clear`       | Xóa sạch các câu trả lời lưu trong bộ nhớ đệm cache.                                       |
| **GET**    | `/v1/logs`              | Trích xuất 50 dòng log gần nhất từ `logs/bridge.log`.                                       |

---

## Xử Lý Sự Cố Thường Gặp (Troubleshooting)

1. **Lỗi `[CONFIG ERROR] SECURE_1PSID is empty!` khi khởi động:**
   * Bạn chưa tạo tệp `.env` hoặc chưa điền cookie. Hãy sao chép từ `.env.example` sang `.env` và điền giá trị.
2. **Cảnh báo `COOKIE CÓ THỂ HẾT HẠN!` trong Logs:**
   * Cookie phiên làm việc trên trình duyệt của bạn đã hết hạn. Mở lại trình duyệt Google Gemini, lấy lại 2 chuỗi cookie mới và gửi qua giao diện Dashboard hoặc lệnh:
     ```bash
     curl -X POST http://127.0.0.1:10000/v1/update-cookies -H "Content-Type: application/json" -d "{\"SECURE_1PSID\":\"...\",\"SECURE_1PSIDTS\":\"...\"}"
     ```
3. **Agent báo lỗi `missing required property` khi gọi Tool:**
   * Cơ chế `Auto-Mapper` trong `app/tool_parser.py` sẽ tự động bổ sung và chuẩn hóa tên tham số (`path` → `file_path`, `cmd` → `command`). Nếu có custom tool mới, bạn có thể bổ sung quy tắc ánh xạ vào hàm `normalize_tool_arguments()`.
