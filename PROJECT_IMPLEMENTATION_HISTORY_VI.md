# Hồ Sơ Công Việc Đã Thực Hiện

> Bản ghi chi tiết các cải tiến, quyết định kỹ thuật và kết quả kiểm thử đã
> được xác nhận trong dự án DSH Gemini Bridge. Cập nhật lần cuối: 17/11/2026.
>
> Tài liệu này tập trung vào các hạng mục đã rà soát và triển khai gần đây.
> Những tính năng nền đã có từ trước được phân biệt với các thay đổi của từng
> pha; đây không phải danh sách đầy đủ mọi thay đổi lịch sử của toàn bộ dự án.
> Không đưa cookie, API key, prompt riêng tư hoặc nội dung ảnh vào tài liệu.

## 1. Tóm tắt trạng thái

### Đã triển khai và kiểm thử

- Kiểm soát timeout, cancellation, lock và dọn dẹp tài nguyên trong các luồng
  xử lý Gemini và ảnh.
- Chọn lại đúng model đang hoạt động không tạo `ChatSession` mới hoặc đánh dấu
  tool header là chưa gửi.
- Lời chào độc lập không gửi tool header; header đầy đủ vẫn chờ cho tác vụ thật.
- Lưu fingerprint của tool header để resume cùng chat/schema không phải gửi lại.
- Bảo đảm stream ảnh không ghép phần trả lời Vision dang dở với câu trả lời
  OCR/fallback khác.
- Không retry hoặc chuyển tài khoản để nối tiếp câu trả lời sau khi client đã
  nhận delta.
- Làm rõ cách tạo tool prompt từ schema của request hiện tại.
- Củng cố parser, allowlist tool và kiểm tra arguments theo schema.
- Tích hợp Local AI repair ở chế độ shadow; mặc định tắt và không thực thi kết
  quả.
- Chặn request tạo tiêu đề session nội bộ của DSH khỏi đường Gemini và tạo tiêu
  đề cục bộ; giữ nguyên `[NOTE]` cho chat thông thường.
- Kiểm tra cấu hình instance và chẩn đoán hiện có trước khi thêm hệ thống mới.
- Toàn bộ test tự động trong lần chạy cuối: **191 test pass**.

### Còn chưa xác nhận

- Local AI thật chưa trả về kết quả được Bridge chấp nhận: một canary trả HTTP
  200 nhưng bị đánh dấu `invalid_model_response`; lần kiểm tra cấu trúc sau đó
  timeout.
- Chưa có kiểm thử tích hợp đầy đủ qua DeepSeek Harness/Cline/Roo Code với
  payload tool thực tế của người dùng.
- Chưa xác minh việc Google dừng xử lý upstream sau khi Bridge cancel request.
- Chưa kiểm tra đường Gemini Vision upload trực tiếp bằng ảnh thật.

## 2. Pha A — Timeout, cancellation và chẩn đoán lỗi

### Vấn đề cần xử lý

Request có thể chờ lâu khi upstream Gemini không phản hồi. Với ảnh, thời gian
có thể bị tiêu thụ ở nhiều bước liên tiếp: upload/Vision, OCR dự phòng và gửi
văn bản OCR cho Gemini. Cần tránh tình trạng mỗi bước đặt lại timeout riêng,
request chiếm lock lâu hoặc file tạm không được xóa.

### Công việc đã làm

1. Rà soát luồng stream và non-stream trong `app/gemini_session.py`, luồng điều
   khiển trong `main.py` và deadline ảnh.
2. Thêm test client giả để kiểm tra timeout, nhận cancellation, giải phóng lock,
   reset trạng thái inflight và cho request kế tiếp chạy.
3. Bảo đảm timeout được trả thành lỗi có cấu trúc, gồm mã `gemini_timeout`;
   stream kết thúc rõ ràng thay vì để client chờ vô hạn.
4. Kiểm tra cleanup file ảnh tạm ở các nhánh thành công, lỗi và timeout.
5. Ghi rõ các nhóm lỗi/stage để phân biệt timeout, xác thực, quota, upstream và
   trạng thái Bridge bận. Activity log ghi model, instance, outcome, stage và
   thời gian tổng nhưng không lưu prompt, token, cookie, command hay nội dung ảnh.
6. Giữ nguyên chính sách retry/backoff và request interval thay vì thay đổi
   theo phỏng đoán.

### Lỗi phát hiện và cách xử lý

Test ban đầu tái hiện việc `asyncio.to_thread` không dừng worker OCR ngay khi
coroutine bị timeout. Trên Windows, worker còn đọc file khiến việc xóa file tạm
có thể thất bại. Cleanup được chuyển sang sau khi worker kết thúc; nếu lần xóa
đầu chưa thành công, file tiếp tục nằm trong danh sách cần dọn.

Đã đọc mã thư viện `gemini_webapi 2.1.1`: coroutine có xử lý cancellation ở
phần đã kiểm tra, nhưng chưa có bằng chứng để khẳng định Google dừng công việc
phía server sau khi client hủy request.

### Thử nghiệm thật được ghi nhận

- Guest Mode riêng, không dùng cookie: status xác nhận `is_guest=true`; một
  prompt text-only ngắn trả `GUEST_SMOKE_OK`. Không retry.
- Ảnh tổng hợp qua Guest Mode: OCR cục bộ bắt đầu, nhưng text generation
  timeout ở khoảng 29.6 giây trong lần thử với giới hạn 20 giây. Không retry.
- Thử lại một ảnh tổng hợp với override timeout 60 giây: OCR fallback rồi
  Gemini text trả marker `IMAGE SMOKE 0K`, tổng latency khoảng 4.5 giây.
  Override này chỉ dùng cho lần thử; mặc định production vẫn là 90 giây.
- Guest Mode không dùng luồng direct Vision upload; do đó kết quả trên chỉ xác
  nhận đường ảnh → OCR cục bộ → Gemini text.

**Files liên quan:** `app/activity.py`, `app/errors.py`,
`app/gemini_session.py`, `app/vision.py`, `main.py`,
`tests/test_session.py`, `tests/test_control.py`.

## 3. Pha B — Stream ảnh và tránh ghép câu trả lời

### Vấn đề cần xử lý

Nếu Vision đã phát một phần văn bản rồi timeout, sau đó Bridge phát tiếp câu
trả lời từ OCR fallback, client có thể nhận một câu trả lời bị ghép từ hai
nhánh khác nhau.

### Công việc đã làm

- Buffer delta từ Vision thay vì phát ngay cho client.
- Chỉ phát buffer nếu Vision hoàn tất thành công.
- Nếu Vision lỗi, timeout hoặc vượt giới hạn buffer 1 MiB, bỏ nội dung Vision
  dở dang và chuyển sang OCR/text fallback hoặc trả lỗi.
- Giữ SSE heartbeat khi đang chờ Vision.
- Sau khi một delta đã gửi cho client, lỗi mạng/xác thực không còn khởi động
  retry/failover để nối một câu trả lời mới vào stream cũ.
- Quota handoff sau delta tiếp tục bị chặn bởi guard hiện có. Các lỗi xảy ra
  trước delta vẫn theo chính sách retry/handoff vốn có.

**Đánh đổi UX:** với request có ảnh, văn bản Vision chỉ bắt đầu xuất hiện sau
khi nhánh Vision hoàn tất; không còn phát token Vision từng phần trong khi chờ.
Luồng text-only không đổi.

**Kiểm thử:** Vision thành công nhiều delta, timeout/exception sau delta, vượt
giới hạn buffer, heartbeat và xác nhận không lộ phần Vision đã bị hủy. Lỗi
network/auth/quota sau delta chỉ trả delta ban đầu cùng lỗi, không retry. Các
mốc kiểm thử khi triển khai lần lượt đạt 142 và 145 test.

**Files liên quan:** `app/gemini_session.py`, `main.py`,
`tests/test_session.py`, `tests/test_control.py`.

## 4. Pha C — Tool prompt, parser và kiểm tra arguments

### 4.1. Đồng bộ prompt với schema request

- Tool header được tạo từ schema thực sự của request hiện tại, gồm tên tool,
  tên trường, kiểu, required/optional và description có giới hạn độ dài.
- Không thêm ví dụ tool hoặc tham số giả vào prompt.
- Khi schema thay đổi, header cũ hết hiệu lực; khi tools bị bỏ khỏi request,
  Bridge không tái sử dụng danh sách cũ ngoài ý muốn.
- Không lặp tool header khi schema không đổi theo hành vi session hiện tại.

**Giới hạn:** chưa có payload tools thực tế đã được xác nhận từ Harness trong
workspace; kiểm thử sử dụng OpenAI function-schema fixtures.

### 4.2. Sửa parser theo quy tắc xác định

- Hỗ trợ bóc tách tool call có đánh dấu rõ, gồm dạng fenced JSON phù hợp.
- Sửa trailing comma chỉ khi xác định được dấu phẩy nằm ngoài chuỗi JSON.
- Giữ xử lý escape Windows path theo quy tắc đã kiểm thử.
- Không tự thêm ngoặc bị thiếu, không bịa trường bắt buộc và không biến văn
  xuôi nhắc tên tool thành lệnh gọi tool.
- Khi markup cho thấy model có ý định gọi tool nhưng parser từ chối, trả lỗi
  `invalid_tool_call` thay vì trả một phản hồi thành công giả.
- Log parser chỉ ghi loại lỗi, không ghi JSON, command, path hay arguments.

### 4.3. Allowlist và kiểm tra schema/đường dẫn

- Chỉ chấp nhận tool có tên chính xác trong tools map của request; tools map
  rỗng, tool lạ, sai khác hoa-thường, tool bị cấm hoặc batch có phần tử invalid
  đều bị từ chối.
- Arguments phải là object; kiểm tra required, kiểu string/boolean/number/
  integer/array/object, enum và `additionalProperties: false` khi schema quy
  định.
- Không tự điền required value.
- Alias chỉ đổi khi schema của chính tool khai báo key đích và không ghi đè một
  key nguồn hợp lệ.
- Path phải nằm trong project root theo quy tắc áp dụng; path bị chặn bị reject
  thay vì bị thay âm thầm bằng file giả.
- Lệnh bị chặn không ghi raw command vào log.

**Kiểm thử:** JSON hợp lệ/sai, fence, trailing comma trong/ngoài string,
Windows path, JSON cụt, prose, tool lạ, forbidden tool, required/type/enum,
extra key, alias theo schema, project path và nội dung log. Mốc cuối Pha A-C
đạt 160 test.

**Files liên quan:** `app/prompt_builder.py`, `app/tool_parser.py`,
`app/security.py`, `app/gemini_session.py`, `main.py`,
`tests/test_core.py`, `tests/test_streaming.py` và các test parser/security.

## 5. Pha D — Tích hợp Local AI repair ở shadow mode

### Mục tiêu và giới hạn

Local AI chỉ được thử như bộ sửa cú pháp cuối cùng khi parser xác định thất bại.
Nó không xử lý Gemini timeout, không tạo tool mới và không tự thực thi kết quả.
Tính năng mặc định là `TOOL_REPAIR_ENABLED=false`.

### Công việc đã làm

- Thêm `app/tool_repair.py` để lập lịch đánh giá bất đồng bộ trong cả route
  stream và non-stream.
- Chỉ candidate có tool nằm trong allowlist mới được xét; prose, candidate
  không rõ, schema không hợp lệ hoặc tool không khai báo bị bỏ qua.
- Không gửi candidate có trường `command`, `path`, `file_path` hoặc `cwd`.
- Trước inference, kiểm tra `/health` và `/slots`; không gửi nếu server chưa
  sẵn sàng hoặc đang bận.
- Chỉ gửi một request, không tự retry; evaluator chạy nền để không kéo dài
  response gốc của Bridge.
- Kết quả phải giữ nguyên token dữ liệu, đúng tên tool và qua parser/schema
  hiện có. Kết quả không được trả cho DSH hoặc thực thi.
- Log shadow chỉ ghi outcome, latency và độ dài candidate.
- Endpoint mặc định chỉ chấp nhận loopback/private IP. Sau khi chủ dự án xác
  nhận `192.196.10.2` là máy LAN, thêm allowlist tường minh
  `TOOL_REPAIR_TRUSTED_IPS`; không mở chấp nhận mọi địa chỉ public.
- Thêm `httpx` làm dependency và test mock cho phản hồi hợp lệ, thay đổi giá
  trị/tool, schema sai, model bận/chưa sẵn sàng, timeout, lỗi kết nối, endpoint
  không được tin cậy, protected arguments và cấu hình tắt.

### Kết quả thử với server thật

- `http://192.196.10.2:8000/health`, `/slots` và `/v1/models` đều HTTP 200.
- Server báo 4 slot và tất cả rảnh; model ID là
  `D:\AI\Models\Nemotron\Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf`.
- Canary 2 giây timeout sau khoảng 2003 ms.
- Canary riêng 10 giây nhận HTTP 200 sau khoảng 9358 ms nhưng evaluator trả
  `invalid_model_response`.
- Lần kiểm tra metadata response tiếp theo timeout ở 10 giây, không thu được
  body. Vì vậy chưa xác định server trả thiếu field, content rỗng/sai kiểu hay
  phản hồi quá chậm.

**Kết luận trạng thái:** code shadow và mock tests đã có; live inference chưa
được xác nhận thành công. Không bật tính năng thường trực cho đến khi xác định
được response format và canary cho kết quả `valid_repair`.

**Files liên quan:** `app/tool_repair.py`, `app/tool_parser.py`,
`app/config.py`, `.env.example`, `requirements.txt`, `main.py`,
`tests/test_tool_repair.py`, `tests/test_control.py`.

## 6. Pha E — Những quyết định về phạm vi

- **TASK-009 — alias model/routing:** chưa đổi alias `1`/`2`/`acc1`/`acc2` thành
  lựa chọn worker. Hiện model ID phải lấy từ `/v1/models`; chưa có yêu cầu và
  dữ liệu đủ để ánh xạ alias mà không gây đổi model ngoài ý muốn.
- **TASK-010 — nhiều agent/instance:** xác nhận mô hình một process/instance
  cho mỗi lane. Gateway dùng `10000`; worker `acc1` và `acc2` lần lượt dùng
  `10001` và `10002`. Cấu hình, session, log/activity, cookie cache, account
  state, chain và handoff được tách theo instance. `tests/test_instance.py`
  kiểm tra đường dẫn không giao nhau. Chưa thêm Session Registry.
- **TASK-011 — metrics/dashboard mới:** không thêm dashboard riêng vì activity
  log hiện đã cung cấp model, instance, outcome/error, stage và latency tổng;
  chưa có bằng chứng nhu cầu cần thêm metrics theo stage.

Các mục ngoài mục tiêu cá nhân như RBAC nhiều người dùng, VM/container bắt buộc,
xử lý quota bằng cách tự đổi IP, retry liên tục hoặc để Local AI tự tạo tool
đã không được triển khai.

## 7. Xử lý request tạo tiêu đề session của DSH

### Vấn đề

DSH gửi một request riêng có nội dung dạng:

```text
Generate the session title from this JSON array of human messages:
[{"seq":12,"text":"chào"}]
```

Bridge trước đó xem đây như tin nhắn user bình thường. Vì vậy nội dung được đưa
vào Gemini Web, tạo thêm một lượt gọi và xuất hiện trong chat Gemini dù đây chỉ
là yêu cầu tạo tiêu đề giao diện.

### Cách xử lý đã triển khai

- Thêm `app/title_generation.py` để chỉ nhận diện mẫu có prefix chính xác, một
  user message và JSON array hợp lệ gồm các trường `text`.
- Tiêu đề lấy từ tin nhắn human đầu tiên không rỗng; chuẩn hóa khoảng trắng và
  giới hạn độ dài tối đa 80 ký tự.
- Route trong `main.py` trả OpenAI-compatible completion cục bộ cho cả stream
  và non-stream, trước bước xác thực/gọi Gemini và trước khi ghi transcript/
  ledger. Do đó request tiêu đề không làm bẩn chat Gemini hoặc session nội bộ.
- Request khớp prefix nhưng JSON sai bị trả `invalid_title_request` HTTP 400,
  không rơi tiếp xuống Gemini.
- Request chat thông thường không đổi; `[NOTE]` vẫn được thêm theo hành vi cũ.
- Activity log chỉ ghi stage, latency và độ dài, không ghi prompt hay tiêu đề.

### Đánh đổi

Tiêu đề cục bộ là cách rút gọn xác định từ tin nhắn human đầu tiên, không có khả
năng tóm tắt nhiều lượt như model. Mẫu chỉ được xử lý cục bộ khi nhận diện đúng
request title chuyên biệt; các request khác tiếp tục theo luồng bình thường.

### Kiểm thử

- JSON title hợp lệ, chuẩn hóa và giới hạn độ dài.
- Tin nhắn thường không bị nhận nhầm; title JSON hỏng/trailing text/entry không
  phải human bị từ chối.
- Route non-stream và stream trả title, không xác thực hoặc gọi Gemini, không
  ghi transcript và không lưu prompt vào activity.
- Test riêng xác nhận `[NOTE]` vẫn còn trên user message thông thường.

Files: `app/title_generation.py`, `main.py`,
`tests/test_title_generation.py`, `tests/test_control.py`.

## 8. Tránh gửi lại tool header không cần thiết

### Vấn đề

Log vận hành ghi nhận prompt `hi` đi cùng tool header, tổng cộng 16–18 nghìn ký
tự. Ngoài ra, thao tác `gemini-carbon → gemini-carbon` vẫn tạo `ChatSession` mới
và gửi lại header. Sau khi Bridge khởi động lại, session resume cũng đánh dấu
header là chưa gửi vì session state chỉ lưu metadata chat/model, không ghi nhận
schema nào đã có trong hội thoại.

### Cách xử lý

- Chỉ lời chào độc lập dạng text (`hi`, `hello`, `hey`, `chào`, `xin chào`) mới
  hoãn header. Prompt chào được gửi riêng; `tool_header_sent` vẫn false để tác
  vụ kế tiếp nhận đủ tool schema và hướng dẫn.
- Giữ danh sách tool, mô tả, tên/type/required của tham số; rút gọn cách trình
  bày thành `!name:type` (bắt buộc) và `?name:type` (tùy chọn), đồng thời gộp
  các quy tắc lặp. Header này được gửi khi chat mới hoặc model/schema đổi;
  resume cùng fingerprint không gửi lại.
- Chọn lại model đang hoạt động với `keep_chat=True` giữ nguyên `ChatSession`
  và cờ header, không tạo lại context.
- Lưu SHA-256 của tool header trong session state. Khi resume, Bridge so sánh
  fingerprint với schema request hiện tại; cùng schema thì giữ header đã có,
  khác schema thì gửi header mới. Chỉ lưu hash, không lưu nội dung schema.
- Session state cũ chưa có fingerprint có thể cần gửi header một lần để khởi
  tạo dấu vết; những lần resume sau dùng fingerprint đã lưu.
- `keep_chat=False` vẫn giữ hành vi tạo chat mới có chủ đích.

### Kiểm thử

- Chỉ standalone greeting được nhận diện; request có thêm nội dung hoặc ảnh không
  bị xem là greeting.
- Route stream/non-stream gửi greeting không kèm header.
- Sau greeting, header vẫn pending và được gắn vào prompt tác vụ kế tiếp.
- Gọi lại model đang hoạt động không gọi `start_chat`, không thay chat hiện tại,
  không xóa cờ tool header và không đặt lại bộ đếm turn.
- Fingerprint khớp được giữ qua resume; schema đổi làm header được gửi lại.

Files: `app/prompt_builder.py`, `app/gemini_session.py`, `main.py`,
`tests/test_core.py`, `tests/test_session.py`, `tests/test_control.py`.

## 9. Kiểm thử và xác minh tổng hợp

### Test tự động

- Các mốc Pha A-C tăng dần qua 135, 137, 142, 145, 149 rồi 160 test khi thêm
  các nhóm kiểm thử.
- Sau khi xử lý title cục bộ, model no-op, defer greeting và lưu fingerprint:
  `python -m unittest discover -s tests` — **191 tests, OK**.
- Test tập trung Local AI: **16 tests pass**.
- Test parser title cục bộ: **6 tests pass**; route/control: **29 tests pass**
  (bao gồm greeting defer header và model giữ nguyên `ChatSession`).
- `py_compile` cho các module và test Local AI đã cập nhật: pass.
- Pylance syntax checks trước đó không phát hiện lỗi trên các file parser,
  repair và test liên quan.
- Một lần chạy toàn bộ song song từng có lỗi deadline ảnh phụ thuộc thời gian;
  test đó pass khi chạy riêng và lần chạy toàn bộ tuần tự cuối cùng cũng pass.

### Phân biệt test tự động và smoke test thật

Unit tests của Local AI dùng mock, không gửi dữ liệu ra model thật. Các smoke
test Gemini Guest Mode và probe Local AI được ghi riêng ở trên; chúng không
thay thế kiểm thử tích hợp đầy đủ với DSH/Harness hoặc direct Gemini Vision.

## 10. Các điểm cần tiếp tục

1. Ghi nhận cấu trúc response của llama.cpp theo cách không lưu nội dung nhạy
   cảm; phân biệt lỗi thiếu `choices`, thiếu `message`, content rỗng/sai kiểu,
   JSON không hợp lệ và timeout.
2. Thử canary nhỏ với model/template hiện tại; đối chiếu các cảnh báo
   `<tool_response>`/`<|s|>` trong log server, nhưng không xem chúng là nguyên
   nhân cho đến khi có bằng chứng.
3. Chỉ sau khi response hợp lệ mới quyết định timeout shadow dài hơn. Tăng
   timeout không tự sửa được response sai cấu trúc.
4. Khi thử với DSH thật, dùng schema/candidate tổng hợp hoặc đã loại dữ liệu
   nhạy cảm; tiếp tục xác nhận repaired output không được thực thi.
5. Xử lý cảnh báo tài khoản `default` và `acc2` dùng chung trước khi dựa vào hai
   instance như hai danh tính độc lập.
6. Kiểm tra firewall/API key của dịch vụ Local AI nếu tiếp tục bind
   `0.0.0.0`; chỉ mở cho máy Bridge cần truy cập.

## Tài liệu tham khảo

- [Tổng quan dự án](./PROJECT_OVERVIEW_VI.md)
- [README tiếng Việt](./README_VI.md)
- [Kiến trúc tiếng Việt](./ARCHITECTURE_VI.md)
- [Kế hoạch triển khai và ghi nhận test](./TODO_IMPLEMENTATION_PLAN_2026-11-16.txt)
