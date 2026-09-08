# Spec: slack-redmine — Tạo Redmine ticket từ Slack message

## 1. Mục đích

Trên Slack, chọn một message bất kỳ → mở modal với nội dung prefill từ message (có thể edit) → submit → tạo issue trong Redmine project tương ứng với channel → post link ticket vào thread của message gốc.

Pattern tham khảo: GitHub official Slack integration ("⋮ → Create an Issue" → modal prefill). Khảo sát open source cho thấy chưa có project nào làm flow này cho Redmine mà còn được maintain, nên tự viết bằng Bolt.

## 2. Flow người dùng

1. Người dùng bấm `⋮` (More actions) trên message → chọn shortcut **Create Redmine ticket**.
2. App mở modal với các field:
   - **Project** (hiển thị, không đổi được): tra từ mapping channel → project. Channel chưa được map thì dùng default project và hiển thị ghi chú.
   - **Tracker** (dropdown): danh sách tracker của project, lấy từ Redmine API; default là tracker đầu tiên.
   - **Subject** (text): prefill dòng đầu tiên của message, cắt 100 ký tự.
   - **Description** (multiline): prefill toàn bộ message text (raw Slack mrkdwn, người dùng tự sửa nếu cần).
   - **Assignee** (dropdown, optional): danh sách member của project, lấy từ Redmine API. Default chọn sẵn chính người bấm shortcut, bằng cách match email Slack ↔ email Redmine; không match được thì để trống.
3. Submit → app gọi Redmine REST API `POST /issues.json` tạo issue.
   - **Author của ticket = chính người submit** (impersonation): app match email Slack ↔ email Redmine, rồi gửi kèm header `X-Redmine-Switch-User: <login>` (cần admin API key). **Fallback mềm**: không match được email, hoặc Redmine từ chối (403 người đó thiếu quyền add issue / 412 user không hợp lệ) → retry không có header, ticket đứng tên user của API key.
   - Cuối description tự động append link ngược về message Slack gốc:

   ```
   ---
   From Slack: <permalink>
   ```

4. App post message vào thread của message gốc:

   > Đã tạo Redmine ticket [Tracker #ID: Subject](url) trong project **ProjectName**

   - Link dùng cú pháp mrkdwn của Slack `<url|Tracker #ID: Subject>` — hiển thị giống format của [redmine-ticket-copy-markdown](https://github.com/umbalaconmeogia/bookmarklet-collection/tree/main/src/redmine-ticket-copy-markdown).
   - Nếu channel chưa được map project, thêm đuôi: `(channel này chưa được map project)`.
   - Tắt unfurl (không hiện preview link).

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

```json
{
  "slack_channel_redmine_project_map": {
    "C0123456789": { "project": "project-a", "note": "#dev-team" },
    "C0987654321": "project-b"
  },
  "default_project": "general"
}
```

- Key là Slack channel ID; value là Redmine project identifier (string), hoặc object `{"project": ..., "note": ...}` trong đó `note` là ghi chú tự do cho người đọc (JSON không có comment), app bỏ qua.
- Channel không có trong map → dùng `default_project`. Nếu `default_project` là `null` → báo lỗi ephemeral, không tạo ticket.

## 5. Setup phía Redmine

- Bật REST API: Administration → Settings → API → **Enable REST web service**.
- Tạo user riêng kiểu `slack-bot` (ticket không mang tên cá nhân), cấp quyền add issue vào các project liên quan, lấy API key. Muốn có default assignee theo email thì dùng API key của user có quyền admin.

## 6. Setup phía Slack (api.slack.com/apps → Create New App)

- **Interactivity & Shortcuts**: bật, tạo shortcut loại *On messages*, callback ID `create_redmine_ticket`.
- **Socket Mode**: bật, tạo App-Level Token scope `connections:write`.
- **OAuth & Permissions** — bot scopes: `commands`, `chat:write`, `users:read`, `users:read.email` → install vào workspace.
- Bot phải được `/invite` vào channel thì mới post vào thread được.

## 7. Xử lý lỗi

- Redmine API fail (hoặc lỗi bất kỳ khi submit): gửi **ephemeral message** (chỉ người bấm thấy) trong channel/thread đó, kèm lý do lỗi. Không DM, không post public.
- Channel chưa map và không có default project: ephemeral báo "channel chưa được map", kèm channel ID để admin thêm vào config.

## 8. Ghi chú kỹ thuật

- `trigger_id` của Slack chỉ sống ~3 giây: trước khi mở modal, app phải gọi Redmine 2 lần (trackers + memberships). App cache kết quả các call này (TTL vài phút) để mở modal nhanh.
- Dropdown `static_select` của Slack giới hạn 100 option → lấy tối đa 100 member đầu của project.
- Message text từ shortcut payload là raw Slack mrkdwn (`<@U123>`, `<url|text>`...): phase 1 để nguyên, người dùng tự sửa trong modal.
- Response của `POST /issues.json` có sẵn `issue.tracker.name` — không cần gọi thêm API để build message post lại.

## 9. Hướng mở rộng (phase 2, chưa làm)

- Override project ngay trong modal (dropdown từ `/projects.json`).
- Dropdown priority; set custom field link ngược về Slack thay vì append vào description.
- Emoji reaction trigger (react 🎫 → tạo ticket) qua Events API `reaction_added`.
- Attachment: shortcut payload không mang file; cần scope `files:read` + upload qua Redmine `/uploads.json`.
- Prefill form Redmine qua URL (`issues/new?issue[subject]=...`) đã cân nhắc nhưng bỏ — UX kém hơn modal.
