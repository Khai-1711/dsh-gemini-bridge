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
* **Vấn đề:** Gemini Web dựa vào cookies trình duyệt (`SECURE_1PSID`, `SECURE_1PSIDTS`). Sau một thời gian hoạt động hoặc khi tải cao, cookie có thể hết hạn hoặc bị Google tạm khóa session, khiến toàn bộ agent bị gián đoạn.
* **Giải pháp v3.0:**
  * **Hàng đợi có kiểm soát thời gian (`Lock with Timeout`):** Sử dụng `asyncio.wait_for(self.lock.acquire(), timeout=30.0)`. Nếu server đang xử lý tác vụ khác quá lâu, hệ thống sẽ trả thông báo bận (HTTP 429) thay vì để request treo vô tận.
  * **Tự động chuyển đổi tài khoản (Failover):** Khi ghi nhận 3 lỗi xác thực liên tiếp, hệ thống tự động ngắt kết nối client hiện tại và khởi tạo client mới với bộ cookie dự phòng (`BACKUP_1PSID`, `BACKUP_1PSIDTS`).
  * **Hot-Reload không cần khởi động lại:** Endpoint `POST /v1/update-cookies` cho phép cập nhật cookie mới trực tiếp ngay trong lúc server đang chạy qua giao diện Web Dashboard hoặc API.
  * **Kéo dài phiên tự động:** Kích hoạt `auto_refresh=True` trong `GeminiClient.init()` để duy trì session sống lâu trong suốt ngày làm việc.

### 2.2. Bộ Nhớ Đệm Phản Hồi Thông Minh (Prompt Cache)
* Tích hợp bộ nhớ cache cục bộ dựa trên mã băm SHA-256 của prompt với thời gian sống (TTL) 5 phút.
* Khi agent lặp lại câu hỏi hoặc truy vấn thông tin tương tự (chưa kèm ảnh), hệ thống trả về kết quả đã cache ngay lập tức (độ trễ < 1ms), tiết kiệm quota và tài nguyên mạng.

### 2.3. Tầng Xử Lý Hình Ảnh Đa Phương Thức (Multimodal 3-Tier Fallback)
Khi người dùng đính kèm ảnh màn hình (Base64) trong DeepSeek Harness:
1. **Nén ảnh tự động:** Ảnh được tối ưu kích thước (tối đa 1600x1600, chất lượng JPEG 85%) bằng Pillow để giảm thiểu dung lượng RAM và thời gian truyền tải.
2. **Tầng 1 (Native Vision):** Gửi trực tiếp tệp ảnh vào tham số `files=[...]` của Gemini Web API.
3. **Tầng 2 (3-Engine OCR Fallback):** Nếu cổng upload ảnh của Google từ chối hoặc cookie phụ bị hạn chế, hệ thống tự động kích hoạt bộ chuyển đổi OCR theo thứ tự ưu tiên:
   * **Engine 1 - RapidOCR:** Dùng thư viện ONNX Runtime nhẹ, độ chính xác cao.
   * **Engine 2 - Windows Native OCR:** Gọi API nhận diện chữ gốc của Windows 11 (`Windows.Media.Ocr`) qua PowerShell, không tốn tài nguyên Python.
   * **Engine 3 - Tesseract OCR:** Tương thích đa nền tảng (Linux, macOS, Windows).
   Văn bản trích xuất được chèn tự động vào ngữ cảnh prompt để Gemini đọc hiểu như dữ liệu text thông thường.

### 2.4. Rút Ngắn Ngữ Cảnh Thông Minh (Smart Truncation)
* **Vấn đề của thuật toán cũ:** Cắt lấy nửa đầu và nửa cuối của kết quả Tool (khi vượt quá giới hạn ký tự) thường làm mất phần stack trace hoặc thông báo lỗi cốt lõi ở giữa.
* **Cơ chế Smart Truncation v3.0:**
  1. Quét toàn bộ nội dung kết quả tool để phát hiện các từ khóa lỗi (`Traceback`, `Error`, `Exception`, `Failed`, `line`, v.v.).
  2. Gom cụm các dòng lỗi quan trọng nhất vào một khối bảo vệ riêng biệt (`KEY ERRORS/WARNINGS PRESERVED`).
  3. Cắt ngắn nội dung văn bản thông thường nhưng luôn ưu tiên giữ nguyên khối lỗi bên trong prompt gửi đến Gemini.

### 2.5. Bảo Mật Nhiều Lớp (Defense-in-Depth Sandbox)
* **Chống vượt ngục đường dẫn (Path Traversal Prevention):** Sử dụng `os.path.realpath(os.path.abspath(path))` để triệt tiêu mọi thủ thuật dùng đường dẫn tương đối (ví dụ `..\..\..\Windows\System32`) trỏ vào ổ C:\.
* **Tường lửa lệnh dòng lệnh cứng rắn:** Bổ sung regex nhận diện và triệt tiêu các kỹ thuật bypass trong PowerShell:
  * Biến môi trường hệ thống: `$env:SystemDrive`, `$env:windir`, `$env:APPDATA`.
  * Ký tự thoát: Backtick escape (ví dụ `` C`:\ ``).
  * Lệnh thực thi động: `Invoke-Expression`, `iex`, WebRequest download string.
  * Tương tác trực tiếp với .NET Framework: `[System.IO.File]`, `[System.Diagnostics.Process]`.

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
