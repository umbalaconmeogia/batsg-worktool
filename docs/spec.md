# Spec: slack-redmine — Tạo Redmine ticket từ Slack message

## 1. Mục đích

Trên Slack, chọn một message bất kỳ → mở modal với nội dung prefill từ message (có thể edit) → submit → tạo issue trong Redmine project tương ứng với channel → post link ticket vào thread của message gốc.

Pattern tham khảo: GitHub official Slack integration ("⋮ → Create an Issue" → modal prefill). Khảo sát open source cho thấy chưa có project nào làm flow này cho Redmine mà còn được maintain, nên tự viết bằng Bolt.

## 2. Flow người dùng

1. Người dùng bấm `⋮` (More actions) trên message → chọn shortcut **Create Redmine ticket**.
2. App mở modal với các field:
   - Dòng context đầu modal: `Redmine project: *tên*` (chỉ đọc, cập nhật khi đổi project). Các ô theo thứ tự: Tracker, Subject, Description, Assignee, custom field bắt buộc, Priority, **Project**, checkbox Remember, Reply language — Project và Remember để gần cuối vì ít dùng (chỉ khi channel chưa map). Slack không hỗ trợ xếp hai `input` trên cùng một hàng.
   - **Priority** (dropdown, optional, ngay trên Project): danh sách từ `GET /enumerations/issue_priorities.json` (active), chọn sẵn priority `is_default` của Redmine. Giữ nguyên khi đổi project. Không lấy được enumeration → không hiện ô, Redmine dùng default.
   - **Project** (dropdown, `block_id` = `project`, `dispatch_action`): liệt kê mọi project active từ `GET /projects.json` (phân trang, cache), sắp xếp theo tên; ≤100 project dùng `static_select`, nhiều hơn dùng `external_select` (options handler lọc theo query). Chọn sẵn project map với channel (từ `slack-redmine-mapping.csv`), channel chưa map thì `default_project`, không có thì để trống; hint ghi rõ trạng thái map. Đổi project → app nhận `block_actions`, gọi `views.update` dựng lại modal cho project mới (tracker, assignee, custom field), giữ nguyên Subject/Description/checkbox người dùng đã sửa.
   - **Tracker** (dropdown): danh sách tracker của project, lấy từ Redmine API; default là tracker đầu tiên.
   - Trước khi prefill, message text được bỏ các mention user Slack (`<@U123>`, `<@U123|name>`) — Redmine không hiểu, và chúng hay đứng đầu message làm Subject xấu; khoảng trắng thừa và dòng trống đầu bị dọn. Các markup khác (`<!here>`, `<url|text>`, emoji `:x:`) giữ nguyên.
   - **Subject** (text): prefill dòng đầu tiên của message (sau khi dọn), cắt 100 ký tự.
   - **Description** (multiline): prefill toàn bộ message text (raw Slack mrkdwn, người dùng tự sửa nếu cần).
   - **Assignee** (dropdown, optional): danh sách member của project, lấy từ Redmine API. Default chọn sẵn chính người bấm shortcut, bằng cách match email Slack ↔ email Redmine; không match được thì để trống.
   - **Custom field bắt buộc** (nếu có): mỗi issue custom field có `is_required` và được bật trong project sinh một ô nhập (`block_id` = `cf_<id>`). Định nghĩa lấy từ `GET /custom_fields.json` (cần admin key; không có thì bỏ qua). Kiểu hỗ trợ: text (multiline), string/int/float/link (text), list/enumeration (dropdown, multi nếu `multiple`), bool (Yes/No), date (datepicker); kiểu khác bỏ qua. Ô nhập luôn required trong modal vì modal không cập nhật theo tracker đang chọn; field chỉ dùng cho một số tracker → thêm hint "Used by tracker: ...", chọn tracker khác thì Redmine tự bỏ qua giá trị (custom field không available cho tracker đó).
   - **Remember this project for this channel** (checkbox trong `actions` block ngay dưới Project, không có label riêng; giá trị vẫn đọc từ `view.state.values`): tích thì sau khi tạo ticket thành công, app ghi channel → project vào `slack-redmine-mapping.csv` (thêm mới hoặc cập nhật), kèm tên channel vào cột `channel_name` nếu có scope `channels:read`/`groups:read`; cột `note` giữ nguyên. Chỉ ghi khi project khác mapping hiện tại. Lỗi ghi file chỉ log, không ảnh hưởng ticket.
   - **Reply language** (dropdown, cuối modal): ngôn ngữ của message post lại vào thread và message báo lỗi. `vi` (Tiếng Việt) / `ja` (日本語); default lấy từ `default_language` trong config, không có thì `vi`.
3. Submit → app gọi Redmine REST API `POST /issues.json` tạo issue.
   - **Author của ticket = chính người submit** (impersonation): app match email Slack ↔ email Redmine, rồi gửi kèm header `X-Redmine-Switch-User: <login>` (cần admin API key). **Fallback mềm**: không match được email, hoặc Redmine từ chối (403 người đó thiếu quyền add issue / 412 user không hợp lệ) → retry không có header, ticket đứng tên user của API key.
   - Cuối description tự động append link ngược về message Slack gốc:

   ```
   ---
   From Slack: <permalink>
   ```

4. App post message vào thread của message gốc:

   > [STATUS] [Tracker #ID: Subject](url) — ticket được tạo bởi @user trong project **ProjectName**
   > (ja: [STATUS] [Tracker #ID: Subject](url) — @user がチケットをプロジェクト **ProjectName** に作成しました)

   - `@user` là mention Slack (`<@U...>`) của người bấm submit.

   - `[STATUS]` = tên status của ticket trên Redmine viết hoa (`[NEW]`, `[IN PROGRESS]`, `[CLOSED]`...), không ánh xạ sang chữ khác. Được cập nhật về sau, xem mục 2b.

   - Link dùng cú pháp mrkdwn của Slack `<url|Tracker #ID: Subject>` — hiển thị giống format của [redmine-ticket-copy-markdown](https://github.com/umbalaconmeogia/bookmarklet-collection/tree/main/src/redmine-ticket-copy-markdown).
   - Nếu vừa ghi mapping: thêm đuôi `(đã ghi nhớ project này cho channel)`; nếu channel chưa map và không ghi nhớ: `(channel này chưa được map project)`.
   - Tắt unfurl (không hiện preview link).

## 2b. Theo dõi status ticket

Mục đích: nhìn Slack biết ticket xong chưa, không phải mở Redmine.

- **Ghi nhận**: mỗi message bot post về ticket được ghi vào `posted-messages.csv` (gitignore, app tự maintain): `channel_id`, `ts`, `issue_ids` (`;`-separated), `posted_at`, `statuses` (nhãn đã ghi lần cuối), `body` (phần text sau tiền tố; rỗng = message nhiều ticket, sinh lại từ Redmine). Ghi qua file tạm + `os.replace`, có lock.
- **Job hằng ngày** (thread trong process, chạy 30s sau khi start rồi mỗi ngày lúc `status_check_time`): với từng dòng chưa quá `status_track_days` ngày, `GET /issues/<id>.json` → nếu chuỗi status khác lần trước thì `chat.update` message với tiền tố mới, giữ nguyên phần thân. Dòng quá hạn, ticket đã xoá (404), message đã xoá (`message_not_found`/`channel_not_found`) → bỏ khỏi file. Không quét Slack history; chỉ những message bot post sau khi có tính năng này mới được theo dõi.
- **Shortcut "Check Redmine status"** (`callback_id` = `check_redmine_status`, On messages): cập nhật ngay. Tìm mọi link `REDMINE_URL/issues/<id>` trong message.
  - Message của bot (so `bot_id`/`user` với `auth.test`): `chat.update` tại chỗ, chỉ thay tiền tố.
  - Message của người: Slack không cho app sửa → bot post một reply trong thread, mỗi ticket một dòng `[STATUS] [Tracker #ID: Subject](url)`. Bấm lại thì cập nhật reply cũ (tìm bằng `conversations.replies`, cần `channels:history`/`groups:history`; thiếu scope thì post reply mới). Reply này cũng được ghi vào `posted-messages.csv` để job hằng ngày theo dõi tiếp.
  - Không có link → ephemeral báo; lỗi Redmine → ephemeral kèm lý do. Ngôn ngữ: `default_language` (shortcut không có modal).

## 3. Kiến trúc

```
PC/điện thoại user ──► Slack cloud ◄── (WebSocket outbound, Socket Mode) ── App server
                                                                              │
                                                                              └──► Redmine (REST API)
```

- App là một process Python nhỏ dùng **Bolt for Python + Socket Mode**: chỉ cần outbound internet, không cần public IP/HTTPS endpoint. Chạy được trên VPS, container, hoặc máy luôn bật bất kỳ.
- User không bao giờ kết nối trực tiếp tới app server — mọi tương tác đi qua Slack.
- Redmine chỉ cần bật REST API; không cài plugin gì vào Redmine (phù hợp với Redmine hosted/cloud).

## 4. Cấu hình

Mọi thông tin môi trường-cụ-thể nằm ngoài code, để repo public được:

### `.env` (gitignore; có `.env.example`)

| Biến | Ý nghĩa |
|---|---|
| `SLACK_BOT_TOKEN` | Bot token `xoxb-...` |
| `SLACK_APP_TOKEN` | App-level token `xapp-...` (scope `connections:write`) |
| `REDMINE_URL` | URL của Redmine, ví dụ `https://redmine.example.com` |
| `REDMINE_API_KEY` | API key của user bot trên Redmine. Nên là **admin key** — cần cho việc tra user theo email (default assignee) và impersonation (author = người submit); không phải admin thì app vẫn chạy, chỉ mất 2 tính năng này (mọi ticket đứng tên user của API key). |

### `config.json` (gitignore; có `config.json.example`)

Setting tĩnh do người sửa, đọc một lần lúc khởi động (đổi thì restart):

```json
{
  "default_project": "general",
  "default_language": "vi"
}
```

- `default_project`: project chọn sẵn khi channel chưa có trong mapping. `null` → ô Project trống, người dùng phải chọn (Slack không cho submit khi input required trống).
- `default_language` (optional): `vi` hoặc `ja`, giá trị chọn sẵn của ô Reply language trong modal. Không có / không hợp lệ → `vi`.
- `status_check_time` (optional, `HH:MM` giờ local, mặc định `07:00`), `status_track_days` (optional, mặc định 90): xem mục 2b.

### `slack-redmine-mapping.csv` (gitignore; có `.example`)

Mapping Slack channel → Redmine project, **do app maintain** (checkbox Remember trong modal) nhưng người cũng sửa tay được. Cột: `channel_id`, `project` (Redmine identifier), `channel_name` (app điền khi ghi, cần scope đọc channel), `note` (tự do cho người, app không sửa). Đọc lại từ đĩa mỗi lần mở modal → sửa tay không cần restart. Ghi qua file tạm + `os.replace` (atomic) và có lock trong process. Tách khỏi `config.json` để app không ghi đè file người sửa tay.

Tương thích ngược: khởi động mà `config.json` có `slack_channel_redmine_project_map` và chưa có CSV → app chuyển sang CSV một lần (log info); `note` cũ dạng `Slack: <tên>` được tách thành `channel_name`, dạng khác giữ ở `note`. Có cả hai → CSV thắng, key cũ bị bỏ qua (log warning).

## 5. Setup phía Redmine

- Bật REST API: Administration → Settings → API → **Enable REST web service**.
- Tạo user riêng kiểu `slack-bot` (ticket không mang tên cá nhân), cấp quyền add issue vào các project liên quan, lấy API key. Muốn có default assignee theo email thì dùng API key của user có quyền admin.

## 6. Setup phía Slack (api.slack.com/apps → Create New App)

- **Interactivity & Shortcuts**: bật, tạo shortcut loại *On messages*, callback ID `create_redmine_ticket`.
- **Socket Mode**: bật, tạo App-Level Token scope `connections:write`.
- **OAuth & Permissions** — bot scopes: `commands`, `chat:write`, `users:read`, `users:read.email`, `im:write`, `channels:read`, `groups:read`, `channels:history`, `groups:history` → install vào workspace. Shortcut thứ hai: On messages, callback ID `check_redmine_status`.
- Bot phải được `/invite` vào channel thì mới post vào thread được.

## 7. Xử lý lỗi

- Redmine API fail (hoặc lỗi bất kỳ khi submit): gửi **ephemeral message** (chỉ người bấm thấy) trong channel/thread đó, kèm lý do lỗi. Không DM, không post public.
  - Redmine trả 422 kèm body `{"errors": [...]}` (ví dụ "Acceptance criteria cannot be blank", "Subject cannot be blank"): app đưa nguyên các message này vào ephemeral và log, không chỉ báo mã HTTP.
- Channel chưa map và không có default project: modal vẫn mở với ô Project trống, người dùng tự chọn (và tích Remember nếu muốn map luôn).
- Bot chưa được invite vào channel (thường là channel private): ticket đã tạo nhưng `chat.postMessage`/`chat.postEphemeral` đều trả `channel_not_found`. App fallback gửi **DM** cho người submit (scope `im:write`) với nội dung lẽ ra post vào thread + nhắc `/invite` bot; DM cũng thất bại thì log warning. Mục đích: người dùng không tưởng là lỗi rồi bấm lại tạo ticket trùng.

## 8. Ghi chú kỹ thuật

- `trigger_id` của Slack chỉ sống ~3 giây: trước khi mở modal, app phải gọi Redmine 2 lần (trackers + memberships). App cache kết quả các call này (TTL vài phút) để mở modal nhanh.
- Dropdown `static_select` của Slack giới hạn 100 option → lấy tối đa 100 member đầu của project.
- Message text từ shortcut payload là raw Slack mrkdwn (`<@U123>`, `<url|text>`...): phase 1 để nguyên, người dùng tự sửa trong modal.
- Response của `POST /issues.json` có sẵn `issue.tracker.name` — không cần gọi thêm API để build message post lại.

## 9. Hướng mở rộng (phase 2, chưa làm)

- ~~Override project ngay trong modal~~ (đã làm).
- Dropdown priority; set custom field link ngược về Slack thay vì append vào description.
- Emoji reaction trigger (react 🎫 → tạo ticket) qua Events API `reaction_added`.
- Attachment: shortcut payload không mang file; cần scope `files:read` + upload qua Redmine `/uploads.json`.
- Prefill form Redmine qua URL (`issues/new?issue[subject]=...`) đã cân nhắc nhưng bỏ — UX kém hơn modal.
