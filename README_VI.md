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

### 4. Khởi động nhanh cả hai luồng (`run_all.bat`)
Chủ dự án có thể khởi động cả hai luồng `acc1` (cổng 10000) và `acc2` (cổng 10001) chỉ với **MỘT thao tác**:
- **(a) Bấm đúp `run_all.bat`:** Script sẽ kiểm tra cổng lắng nghe và tự động khởi động 2 cửa sổ cmd thu nhỏ (`minimized`) cho 2 luồng riêng biệt. Nếu luồng nào đã chạy từ trước, script tự động bỏ qua.
- **(b) Tạo shortcut ra Desktop:** Bấm chuột phải vào `run_all.bat` → *Send to* → *Desktop (create shortcut)* để mở 2 luồng nhanh chóng từ màn hình chính.
- **(c) Tự chạy khi khởi động máy:** Nhấn tổ hợp phím `Win + R` → gõ `shell:startup` → Enter → kéo shortcut của `run_all.bat` vào thư mục Startup này. Trong thuộc tính của Shortcut (Properties), chọn *Run: Minimized* để chạy ngầm gọn gàng.
- **Dừng tiến trình:** Khi cần tắt hoàn toàn cả 2 tiến trình ngay lập tức, chạy `stop_all.bat`. Lưu ý: trong vận hành thông thường, hãy sử dụng công tắc **BẬT/TẮT** trên Dashboard để chờ request đang xử lý hoàn tất (drain an toàn).

### 5. Khởi động thủ công từng luồng
Tại thư mục `dsh_gemini_bridge/`, bạn vẫn có thể khởi chạy riêng lẻ từng luồng:
```bash
# Chạy luồng mặc định
python main.py

# Hoặc chạy riêng lẻ qua file .bat:
run_acc1.bat    # Luồng acc1 cổng 10000
run_acc2.bat    # Luồng acc2 cổng 10001
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

## Chạy 2 Agent Song Song (Multi-Instance Isolation)

Bridge hỗ trợ chạy tối đa 2 Agent DSH độc lập cùng lúc từ **cùng một thư mục mã nguồn**, mỗi con dùng một tài khoản Google riêng biệt và một cổng riêng biệt mà không bao giờ xung đột hay lẫn lộn dữ liệu:

### Bảng Định Danh Instance

| ID | Tài khoản | Cổng | Agent DSH | File cấu hình | File session | Log chạy | Nhật ký công việc |
|---|---|---|---|---|---|---|---|
| `acc1` | Google A | 10000 | DSH 1 (dự án 1) | `.env.acc1` | `.bridge_session_acc1.json` | `logs/bridge_acc1.log` | `logs/activity_acc1.jsonl` |
| `acc2` | Google B | 10001 | DSH 2 (dự án 2) | `.env.acc2` | `.bridge_session_acc2.json` | `logs/bridge_acc2.log` | `logs/activity_acc2.jsonl` |
| *(default)* | - | 10000 | Đơn lẻ (cũ) | `.env` | `.bridge_session.json` | `logs/bridge.log` | `logs/activity.jsonl` |

### Hướng Dẫn Cấu Hình & Chạy

1. **Tạo 2 file môi trường:**
   - Copy `.env.example` thành `.env.acc1`:
     ```ini
     PORT=10000
     INSTANCE_LABEL="Frontend"
     PEER_LANES="acc2=http://127.0.0.1:10001"
     LANE_ADMIN_TOKEN="<chuỗi ngẫu nhiên dài dùng chung cho cả 2 luồng>"
     ```
   - Copy `.env.example` thành `.env.acc2`:
     ```ini
     PORT=10001
     INSTANCE_LABEL="Backend"
     PEER_LANES="acc1=http://127.0.0.1:10000"
     LANE_ADMIN_TOKEN="<cùng chuỗi token như ở .env.acc1>"
     ```
   - *Tạo token an toàn:* `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - *Lưu ý bảo mật:* `LANE_ADMIN_TOKEN` là bí mật xác thực giữa 2 luồng, không commit ra ngoài (tất cả file `.env.*` đã nằm trong `.gitignore`).
2. **Khởi chạy:**
   - Nhấp đúp `run_acc1.bat` để chạy con `acc1` (cổng 10000).
   - Nhấp đúp `run_acc2.bat` để chạy con `acc2` (cổng 10001).
3. **Cấu hình DeepSeek Harness:**
   - DSH 1: Base URL `http://127.0.0.1:10000/v1`
   - DSH 2: Base URL `http://127.0.0.1:10001/v1`
4. **Theo dõi công việc riêng biệt:**
   - Nhật ký hoạt động: `http://127.0.0.1:10000/v1/activity` và `http://127.0.0.1:10001/v1/activity`
   - Dashboard: `http://127.0.0.1:10000/dashboard` và `http://127.0.0.1:10001/dashboard`
5. **CẢNH BÁO QUAN TRỌNG VỀ TÀI KHOẢN:**
   - TUYỆT ĐỐI KHÔNG dùng chung cookie giữa 2 instance (nếu dùng chung sẽ cùng vào 1 tài khoản).
   - `BACKUP_1PSID` của instance này không được trùng với tài khoản của instance kia.
   - Để lấy cookie: hãy đăng nhập mỗi tài khoản Google trong 1 Profile Chrome (hoặc cửa sổ ẩn danh Incognito) riêng biệt, copy cookie rồi đóng cửa sổ để tránh xoay cookie ngầm.
   - Kiểm tra hai con có bị trùng tài khoản không bằng cách đối chiếu mã `account_key` tại `http://127.0.0.1:10000/health` và `http://127.0.0.1:10001/health` (2 mã hash 12 ký tự này bắt buộc phải khác nhau).

---

## Danh Sách Các Endpoint Quản Trị Hỗ Trợ

| Phương thức | Đường dẫn               | Mô tả chức năng                                                                            |
|------------|-------------------------|---------------------------------------------------------------------------------------------|
| **POST**   | `/v1/chat/completions`  | Endpoint chính xử lý hội thoại, hình ảnh và điều phối Tool Calling.                        |
| **GET**    | `/v1/models`            | Trả về danh sách model thực tế được phát hiện trên tài khoản Google.                       |
| **GET**    | `/health`               | Báo cáo chi tiết: Uptime, tổng số request, tỷ lệ lỗi, trạng thái cookie, dung lượng cache.  |
| **GET**    | `/dashboard`            | Giao diện Web Dashboard điều khiển trực quan.                                              |
| **GET**    | `/v1/control`           | Lấy trạng thái công tắc API (`off`, `starting`, `on`, `stopping`, `drain_timeout`).        |
| **POST**   | `/v1/control/enable`    | Bật kết nối Gemini Web & mở nhận request cho Agent.                                       |
| **POST**   | `/v1/control/disable`   | Tắt kết nối Gemini (chờ request đang chạy xong rồi mới đóng client).                      |
| **POST**   | `/v1/update-cookies`    | Cập nhật `SECURE_1PSID` và `SECURE_1PSIDTS` mới ngay khi server đang chạy (không cần restart).|
| **POST**   | `/v1/switch-model`      | Chuyển đổi linh hoạt giữa các model Gemini khả dụng (`gemini-flash`, v.v.).                |
| **POST**   | `/v1/reset`             | Đóng đoạn chat hiện tại trên Gemini Web và tạo một đoạn chat mới tinh.                      |
| **POST**   | `/v1/cache/clear`       | Xóa sạch các câu trả lời lưu trong bộ nhớ đệm cache.                                       |
| **GET**    | `/v1/logs`              | Trích xuất 50 dòng log gần nhất từ file log của instance.                                  |
| **GET**    | `/v1/activity`          | Nhật ký công việc chi tiết của chính instance (request, tool calls, model, latency).       |

---

## Công Tắc BẬT/TẮT API Trên Dashboard (API Control Switch)

Bridge tích hợp cơ chế kiểm soát kết nối chủ động:
- **Khởi động ở trạng thái TẮT (mặc định):** Server web và dashboard vẫn hoạt động, nhưng **KHÔNG** kết nối Gemini Web và **KHÔNG** chạy vòng tự làm mới token ngầm (chống hao mòn / quét cookie khi không dùng).
- **Mỗi Instance một công tắc riêng:** `acc1` trên `:10000/dashboard` và `acc2` trên `:10001/dashboard` có công tắc hoàn toàn độc lập. Trạng thái không lưu ra đĩa (mỗi lần khởi động luôn theo biến `API_START_STATE`).

### Bảng Hành Vi Trạng Thái

| Trạng thái | Kết nối Gemini | /v1/chat/completions | Dashboard, /health, Công tắc |
|---|---|---|---|
| `off` | KHÔNG init | HTTP 503 (`code: "bridge_disabled"`) | Sử dụng bình thường |
| `starting` | Đang init | HTTP 503 (`code: "bridge_disabled"`, đang khởi động) | Sử dụng bình thường |
| `on` | Đã init | Nhận và xử lý bình thường | Sử dụng bình thường |
| `stopping` | Đang chờ drain / ngắt | HTTP 503 (`code: "bridge_disabled"`, đang tắt) | Sử dụng bình thường |

### Cấu Hình Liên Quan Trong `.env`

```ini
# Trạng thái lúc khởi động: off (mặc định) = chờ bấm BẬT trên dashboard; on = tự bật lúc chạy
API_START_STATE=off

# Số giây tối đa chờ request đang chạy dở hoàn thành khi bấm TẮT (mặc định: 120 giây)
DISABLE_DRAIN_TIMEOUT=120
```

### Cơ Chế Draining Khi Bấm TẮT
1. Request **MỚI** sẽ lập tức bị từ chối với HTTP 503 `bridge_disabled`.
2. Request **ĐANG CHẠY** được giữ cho chạy tới khi hoàn thành (tối đa `DISABLE_DRAIN_TIMEOUT` giây), sau đó Bridge lưu metadata session và đóng client.
3. Nếu request chạy quá thời hạn quy định, Bridge sẽ **KHÔNG cắt ngang** (trả HTTP 409 `bridge_control_conflict` và giữ nguyên trạng thái `on` để bảo vệ dữ liệu).

### Dùng Từ Máy Thứ Hai (An Toàn Mạng LAN)
Khi cần truy cập Bridge hoặc Dashboard từ một máy tính khác trong cùng mạng nội bộ (LAN):
1. **Thiết lập an toàn bắt buộc:**
   - Trong file `.env` (hoặc `.env.acc1`, `.env.acc2`), đặt `HOST=0.0.0.0` VÀ BẮT BUỘC ĐẶT `API_KEY`:
     ```ini
     HOST=0.0.0.0
     API_KEY=chuỗi_khóa_bí_mật_tự_chọn
     ```
   - *Cơ chế phòng thủ:* Nếu đặt `HOST=0.0.0.0` mà `API_KEY` để trống, Bridge sẽ **TỪ CHỐI KHỞI ĐỘNG** (thoát mã lỗi 1) để bảo vệ tài khoản Google của bạn khỏi truy cập trái phép từ các thiết bị khác trong mạng (trừ khi cố ý đặt `ALLOW_INSECURE_LAN=true`).
2. **Tạo luật Windows Firewall giới hạn địa chỉ IP:**
   Khuyến nghị chỉ cho phép IP cụ thể của máy thứ hai truy cập vào các cổng 10000/10001 (chạy trong PowerShell với quyền Administrator):
   ```powershell
   New-NetFirewallRule -DisplayName "DSH Bridge" -Direction Inbound -LocalPort 10000,10001 -Protocol TCP -RemoteAddress <IP_MÁY_2> -Action Allow
   ```
3. **Lưu ý bảo mật đường truyền:**
   - Kết nối trên mạng LAN sử dụng giao thức HTTP thông thường (chưa mã hóa TLS/HTTPS), do đó API Key có thể bị nghe lén nếu kết nối qua mạng Wi-Fi không tin cậy. Chỉ nên sử dụng tính năng này trên mạng gia đình hoặc văn phòng tin cậy.

---

## Xử Lý Sự Cố Thường Gặp (Troubleshooting)

0. **Agent nhận mã lỗi HTTP 503 `bridge_disabled`:**
   * **Triệu chứng:** Agent thông báo `Bridge đang TẮT. Hãy bật ở dashboard rồi thử lại.`
   * **Cách xử lý:** Mở Dashboard của instance tương ứng (`http://127.0.0.1:10000/dashboard` cho con 1 hoặc `:10001/dashboard` cho con 2) và bấm nút **BẬT**. Sau khi trạng thái chuyển sang màu xanh **BẬT**, gửi lại prompt trên Agent.

1. **Xử lý khi bridge rơi vào Guest mode (Chưa đăng nhập):**
   * **Triệu chứng:** Dashboard hiển thị huy hiệu `GUEST (CHƯA ĐĂNG NHẬP)` màu đỏ, request API từ Agent trả về HTTP 503 với mã lỗi `{"error": {"code": "gemini_not_authenticated", "message": "Gemini chưa đăng nhập..."}}`.
   * **Nguyên nhân:** Cookie `SECURE_1PSID` hoặc `SECURE_1PSIDTS` đã hết hạn hoặc bị Google thu hồi (khi bạn đăng xuất trình duyệt hoặc đổi IP/đăng nhập từ thiết bị khác). Thư viện `gemini-webapi` 2.x có cơ chế tự động fallback về phiên Guest, nhưng Bridge đã chủ động chặn đứng phiên Guest để không làm hỏng dữ liệu chat và không mất tính năng Gemini Pro.
   * **Cách xử lý:**
     1. Mở trình duyệt Chrome → truy cập [https://gemini.google.com](https://gemini.google.com) → đảm bảo đang đăng nhập tài khoản Google của bạn.
     2. Bấm `F12` → tab **Application** (hoặc Lưu trữ) → **Storage** → **Cookies** → `https://gemini.google.com`.
     3. Sao chép giá trị của 2 cookie: `__Secure-1PSID` và `__Secure-1PSIDTS`.
     4. Mở Dashboard Bridge (`http://127.0.0.1:10000/dashboard`) → dán vào thẻ **Quản Lý Cookie (Hot-Reload)** → bấm **Lưu & Cập Nhật**. Hoặc gửi API `POST /v1/update-cookies`. Cookie sẽ được nạp ngay mà không cần khởi động lại server.

2. **Lỗi `[CONFIG ERROR] SECURE_1PSID is empty!` khi khởi động:**
   * Bạn chưa tạo tệp `.env` hoặc chưa điền cookie. Hãy sao chép từ `.env.example` sang `.env` và điền giá trị.
3. **Cảnh báo `COOKIE CÓ THỂ HẾT HẠN!` trong Logs:**
   * Cookie phiên làm việc trên trình duyệt của bạn đã hết hạn. Mở lại trình duyệt Google Gemini, lấy lại 2 chuỗi cookie mới và gửi qua giao diện Dashboard hoặc lệnh:
     ```bash
     curl -X POST http://127.0.0.1:10000/v1/update-cookies -H "Content-Type: application/json" -d "{\"SECURE_1PSID\":\"...\",\"SECURE_1PSIDTS\":\"...\"}"
     ```
4. **Agent báo lỗi `missing required property` khi gọi Tool:**
   * Cơ chế `Auto-Mapper` trong `app/tool_parser.py` sẽ tự động bổ sung và chuẩn hóa tên tham số (`path` → `file_path`, `cmd` → `command`). Nếu có custom tool mới, bạn có thể bổ sung quy tắc ánh xạ vào hàm `normalize_tool_arguments()`.
