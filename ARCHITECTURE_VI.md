# Kiến Trúc Hệ Thống & Luồng Hoạt Động (System Architecture v3.0)

> 🌐 **Ngôn ngữ:** [English](ARCHITECTURE.md) | **Tiếng Việt**

Tài liệu này mô tả chi tiết thiết kế kỹ thuật, luồng dữ liệu (Data Pipeline), cơ chế phục hồi lỗi (Fault Tolerance) và các tầng bảo vệ đa lớp của **DSH Gemini Agent Bridge v3.0**.

---

## 1. Sơ Đồ Luồng Dữ Liệu Tổng Thể (End-to-End Request Lifecycle)

```
[DeepSeek Harness (pi-ai) / Coding Agent Client]
        │
        │ 1. POST /v1/chat/completions (OpenAI Payload: messages + tools + base64 images)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ main.py (FastAPI Controller & SSE Multiplexer)                          │
│  ├─ [Tùy chọn] API Key Authentication Middleware                        │
│  ├─ Rút trích Delta Prompt & dọn dẹp Payload khỏi RAM ngay tức thì      │
│  ├─ Kiểm tra Response Cache SHA-256 (TTL 5 phút, trả kết quả trong 1ms) │
│  ├─ Mở luồng SSE Streaming ngay lập tức (Gửi First Chunk trong 1ms)     │
│  └─ Bắn nhịp tim ": keep-alive\n\n" mỗi 2.5s chống idle timeout         │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ app/gemini_session.py (SingleChatManager - Stateful Session Engine)     │
│  ├─ asyncio.Lock(timeout=30s): Ngăn chặn Race Condition & quá tải      │
│  ├─ Rate-Limit Pacing: Giãn cách an toàn 0.5s - 2.0s chống rate-limit   │
│  ├─ Cookie Failover Pool: Tự động đổi sang tài khoản dự phòng khi lỗi   │
│  └─ Duy trì ĐÚNG 1 ĐOẠN CHAT DUY NHẤT trên giao diện Gemini Web         │
└──────────────┬───────────────────────────────────────┬──────────────────┘
               │                                       ▲
               ▼ 2. Xử lý Prompt & Vision              │ 6. Raw Response
┌──────────────────────────────┐             ┌─────────┴──────────────────┐
│ app/prompt_builder.py        │             │ Gemini Web (Google Server) │
│  + app/vision.py             │────────────►│  - Xử lý trên 1 Session    │
│  ├─ Smart Truncation: Giữ    │ 3. Gửi kèm  │  - Hỗ trợ ảnh nén upload   │
│     lại dòng lỗi/stacktrace  │    files=[] │  - auto_refresh=True       │
│  ├─ Tầng 1: Native Vision    │             └────────────────────────────┘
│  ├─ Tầng 2: OCR 3-Engine Fallback (RapidOCR -> WinRT -> Tesseract)      │
│  └─ Sandwich Prompting: Bắt buộc khớp ngôn ngữ & Hỏi trước khi làm      │
└──────────────────────────────┘
               │
               ▼ 7. Bóc tách & Kiểm duyệt bảo mật
┌─────────────────────────────────────────────────────────────────────────┐
│ app/tool_parser.py + app/security.py (Auto-Mapper & Hardened Sandbox)   │
│  ├─ Quét tuyến tính O(N) bóc tách <tool_call> (Hỗ trợ Parallel Tools)  │
│  ├─ Auto-Mapper: Chuẩn hóa tham số (path->file_path, cmd->command, ...) │
│  ├─ Path Jail: Chặn triệt để Path Traversal (realpath) trỏ vào C:\      │
│  └─ Shell Firewall: Hủy lệnh Pwsh chứa $env, backticks, IEX, C:\        │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │
                                   │ 8. Trả về chuẩn OpenAI tool_calls hoặc content
                                   ▼
                    [DeepSeek Harness thực thi công cụ cục bộ trên ổ D:]
```

---

## 2. Phân Tích Chuyên Sâu Các Cơ Chế Cốt Lõi

### 2.1. Quản Lý Phiên Đơn Nhất & Cơ Chế Dự Phòng (Failover Cookie Pool)
* **Vấn đề:** Gemini Web dựa vào cookies trình duyệt (`SECURE_1PSID`, `SECURE_1PSIDTS`). Sau một thời gian hoạt động hoặc khi tải cao, cookie có thể hết hạn hoặc bị Google tạm khóa session, khiến toàn bộ agent bị gián đoạn. Thư viện `gemini-webapi` có cơ chế tự động rơi vào chế độ Guest không thông báo lỗi, làm mất model Pro và chức năng upload file.
* **Giải pháp v3.0:**
  * **Phát hiện Guest mode & Kiểm soát xác thực:** Sau mỗi lần `init()`, hệ thống kiểm tra `client.account_status == AccountStatus.AVAILABLE` và `client._cookie_source != "Guest"`. Nếu rơi vào Guest mode, hệ thống lập tức từ chối và chặn request (HTTP 503), bảo vệ tính toàn vẹn của cuộc trò chuyện.
  * **Gắn kết phiên với tài khoản (`account_key`):** Tệp lưu trữ `.bridge_session.json` lưu kèm mã băm SHA-256 rút gọn (`account_key`) của cookie. Khi khởi động lại hoặc đổi tài khoản, hệ thống chỉ khôi phục nếu đúng tài khoản đó, ngăn chặn việc chat nhầm phiên giữa các tài khoản khác nhau.
  * **Hàng đợi có kiểm soát thời gian (`Lock with Timeout`):** Sử dụng `asyncio.wait_for(self.lock.acquire(), timeout=30.0)`. Nếu server đang xử lý tác vụ khác quá lâu, hệ thống sẽ trả thông báo bận (HTTP 429) thay vì để request treo vô tận.
  * **Tự động chuyển đổi tài khoản (Failover):** Khi ghi nhận lỗi xác thực liên tiếp, hệ thống tự động ngắt kết nối client hiện tại và khởi tạo client mới với bộ cookie dự phòng (`BACKUP_1PSID`, `BACKUP_1PSIDTS`).
  * **Hot-Reload không cần khởi động lại:** Endpoint `POST /v1/update-cookies` cho phép cập nhật cookie mới trực tiếp ngay trong lúc server đang chạy qua giao diện Web Dashboard hoặc API.
  * **Kéo dài phiên tự động:** Kích hoạt `auto_refresh=True` trong `GeminiClient.init()` để duy trì session sống lâu trong suốt ngày làm việc.

### 2.2. Bộ Nhớ Đệm Phản Hồi (Response Cache)
* Tích hợp bộ nhớ cache cục bộ dựa trên mã băm SHA-256 của prompt với thời gian sống (TTL) 5 phút.
* **Mặc định TẮT (`ENABLE_RESPONSE_CACHE=false`):** Do đặc thù coding agent cần phản hồi mới nhất theo từng thay đổi mã nguồn, tính năng cache chỉ được bật khi người dùng chủ động cấu hình trong `.env` cho tác vụ hỏi đáp tĩnh.

### 2.3. Tầng Xử Lý Hình Ảnh Đa Phương Thức (Multimodal 3-Tier Fallback)
Khi người dùng đính kèm ảnh màn hình (Base64) trong DeepSeek Harness:
1. **Nén ảnh tự động & Xử lý kênh alpha:** Ảnh được tối ưu kích thước (tối đa 1600x1600, chất lượng JPEG 85%) bằng Pillow. Các ảnh có kênh trong suốt (RGBA, LA, PNG trong suốt) được ghép nền trắng trước khi nén JPEG để tránh mất chi tiết chữ. Giới hạn `Image.MAX_IMAGE_PIXELS = 50_000_000` để chống decompression bomb.
2. **Tầng 1 (Native Vision):** Gửi trực tiếp tệp ảnh vào tham số `files=[...]` của Gemini Web API. Nếu gặp sự cố tạm thời, tạm ngưng cổng upload 5 phút (`vision_disabled_until`) trước khi thử lại, không tắt vĩnh viễn.
3. **Tầng 2 (3-Engine OCR Fallback):** Nếu cổng upload ảnh của Google từ chối hoặc cookie phụ bị hạn chế, hệ thống tự động kích hoạt bộ chuyển đổi OCR theo thứ tự ưu tiên:
   * **Engine 1 - RapidOCR:** Dùng thư viện ONNX Runtime nhẹ, độ chính xác cao.
   * **Engine 2 - Windows Native OCR:** Gọi API nhận diện chữ gốc của Windows 11 (`Windows.Media.Ocr`) qua PowerShell, không tốn tài nguyên Python.
   * **Engine 3 - Tesseract OCR:** Tương thích đa nền tảng (Linux, macOS, Windows) với cơ chế tự động fallback từ `-l eng+vie` về `-l eng`.
   Văn bản trích xuất được chèn tự động vào ngữ cảnh prompt để Gemini đọc hiểu như dữ liệu text thông thường.
4. **An toàn dọn dẹp file tạm:** Chỉ xóa các file tạm do bridge tạo ra trong tập `_TEMP_FILES`, không nhận hay xóa file tùy ý của người dùng trên ổ đĩa.

### 2.4. Rút Ngắn Ngữ Cảnh Thông Minh (Smart Truncation)
* **Chỉ áp dụng cho kết quả Tool:** Nội dung trò chuyện giữa người dùng và Agent được bảo toàn tuyệt đối, chỉ có output của các tool (như terminal logs lớn) mới được rút gọn.
* **Cơ chế Smart Truncation:**
  1. Giới hạn mặc định tăng lên `12,000` ký tự (`MAX_TOOL_RESULT_CHARS`).
  2. Quét phần nội dung ở giữa (giữ nguyên đầu và cuối) để phát hiện các dòng lỗi/stack trace (`Traceback`, `Error:`, `Exception:`, `fatal:`, v.v.).
  3. Gom cụm các dòng lỗi quan trọng nhất vào khối `KEY ERRORS/WARNINGS PRESERVED`.
  4. Cắt ngắn nội dung văn bản thừa nhưng luôn ưu tiên giữ nguyên khối lỗi bên trong prompt gửi đến Gemini.

### 2.5. Bảo Mật Nhiều Lớp (Rail-Guard Sandbox)
* **Lưu ý thiết kế:** Bộ lọc bảo mật trong `app/security.py` đóng vai trò là một **rail guard nội bộ** hỗ trợ một người dùng cá nhân tránh các thao tác nhầm lẫn nguy hiểm từ AI agent, không thay thế cho sandbox cấp hệ điều hành (Container/VM).
* **Chống vượt ngục đường dẫn (Path Traversal Prevention):** Sử dụng `os.path.realpath(os.path.abspath(path))` kết hợp `DEFAULT_PROJECT_DIR` để giải quyết đường dẫn tương đối an toàn, chặn triệt để mọi truy cập vào `C:\` hoặc các thư mục hệ điều hành gốc (`WINDOWS`, `PROGRAM FILES`, `PROGRAMDATA`).
* **Tường lửa lệnh dòng lệnh cứng rắn:** Lọc sạch các truy cập nguy hiểm vào ổ `C:\`, Registry, lệnh hủy hoại đĩa (`format <drive>:`), và các kỹ thuật bypass PowerShell (`$env:SystemDrive`, `IEX`, download execute). Cho phép các cờ định dạng lệnh hợp lệ như `prettier --format`, `ruff format`.

### 2.6. Chuẩn Hóa Mã Trạng Thái HTTP (OpenAI Error Standard)
Hệ thống chuyển đổi toàn bộ lỗi nội bộ thành các mã HTTP và cấu trúc JSON chuẩn OpenAI:
* **HTTP 429 (`rate_limit_error / bridge_busy`):** Bridge đang bận xử lý yêu cầu khác (hết thời gian chờ lock).
* **HTTP 502 (`upstream_error / gemini_upstream_error`):** Lỗi kết nối hoặc mất phản hồi từ máy chủ Gemini Web.
* **HTTP 503 (`authentication_error / gemini_not_authenticated`):** Chưa đăng nhập hoặc cookie hết hạn (phát hiện guest mode).
* **HTTP 504 (`timeout_error / gemini_timeout`):** Gemini Web không trả về dữ liệu sau khoảng thời gian `GEMINI_HARD_TIMEOUT`.

### 2.6. Nhật Ký Vận Hành & Quan Sát (Observability & Logging)
* Tự động ghi nhật ký đồng thời ra **Console** và tệp xoay vòng **`logs/bridge.log`** (5MB mỗi tệp, lưu giữ tối đa 3 bản sao lưu).
* Giám sát sức khỏe toàn diện qua endpoint `GET /health` (thời gian uptime, số lượng request, tỷ lệ lỗi, trạng thái cookie dự phòng, dung lượng cache).

### 2.7. Kiến Trúc Frontend Theo Chuẩn Web Components (W3C Custom Elements)
* **Loại bỏ hoàn toàn Monolithic HTML:** File `frontend/index.html` chỉ còn đúng ~30 dòng skeleton chứa các thẻ ngữ nghĩa declarative (`<bridge-navbar>`, `<bridge-metrics>`, `<bridge-terminal>`, `<bridge-model-switch>`, `<bridge-cookie-manager>`, `<bridge-quick-actions>`, `<bridge-toast>`).
* **Không phụ thuộc Node.js hay Build Tooling:** Chạy trực tiếp trên trình duyệt bằng chuẩn **ES Modules (`type="module"`)** và **Custom Elements v1**, mở trang là tải ngay trong 2ms.
* **Hỗ Trợ Song Ngữ (i18n):** Tích hợp từ điển đa ngôn ngữ (`frontend/js/i18n.js`) chuyển đổi linh hoạt giữa Tiếng Việt và English.
* **Hỗ Trợ Dark/Light Mode:** Quản lý theme qua CSS variables (`frontend/css/variables.css`), lưu sở thích vào `localStorage`.
* **Reactive Store Pattern (`frontend/js/state.js`):** Cơ chế PubSub Event Bus tách biệt hoàn toàn tầng dữ liệu và tầng hiển thị; mọi component tự động đăng ký và cập nhật DOM độc lập khi có state thay đổi.
* **Service Client Phân Lớp (`frontend/js/api.js`):** Gom toàn bộ lời gọi API qua HTTP về một tệp duy nhất để dễ bảo trì và xử lý lỗi tập trung.

### 2.8. Công Tắc Bật/Tắt API Chủ Động (On-Demand Connection Control & Draining)
* **Khởi động an toàn (Safe Startup):** Mặc định bridge khởi động ở trạng thái `off` (`API_START_STATE=off`). Server FastAPI, Dashboard và `/health` vẫn online, nhưng client Gemini Web hoàn toàn chưa được kết nối, không chạy vòng xoay cookie ngầm để bảo toàn tuổi thọ phiên Google.
* **Vòng đời 4 trạng thái:** `off` → `starting` → `on` → `stopping` → `off`.
* **Graceful Draining:** Khi người dùng bấm TẮT từ Dashboard:
  1. Chuyển state sang `stopping`: lập tức từ chối mọi request mới (HTTP 503 `bridge_disabled`).
  2. Theo dõi `_inflight` request đang chạy thông qua `asyncio.Event` (`_idle_event`). Chờ tối đa `DISABLE_DRAIN_TIMEOUT` giây (mặc định: 120s) để request dở dang hoàn tất.
  3. Khi không còn request nào chạy (`_inflight == 0`), tự động lưu metadata session ra `.bridge_session_<ID>.json` và gọi `client.close()`.
  4. Nếu quá thời gian timeout mà vẫn còn request chạy, Bridge hủy việc tắt, giữ nguyên trạng thái `on` và trả HTTP 409 `bridge_control_conflict` để không bao giờ cắt ngang tác vụ của người dùng.
* **Origin Guard cho Dashboard & Mạng LAN:** Kiểm tra header `Origin` trên các endpoint thay đổi trạng thái (`/v1/control/enable`, `/v1/control/disable`). Cho phép các kết nối non-browser (DSH, curl) và dashboard cùng cổng (localhost, 127.0.0.1 và các dải IP mạng LAN nội bộ RFC 1918), chặn đứng mọi cuộc tấn công Cross-Origin (CSRF) từ website ngoài internet.
