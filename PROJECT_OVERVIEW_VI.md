# Tổng Quan Dự Án DSH Gemini Bridge

> Tài liệu tóm tắt mục đích, phạm vi xử lý, các vấn đề đã gặp, cách xử lý và
> hướng phát triển. Cập nhật lần cuối: 17/11/2026.

## 1. Mục đích ban đầu

DSH Gemini Bridge là lớp trung gian để DeepSeek Harness (DSH/pi-ai), Cline hoặc
Roo Code có thể sử dụng Gemini Web như một backend theo giao thức tương thích
OpenAI Chat Completions.

Bridge nhận request từ coding agent, chuyển đổi nội dung cần thiết để gửi qua
`gemini_webapi`, quản lý phiên Gemini Web, rồi chuẩn hóa phản hồi thành nội dung
hoặc `tool_calls` cho client. Khi cần gọi công cụ trên máy, DSH/client là bên
thực thi công cụ; Bridge không dùng Local AI để tự chạy lệnh.

Mục tiêu chính là tận dụng Gemini Web trong quy trình coding agent, đồng thời
giữ phiên làm việc ổn định, hỗ trợ ảnh và có các giới hạn vận hành/bảo vệ phù
hợp với môi trường cá nhân.

## 2. Luồng và nội dung được xử lý

1. **Nhận request:** Client gửi `POST /v1/chat/completions` với messages, tool
   schemas và có thể kèm ảnh Base64.
2. **Chuẩn bị prompt:** Bridge xử lý phần nội dung mới của cuộc hội thoại, tạo
   prompt phù hợp với Gemini và rút gọn output công cụ quá dài khi cần.
3. **Xử lý ảnh:** Ưu tiên gửi ảnh cho Gemini Web. Nếu đường upload không dùng
   được, có các phương án OCR dự phòng như RapidOCR, Windows OCR hoặc Tesseract.
4. **Gọi Gemini Web:** Session manager quản lý phiên, giới hạn request đồng
   thời và dùng cookie đã cấu hình để kết nối.
5. **Chuẩn hóa phản hồi:** Bridge trả nội dung văn bản hoặc bóc tách tool call
   thành định dạng OpenAI-compatible. DSH/client tiếp tục quyết định và thực
   thi công cụ.
6. **Theo dõi vận hành:** Dashboard, health/status và log giúp quan sát trạng
   thái Bridge, tài khoản, request và worker.

## 3. Chức năng hiện có

- API tương thích OpenAI Chat Completions cho các client coding agent.
- Quản lý hội thoại Gemini Web theo phiên và hỗ trợ streaming/SSE.
- Bóc tách tool call, chuẩn hóa một số tham số và kiểm tra theo schema.
- Hỗ trợ ảnh, nén ảnh và OCR dự phòng.
- Quản lý cookie, phát hiện trạng thái Guest và hỗ trợ tài khoản dự phòng.
- Hàng đợi có giới hạn thời gian, timeout, health check và Dashboard.
- Hỗ trợ nhiều instance/worker với cấu hình và session tách biệt.
- Chọn lại model hiện đang hoạt động không tạo session Gemini mới; tránh gửi lại
  toàn bộ tool header khi không cần thiết.
- Lời chào thuần không gửi tool header; header được giữ lại cho request tác vụ
  đầu tiên.
- Fingerprint tool header được lưu cùng metadata session để không gửi lặp khi
  resume cùng chat và cùng schema.
- Tool header giữ nguyên các công cụ, mô tả và tham số nhưng dùng cách trình
  bày gọn hơn để giảm prompt gửi ở lượt đầu.
- Nhận diện request tạo tiêu đề session nội bộ của DSH và tạo tiêu đề cục bộ,
  tránh gửi request phụ này lên Gemini; `[NOTE]` vẫn giữ trong chat thông thường.
- Các hàng rào nội bộ cho đường dẫn và lệnh. Đây là biện pháp hỗ trợ giảm thao
  tác ngoài ý muốn, không thay thế sandbox/container ở cấp hệ điều hành.

## 4. Khó khăn đã gặp và cách xử lý

### 4.1. Phiên Gemini và cookie

Gemini Web phụ thuộc vào session/cookie; cookie hết hạn hoặc rơi vào Guest có thể
làm gián đoạn agent.

**Cách xử lý:** kiểm tra trạng thái xác thực, hỗ trợ nạp lại cookie, quản lý
cookie dự phòng và giới hạn thời gian chờ request. Cần bảo đảm mỗi instance dùng
đúng tài khoản/cookie của nó; hiện có cảnh báo cấu hình `default` và `acc2` dùng
chung tài khoản cần được xử lý riêng.

### 4.2. Tool call sai cú pháp hoặc không đúng schema

Gemini có thể trả về tool call lỗi JSON hoặc thiếu/sai tham số. Nếu Bridge không
bóc tách được, agent không nhận được tool call hợp lệ.

**Cách xử lý:** ưu tiên parser xác định và kiểm tra schema/allowlist. Local AI
repair được tích hợp thêm ở chế độ shadow: chỉ xem xét candidate tool call lỗi
cú pháp; không dùng khi Gemini timeout; không sửa các trường nhạy cảm như
`command`, `path`, `file_path`, `cwd`; kết quả không được gửi cho DSH hoặc thực
thi. Tính năng mặc định đang tắt.

**Trạng thái kiểm tra Local AI thật (17/11/2026):**

- `192.196.10.2:8000` đã xác nhận là máy LAN của chủ dự án; IP này được cho phép
  bằng allowlist chính xác thay vì mở quyền cho mọi IP ngoài private range.
- `/health`, `/slots` và `/v1/models` phản hồi HTTP 200; server công bố model
  `D:\AI\Models\Nemotron\Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf`.
- Canary 2 giây timeout. Canary riêng 10 giây nhận HTTP 200 sau khoảng 9.36 giây
  nhưng Bridge đánh dấu `invalid_model_response`. Lần kiểm tra cấu trúc response
  tiếp theo timeout trước khi lấy được body.
- Vì vậy chưa xác định chính xác server thiếu field nào, trả content rỗng/sai
  kiểu hay phản hồi quá chậm. Local AI chưa được xác nhận hoạt động xuyên suốt;
  chưa nên bật shadow thường trực.

### 4.3. Ảnh và thời hạn xử lý

Upload ảnh phụ thuộc vào Gemini Web và có thể chậm hoặc thất bại.

**Cách xử lý:** nén ảnh, đặt deadline tổng và dùng OCR dự phòng khi upload native
không thành công. Cần tiếp tục kiểm tra trên ảnh thực tế và tải lớn để tránh
timeout giữa upload, OCR và phản hồi.

### 4.4. Cô lập và bảo vệ thao tác

Tool call từ model không nên được xem là an toàn chỉ vì có schema. Sai khác nhỏ
trong đường dẫn hoặc tham số có thể gây thao tác ngoài ý muốn.

**Cách xử lý:** dùng allowlist/schema, lọc các trường nhạy cảm và kiểm tra đường
dẫn/lệnh trong Bridge/agent. Không xem các kiểm tra này là thay thế cho quyền
hạn hệ điều hành, firewall hoặc sandbox chuyên dụng.

## 5. Hướng mở rộng

1. **Hoàn tất chẩn đoán Local AI:** ghi log có cấu trúc nhưng không ghi nội dung
   prompt/response; tách lỗi thiếu `choices`, thiếu `message`, content rỗng/sai
   kiểu, JSON không hợp lệ và timeout. Sau đó kiểm tra chat template/tokenizer
   của model bằng canary nhỏ.
2. **Đánh giá shadow trên bộ mẫu an toàn:** đo tỷ lệ sửa đúng, từ chối, thay đổi
   dữ liệu và độ trễ. Chỉ cân nhắc bật thường trực sau khi đạt tiêu chí và có
   quyết định rõ ràng của chủ dự án.
3. **Bổ sung kiểm thử tích hợp với client thật:** kiểm tra request tools/ảnh,
   streaming, lỗi upstream và nhiều instance qua DSH/Harness.
4. **Cải thiện vận hành:** xử lý cấu hình tài khoản trùng, chuẩn hóa cấu hình
   từng instance, kiểm tra khôi phục session và theo dõi lỗi theo từng worker.
5. **Tăng khả năng quan sát:** hiển thị latency, trạng thái cookie, timeout,
   OCR fallback và các kết quả shadow dưới dạng số liệu tổng hợp, không lưu dữ
   liệu nhạy cảm.
6. **Tăng cường triển khai LAN:** giới hạn firewall theo IP cần thiết, dùng API
   key và tránh để endpoint dịch vụ mở rộng hơn phạm vi sử dụng.

## 6. Tiêu chí hoàn thành

- Client có thể gửi hội thoại qua API tương thích OpenAI và nhận phản hồi ổn
  định từ Gemini Web.
- Tool calls và ảnh được xử lý đúng luồng; lỗi upstream được trả về rõ ràng.
- Session, cookie, timeout và nhiều instance hoạt động theo cấu hình, không
  dùng chung trạng thái ngoài ý muốn.
- Các test tự động pass; các kịch bản tích hợp thực tế quan trọng được kiểm tra
  và ghi nhận riêng.
- Local AI chỉ được bật khi phản hồi thật vượt qua JSON/schema/giữ nguyên dữ
  liệu; kết quả shadow không bao giờ tự thực thi tool.

## Tài liệu liên quan

- [README tiếng Việt](./README_VI.md) — cài đặt, cấu hình và vận hành.
- [Kiến trúc tiếng Việt](./ARCHITECTURE_VI.md) — luồng dữ liệu và thiết kế chi
  tiết.
- [Kế hoạch triển khai](./TODO_IMPLEMENTATION_PLAN_2026-11-16.txt) — trạng thái
  tác vụ và kết quả kiểm thử gần nhất.
