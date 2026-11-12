# Ghi chú phân tích · Day 04

Họ và tên: Trịnh Gia Huy
MSSV: 2A202603924

## 1. Case được chọn

- ID case:
- Câu trả lời của agent:
- Điểm ở lần chạy 1:

## 2. Bằng chứng trong trace

Dán đoạn trace chứng minh lỗi và nêu nguyên nhân có thể xảy ra.

```text

```

## 3. Thay đổi đã thực hiện

- Sửa gì, ở đâu:
- Vì sao chọn thay đổi này:

## 4. So sánh hai lần chạy

| Chỉ số | `eval_before.json` | `eval_after.json` |
|---|---|---|
| Điểm tổng |  |  |
| Điểm case đã chọn |  |  |
| Số model calls |  |  |
| Token, chi phí nếu có |  |  |

- Lỗi đã giải quyết:
- Lỗi còn lại hoặc mới xuất hiện:

## 5. Giới hạn

Nêu giới hạn của điểm judge, của snapshot và của việc chỉ chạy một lần mỗi phiên bản.

---

## Phụ lục · Nhật ký checkpoint

Ghi lại việc đã làm, bằng chứng và quan sát ở từng checkpoint. Mục 1–5 ở trên được điền ở CP5–CP6.

### Cấu hình môi trường

| Mục | Giá trị |
|---|---|
| Python | 3.11 (conda env `kaizen-py311`; `python3` hệ thống là 3.8, không đủ 3.10+) |
| Provider / model agent | `gemini` / `gemini-3.5-flash-lite` (free tier, giới hạn 5 request/phút) |
| Judge | Chưa chốt (mặc định cùng model với agent) |
| Key | Đặt trong `.env` (đã nằm trong `.gitignore`), không commit |

### CP1 · Setup và khám phá tools

**Đã làm**
- Repo đặt tên đúng mẫu L3B: `K5-L3B-DAY04-TrinhGiaHuy-2A202603924-ReactLoopAgent`.
- Chạy 2 lệnh khám phá tool trong GUIDE và thử các trường hợp lỗi của `tools.execute`.
- Unit test ban đầu: 37 test, 9 lỗi, tất cả do `agent.py` chưa implement (`NotImplementedError`).

**Hai tool trả gì**
- `get_status(provider, component)`: status tổng quan, tối đa 5 component và 5 incident khớp chuỗi trong 50 incident gần nhất, `captured_at`, `source` (trang chủ status). Response thành công **không có key `ok`**.
  Ví dụ `get_status("github", "Actions")`: `indicator: none` (All Systems Operational), 23 incident khớp trong 50 incident gần nhất.
- `get_incident_details(provider, incident_id)`: `ok: true`, components, **chỉ 3 update** (2 mới nhất + update đầu tiên, mỗi update tối đa 900 ký tự), `updates_total` và `url` để dẫn nguồn.
  Ví dụ `wp49bp8bqo6c` (Cloudflare): phạm vi nằm trong nội dung update, *"Non-fedramp customers are not impacted"*.

**Lỗi được trả thành observation `{ok: false, error, message}`, không ném exception**

| Tình huống | `error` |
|---|---|
| Tool không tồn tại | `UNKNOWN_TOOL` |
| Provider sai (`aws`) | `ValueError` |
| Arguments không phải dict | `ValueError` |
| Thiếu tham số | `TypeError` |
| Incident không có trong 50 incident gần nhất | `INCIDENT_NOT_FOUND` |
| Timeout giả lập | `TimeoutError` |

**Giới hạn dữ liệu**
- Tìm theo chuỗi, không theo nghĩa: `get_status("cloudflare", "xóa bộ nhớ đệm")` trả 0 incident dù có incident cache purge. Tìm không thấy **không** chứng minh không có sự cố.
- Chỉ xem 50 incident gần nhất; snapshot chụp ngày 09/11/2026, không phải trạng thái hiện tại.

**Thay đổi hạ tầng (không thuộc agent loop)**: gặp HTTP 429 liên tục với Gemini free tier. Nguyên nhân: tool đọc snapshot trả kết quả tức thì nên agent gọi model liên tiếp trong vài giây; mỗi case tốn 2–8 lượt gọi agent + 1 lượt gọi judge, cả bộ 22 case khoảng 80–100 request; code gốc gặp 429 là ném lỗi ngay khiến case bị `unscored`. Đã sửa `providers.py`:
- Giới hạn phía client `LLM_RPM` (tối đa N request mỗi 60 giây, tính riêng từng model, dùng chung cho agent và judge).
- Thử lại khi gặp 429/5xx theo `Retry-After`, hoặc `retryDelay` của Gemini, hoặc chờ tăng dần; tối đa `LLM_MAX_RETRIES` lần.
- Hết quota theo ngày (`PerDay`) thì dừng ngay và báo rõ, không thử lại.
- Tự đọc `.env`; biến đã `export` trong shell được ưu tiên.

Đã kiểm tra offline bằng response giả lập: 429/phút chờ đúng `retryDelay` rồi thành công; 429/ngày dừng ngay; 400 không thử lại.

### CP2 · Bài 1: agent loop

**Đã làm** (`agent.py`, hàm `run_agent`)
1. `messages` = system prompt + câu hỏi.
2. Lặp tối đa `max_steps` lần; **mỗi lần gọi model là 1 step**.
3. Append **nguyên vẹn** assistant message (kể cả `tool_calls`) trước tool results.
4. Có `calls` thì xử lý **mọi** call theo thứ tự: parse arguments, `tools.execute`, ghi trace `{'event':'tool','step','name','arguments','result'}`, gửi lại `{'role':'tool','tool_call_id','content'}`.
5. Quyết định theo `calls` trước, không theo `finish` (có provider trả `stop` kèm tool call).
6. Không còn call: `finish=='stop'` thì `completed`; `length` hay lý do khác thì `error`. Hết lượt thì `max_steps`.

**Kiểm tra**
- Unit test: 34/37 pass (còn `test_invalid_json_sent_back_to_model` cho CP3 và 2 test `EvidenceGate` cho CP5).
- Câu hỏi thật: `python run.py 'GitHub Actions hiện có sự cố không? Hãy kiểm tra incident gần đây.'`

```text
status: completed
step 1 model finish=tool_calls -> get_status({"provider": "github"})                                  incidents=5
step 2 model finish=tool_calls -> get_incident_details({"incident_id": "h02n9572mx9y", "provider": "github"})  ok=True
step 3 model finish=tool_calls -> get_incident_details({"provider": "github", "incident_id": "xr3myzj7ms51"})  ok=True
step 4 model finish=stop       -> final
usage: 322 + 807 + 1232 + 2206 = 4567 tokens, không gặp 429
```

**Quan sát (ứng viên phân tích ở CP5)**
- Model gọi `get_status` **không truyền `component="Actions"`**, nên nhận 5 incident chung của GitHub thay vì lọc theo Actions.
- Câu trả lời chỉ dẫn trang chủ `githubstatus.com`, **không có URL incident** dù tool đã trả `url` (`https://stspg.io/pjgptg3cfc6n` cho `xr3myzj7ms51`, `https://stspg.io/7pwv28hvkmz8` cho `h02n9572mx9y`). Đây là lỗi phần kiểm tra nguồn ở Bài 4 nhắm tới.
- Agent đã đọc chi tiết `h02n9572mx9y` nhưng câu trả lời không nhắc tới incident này.
- Agent ghi `xr3myzj7ms51` xảy ra ngày 04/11/2026: **đúng** (`created_at` 2026-11-04T16:05Z; 07/11 chỉ là `updated_at`). Incident có 9 update nhưng tool chỉ trả 3.
- Câu trả lời có nêu thời điểm snapshot và giới hạn bằng chứng.

### CP3 · Bài 2: xử lý lỗi và trace

**Đã làm** (`agent.py`)

| Tình huống | Xử lý | Kết thúc run? |
|---|---|---|
| Provider lỗi (HTTP, 429 hết lượt thử lại, timeout, response thiếu `message`) | `status='error'`, trace `{'event':'error','source':'provider'}` | Có: không có output model để tiếp tục |
| Arguments không phải JSON hoặc không phải object | Tool result `{'ok':false,'error':'INVALID_ARGUMENTS'}` gửi lại model | Không: model tự sửa hoặc báo thiếu dữ liệu |
| Tool không tồn tại / provider sai / timeout | Giữ nguyên `{ok:false,...}` của `tools.execute` làm observation | Không |
| Tool ném exception bất ngờ / trả về không phải dict | `_execute_tool` chuyển thành `{ok:false,...}` | Không |
| `finish='length'` hoặc lý do khác | `status='error'`, trace `source='model'` | Có: output bị cắt không được báo `completed` |

Trace: mọi event có `event` và `step`; tool event có thêm `error` khi tool lỗi; event `final` ghi độ dài câu trả lời; không ghi key; thông báo lỗi cắt còn 200 ký tự. `answer` luôn là chuỗi.

**Kiểm tra**
- Unit test: 35/37 pass. Còn 2 test `EvidenceGate` (`test_missing_url_gets_revision_within_budget`, `test_missing_url_at_budget_limit_is_not_completed`) thuộc CP5.
- 9 kịch bản lỗi chạy offline với provider giả lập, không kịch bản nào crash:

```text
provider lỗi (429 hết retry)  status=error      trace: error(source=provider)
response sai format           status=error      trace: error(source=provider, KeyError: 'message')
finish=length, content None   status=error      trace: model -> error(source=model); answer kiểu str
JSON arguments lỗi '{'        status=completed  trace: model -> tool(error=INVALID_ARGUMENTS) -> model -> final
arguments là list             status=completed  trace: model -> tool(error=INVALID_ARGUMENTS) -> model -> final
tool không tồn tại            status=completed  trace: model -> tool(error=UNKNOWN_TOOL) -> model -> final
provider không hợp lệ (aws)   status=completed  trace: model -> tool(error=ValueError) -> model -> final
timeout giả lập               status=completed  trace: model -> tool(error=TimeoutError) -> model -> final
tool ném exception            status=completed  trace: model -> tool(error=KeyError) -> model -> final
```

`completed` chỉ nghĩa là agent dừng bình thường, không có nghĩa câu trả lời đúng; phần đúng sai do judge chấm ở eval.

**Chưa làm ở CP3**: không chạy lại qua Gemini (đủ kiểm chứng offline, giữ quota cho eval).

### Kế hoạch tiếp theo

- **CP4**: chạy `eval_before.json` với agent hiện tại (chưa thêm kiểm tra nguồn, chưa sửa prompt) để làm mốc so sánh. Chạy thử `--case E2` trước. Kiểm tra `summary.score_0_100` khác `null`.
- **CP5**: chọn lỗi từ eval (ứng viên: thiếu URL incident trong câu trả lời), implement kiểm tra nguồn `validation_missing_source`, điền mục 1–3.
- **CP6**: chạy `eval_after.json` cùng cấu hình, điền mục 4–5.
