"""Slack app: create a Redmine ticket from a Slack message.

Flow:
1. User picks "Create Redmine ticket" from a message's shortcut menu (More actions).
2. A modal opens: Project dropdown (default = project mapped from the channel),
   Tracker/Assignee/required custom fields loaded from Redmine for that project,
   Subject/Description prefilled from the message text. Changing the project
   rebuilds the project-dependent fields.
3. On submit, an issue is created in the chosen project and a link to the ticket
   is posted back into the message's thread. Optionally the channel -> project
   mapping is saved (slack-redmine-mapping.csv) for next time.

See docs/spec.md for the full specification.
"""

import csv
import json
import os
import re
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

APP_DIR = Path(__file__).parent
load_dotenv(APP_DIR / ".env")

REDMINE_URL = os.environ["REDMINE_URL"].rstrip("/")
REDMINE_API_KEY = os.environ["REDMINE_API_KEY"]

CONFIG_PATH = APP_DIR / "config.json"
if not CONFIG_PATH.exists():
    raise SystemExit("config.json not found. Copy config.json.example to config.json and edit it.")
with open(CONFIG_PATH, encoding="utf-8") as f:
    _config = json.load(f)

DEFAULT_PROJECT = _config.get("default_project")

# Language of the messages the bot posts back to Slack. The modal has a
# selector (last field) whose initial value is this; "vi" if unset/unknown.
MESSAGES = {
    "vi": {
        "created": "Đã tạo Redmine ticket {link} trong project *{project}*",
        "unmapped": " (channel này chưa được map project)",
        "remembered": " (đã ghi nhớ project này cho channel)",
        "failed": "⚠️ Tạo ticket Redmine thất bại: {error}",
        "not_in_channel": "⚠️ Bot chưa được thêm vào channel <#{channel}> nên không post được vào thread. "
                          "Hãy `/invite @{bot}` vào channel đó.",
    },
    "ja": {
        "created": "Redmineチケット {link} をプロジェクト *{project}* に作成しました",
        "unmapped": "（このチャンネルはプロジェクトにマッピングされていません）",
        "remembered": "（このチャンネルのプロジェクトとして記憶しました）",
        "failed": "⚠️ Redmineチケットの作成に失敗しました: {error}",
        "not_in_channel": "⚠️ Botがチャンネル <#{channel}> に追加されていないため、スレッドに投稿できませんでした。"
                          "そのチャンネルで `/invite @{bot}` してください。",
    },
}
LANGUAGE_LABELS = {"vi": "Tiếng Việt", "ja": "日本語"}
DEFAULT_LANGUAGE = _config.get("default_language", "vi")
if DEFAULT_LANGUAGE not in MESSAGES:
    DEFAULT_LANGUAGE = "vi"

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# --- Channel -> project mapping (slack-redmine-mapping.csv) -----------------
#
# Maintained by the app (the modal's "Remember" checkbox) but also editable by
# hand; it is re-read on every use so edits need no restart.
# Columns: channel_id, project (Redmine identifier), channel_name (filled by
# the app when it can read channel info), note (free text for humans; the app
# never touches it).

MAPPING_PATH = APP_DIR / "slack-redmine-mapping.csv"
MAPPING_FIELDS = ["channel_id", "project", "channel_name", "note"]
_mapping_lock = threading.Lock()


def _read_mapping_rows():
    if not MAPPING_PATH.exists():
        return []
    with open(MAPPING_PATH, encoding="utf-8", newline="") as f:
        return [
            {k: (row.get(k) or "").strip() for k in MAPPING_FIELDS}
            for row in csv.DictReader(f)
            if (row.get("channel_id") or "").strip()
        ]


def _write_mapping_rows(rows):
    tmp = MAPPING_PATH.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MAPPING_FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, MAPPING_PATH)  # atomic: readers never see a half-written file


def load_mapping():
    """{channel_id: project identifier}, fresh from disk."""
    with _mapping_lock:
        return {r["channel_id"]: r["project"] for r in _read_mapping_rows() if r["project"]}


def save_mapping(channel_id, project, channel_name=""):
    """Insert or update one channel's project. Never modifies the human `note` column."""
    with _mapping_lock:
        rows = _read_mapping_rows()
        for r in rows:
            if r["channel_id"] == channel_id:
                r["project"] = project
                if channel_name:
                    r["channel_name"] = channel_name
                break
        else:
            rows.append({"channel_id": channel_id, "project": project,
                         "channel_name": channel_name, "note": ""})
        _write_mapping_rows(rows)


def _migrate_legacy_mapping():
    """One-time: config.json used to hold the mapping; move it to the CSV."""
    legacy = _config.get("slack_channel_redmine_project_map")
    if not legacy:
        return
    if MAPPING_PATH.exists():
        app.logger.warning(
            "config.json still has slack_channel_redmine_project_map; it is ignored, "
            "%s is the source of truth now. Remove the key from config.json.", MAPPING_PATH.name,
        )
        return
    rows = []
    for channel_id, v in legacy.items():
        project = v["project"] if isinstance(v, dict) else v
        note = v.get("note", "") if isinstance(v, dict) else ""
        # The old note conventionally held "Slack: <channel name>".
        channel_name = note[len("Slack: "):].strip() if note.startswith("Slack: ") else ""
        rows.append({"channel_id": channel_id, "project": project,
                     "channel_name": channel_name, "note": "" if channel_name else note})
    with _mapping_lock:
        _write_mapping_rows(rows)
    app.logger.info("Migrated %d channel mappings from config.json to %s", len(rows), MAPPING_PATH.name)


_migrate_legacy_mapping()

# --- Redmine helpers -------------------------------------------------------

# trigger_id must be used within ~3s, so Redmine lookups are cached.
_cache = {}
CACHE_TTL = 300  # seconds


def _cached(key, fetch):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = fetch()
    _cache[key] = (now, value)
    return value


def redmine_get(path, params=None):
    r = requests.get(
        f"{REDMINE_URL}{path}",
        headers={"X-Redmine-API-Key": REDMINE_API_KEY},
        params=params,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


class RedmineError(Exception):
    """Redmine rejected a request; str(e) carries Redmine's own error messages."""


def redmine_error_message(r):
    """Human-readable reason from a failed Redmine response.

    On 422 Redmine returns {"errors": ["Subject cannot be blank", ...]};
    other statuses usually have no useful body.
    """
    try:
        errors = r.json().get("errors")
    except ValueError:
        errors = None
    if errors:
        return "; ".join(errors)
    return f"HTTP {r.status_code} {r.reason}"


def get_projects():
    """All active projects visible to the API key, sorted by name."""
    def fetch():
        projects, offset = [], 0
        while True:
            data = redmine_get("/projects.json", {"limit": 100, "offset": offset, "status": 1})
            projects += data.get("projects", [])
            offset += 100
            if offset >= data.get("total_count", 0) or not data.get("projects"):
                break
        return sorted(projects, key=lambda p: p["name"].lower())
    return _cached("projects", fetch)


def get_project(identifier):
    """Project info incl. name, trackers and enabled issue custom fields."""
    return _cached(
        f"project:{identifier}",
        lambda: redmine_get(
            f"/projects/{identifier}.json", {"include": "trackers,issue_custom_fields"}
        )["project"],
    )


def get_issue_custom_fields():
    """All issue custom field definitions (requires an admin API key; [] otherwise)."""
    def fetch():
        try:
            data = redmine_get("/custom_fields.json")
        except requests.HTTPError:
            return []
        return [c for c in data.get("custom_fields", []) if c.get("customized_type") == "issue"]
    return _cached("custom_fields", fetch)


def get_required_custom_fields(project):
    """Required issue custom fields enabled on the project.

    Redmine rejects the issue (422) when one of these is blank, so the modal
    asks for them. Whether a field is required also depends on the tracker
    (custom field -> trackers), which the caller uses to decide optional/hint.
    """
    enabled = {c["id"] for c in project.get("issue_custom_fields", [])}
    return [c for c in get_issue_custom_fields() if c["id"] in enabled and c.get("is_required")]


def get_members(identifier):
    """Users who are members of the project (groups are skipped)."""
    def fetch():
        data = redmine_get(f"/projects/{identifier}/memberships.json", {"limit": 100})
        return [m["user"] for m in data.get("memberships", []) if "user" in m]
    return _cached(f"members:{identifier}", fetch)


def find_redmine_user_by_email(email):
    """Redmine user (id, login, ...) whose mail matches, or None. Requires an admin API key."""
    def fetch():
        try:
            data = redmine_get("/users.json", {"name": email, "limit": 10})
        except requests.HTTPError:
            return None  # non-admin key: silently skip default-assignee and impersonation
        for u in data.get("users", []):
            if u.get("mail", "").lower() == email.lower():
                return u
        return None
    return _cached(f"user:{email}", fetch)


def slack_user_email(client, user_id):
    try:
        return client.users_info(user=user_id)["user"]["profile"].get("email")
    except Exception:
        return None


def slack_channel_name(client, channel_id):
    """Name of a channel (without '#'), or '' (needs channels:read / groups:read)."""
    try:
        return client.conversations_info(channel=channel_id)["channel"]["name"]
    except Exception:
        return ""


# --- Modal helpers ---------------------------------------------------------

_USER_MENTION = re.compile(r"<@[UW][A-Z0-9]+(?:\|[^>]*)?>")


def clean_slack_text(text):
    """Message text without Slack user mentions (<@U123> / <@U123|name>).

    Mentions are noise in a ticket (Redmine cannot resolve them) and often
    sit at the start of the message, which would make a bad Subject. Also
    tidies the whitespace they leave behind and drops leading blank lines.
    """
    text = _USER_MENTION.sub("", text or "")
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    return "\n".join(lines).rstrip()


def plain(text):
    return {"type": "plain_text", "text": str(text)[:150]}


def custom_field_block(cf, project_trackers):
    """Slack input block for a Redmine custom field, or None if unsupported.

    The field is always required in the modal (the modal cannot react to the
    tracker dropdown). If only some trackers of the project use the field, a
    hint lists them; Redmine silently drops the value for other trackers.
    """
    cf_tracker_ids = {t["id"] for t in cf.get("trackers", [])}
    if not cf_tracker_ids:
        return None  # enabled for no tracker: Redmine never asks for it
    fmt = cf.get("field_format")
    default = cf.get("default_value") or ""
    block_id = f"cf_{cf['id']}"

    if fmt in ("list", "enumeration"):
        options = [
            {"text": plain(v.get("label") or v["value"]), "value": str(v["value"])}
            for v in cf.get("possible_values", []) if v.get("value") not in (None, "")
        ][:100]
        if not options:
            return None
        element = {
            "type": "multi_static_select" if cf.get("multiple") else "static_select",
            "action_id": "v",
            "options": options,
        }
        initial = [o for o in options if o["value"] == str(default)]
        if initial and not cf.get("multiple"):
            element["initial_option"] = initial[0]
    elif fmt == "bool":
        options = [{"text": plain("Yes"), "value": "1"}, {"text": plain("No"), "value": "0"}]
        element = {"type": "static_select", "action_id": "v", "options": options}
        if str(default) in ("0", "1"):
            element["initial_option"] = options[0] if default == "1" else options[1]
    elif fmt == "date":
        element = {"type": "datepicker", "action_id": "v"}
    elif fmt in ("string", "text", "int", "float", "link"):
        element = {"type": "plain_text_input", "action_id": "v", "multiline": fmt == "text"}
        if default:
            element["initial_value"] = str(default)
    else:
        return None  # user, version, attachment...: not supported in the modal

    block = {
        "type": "input",
        "block_id": block_id,
        "label": plain(cf["name"]),
        "element": element,
    }
    project_tracker_ids = {t["id"] for t in project_trackers}
    if not project_tracker_ids <= cf_tracker_ids:
        names = [t["name"] for t in project_trackers if t["id"] in cf_tracker_ids]
        block["hint"] = plain("Used by tracker: " + ", ".join(names))
    return block


def input_value(state):
    """Value of one modal input from view.state.values[block][action]."""
    t = state.get("type")
    if t == "plain_text_input":
        return state.get("value")
    if t in ("static_select", "external_select"):
        o = state.get("selected_option")
        return o["value"] if o else None
    if t in ("multi_static_select", "checkboxes"):
        return [o["value"] for o in state.get("selected_options") or []]
    if t == "datepicker":
        return state.get("selected_date")
    return None


def view_field(view, block_id):
    """Current value of one input in a modal (from view.state.values), or None."""
    values = view.get("state", {}).get("values", {})
    block = values.get(block_id)
    if not block:
        return None
    return input_value(next(iter(block.values())))


PROJECT_ACTION_ID = "project_select"
REMEMBER_ACTION_ID = "remember_toggle"
MAX_STATIC_OPTIONS = 100  # Slack's limit for static_select


def project_option(p):
    return {"text": plain(p["name"]), "value": p["identifier"]}


def project_block(projects, initial_ident, mapped_ident):
    """Project dropdown. Changing it dispatches block_actions -> modal rebuilt.

    static_select when the project list fits Slack's 100-option limit,
    external_select (options served by `project_options`) otherwise.
    """
    initial = next((p for p in projects if p["identifier"] == initial_ident), None)
    if len(projects) <= MAX_STATIC_OPTIONS:
        element = {
            "type": "static_select",
            "action_id": PROJECT_ACTION_ID,
            "options": [project_option(p) for p in projects],
        }
    else:
        element = {"type": "external_select", "action_id": PROJECT_ACTION_ID, "min_query_length": 0}
    if initial:
        element["initial_option"] = project_option(initial)
    if mapped_ident:
        hint = f"Channel này map tới project: {mapped_ident}"
    elif DEFAULT_PROJECT:
        hint = f"Channel chưa map project, dùng default: {DEFAULT_PROJECT}"
    else:
        hint = "Channel chưa map project và không có default project"
    return {
        "type": "input",
        "block_id": "project",
        "dispatch_action": True,
        "label": plain("Redmine project"),
        "hint": plain(hint),
        "element": element,
    }


def build_view(client, meta, project_ident, subject=None, description=None, remember=False):
    """The ticket modal for one project. meta is the private_metadata dict.

    subject/description: current values to keep when the modal is rebuilt
    after a project change (default: prefilled from the message text).
    """
    projects = get_projects()
    project = get_project(project_ident) if project_ident else None
    trackers = project.get("trackers", []) if project else []
    members = get_members(project_ident) if project else []
    required_cfs = get_required_custom_fields(project) if project else []

    msg = meta["text"]
    if subject is None:
        subject = msg.splitlines()[0][:100] if msg else ""
    if description is None:
        description = msg

    # Read-only summary at the top; the Project dropdown itself sits near the
    # bottom (rarely changed: only when the channel is not mapped yet).
    project_label = f"*{project['name']}*" if project else "_chưa chọn_"
    blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"Redmine project: {project_label}"}]}]

    if trackers:
        tracker_options = [{"text": plain(t["name"]), "value": str(t["id"])} for t in trackers]
        blocks.append({
            "type": "input",
            "block_id": "tracker",
            "label": plain("Tracker"),
            "element": {
                "type": "static_select",
                "action_id": "v",
                "options": tracker_options,
                "initial_option": tracker_options[0],
            },
        })

    blocks += [
        {
            "type": "input",
            "block_id": "subject",
            "label": plain("Subject"),
            "element": {"type": "plain_text_input", "action_id": "v", "initial_value": subject},
        },
        {
            "type": "input",
            "block_id": "description",
            "label": plain("Description"),
            "element": {
                "type": "plain_text_input",
                "action_id": "v",
                "multiline": True,
                "initial_value": description,
            },
        },
    ]

    if members:
        # Default assignee: the submitting user, if they are a member (matched by email).
        initial_assignee_id = None
        email = slack_user_email(client, meta["user"])
        if email:
            ruser = find_redmine_user_by_email(email)
            if ruser and any(u["id"] == ruser["id"] for u in members):
                initial_assignee_id = ruser["id"]
        assignee_options = [
            {"text": plain(u["name"]), "value": str(u["id"])} for u in members[:MAX_STATIC_OPTIONS]
        ]
        assignee_element = {"type": "static_select", "action_id": "v", "options": assignee_options}
        if initial_assignee_id is not None:
            assignee_element["initial_option"] = next(
                o for o in assignee_options if o["value"] == str(initial_assignee_id)
            )
        blocks.append({
            "type": "input",
            "block_id": "assignee",
            "optional": True,
            "label": plain("Assignee"),
            "element": assignee_element,
        })

    for cf in required_cfs:
        block = custom_field_block(cf, trackers)
        if block:
            blocks.append(block)

    blocks.append(project_block(projects, project_ident, meta["mapped_project"]))

    # Checkbox right under the Project dropdown, no label of its own. An
    # `actions` block (not `input`) so Slack renders no label; its value still
    # arrives in view.state.values.
    remember_option = {
        "text": plain("Remember this project for this channel"),
        "description": plain("Saves channel → project to slack-redmine-mapping.csv"),
        "value": "yes",
    }
    remember_element = {"type": "checkboxes", "action_id": REMEMBER_ACTION_ID, "options": [remember_option]}
    if remember:
        remember_element["initial_options"] = [remember_option]
    blocks.append({"type": "actions", "block_id": "remember", "elements": [remember_element]})

    language_options = [{"text": plain(label), "value": code} for code, label in LANGUAGE_LABELS.items()]
    blocks.append({
        "type": "input",
        "block_id": "language",
        "label": plain("Reply language"),
        "element": {
            "type": "static_select",
            "action_id": "v",
            "options": language_options,
            "initial_option": next(o for o in language_options if o["value"] == DEFAULT_LANGUAGE),
        },
    })

    return {
        "type": "modal",
        "callback_id": "submit_redmine_ticket",
        "private_metadata": json.dumps(meta),
        "title": plain("Create Redmine ticket"),
        "submit": plain("Create"),
        "blocks": blocks,
    }


def notify_user(client, channel_id, user_id, thread_ts, text, msgs):
    """Tell the submitting user something, as an ephemeral in the thread.

    If the bot is not a member of the channel (private channel without
    /invite: chat.* returns channel_not_found), fall back to a DM so the user
    still learns the outcome — otherwise they see nothing and retry, creating
    duplicate tickets. The DM needs the im:write scope; without it we only log.
    """
    try:
        client.chat_postEphemeral(channel=channel_id, user=user_id, thread_ts=thread_ts, text=text)
        return
    except Exception as e:
        first_error = e
    try:
        bot_name = client.auth_test().get("user", "redmine-ticket")
        hint = msgs["not_in_channel"].format(channel=channel_id, bot=bot_name)
        client.chat_postMessage(channel=user_id, text=f"{text}\n{hint}", unfurl_links=False)
    except Exception as e:
        app.logger.warning(
            "Could not notify user %s: ephemeral failed (%s), DM failed (%s)", user_id, first_error, e
        )


# --- Slack handlers --------------------------------------------------------

@app.shortcut("create_redmine_ticket")
def open_modal(ack, shortcut, client):
    ack()
    channel_id = shortcut["channel"]["id"]
    mapped_project = load_mapping().get(channel_id)
    meta = {
        "channel": channel_id,
        "ts": shortcut["message"]["ts"],
        "user": shortcut["user"]["id"],
        "text": clean_slack_text(shortcut["message"].get("text", "")),
        "mapped_project": mapped_project,  # None if the channel is not mapped
    }
    project_ident = mapped_project or DEFAULT_PROJECT
    client.views_open(trigger_id=shortcut["trigger_id"], view=build_view(client, meta, project_ident))


@app.action(PROJECT_ACTION_ID)
def change_project(ack, body, client):
    """Project dropdown changed: rebuild the modal for the new project, keeping edits."""
    ack()
    view = body["view"]
    meta = json.loads(view["private_metadata"])
    selected = body["actions"][0].get("selected_option")
    project_ident = selected["value"] if selected else None
    client.views_update(
        view_id=view["id"],
        hash=view["hash"],
        view=build_view(
            client, meta, project_ident,
            subject=view_field(view, "subject"),
            description=view_field(view, "description"),
            remember=bool(view_field(view, "remember")),
        ),
    )


@app.action(REMEMBER_ACTION_ID)
def toggle_remember(ack):
    ack()  # value is read from view.state on submit; nothing to do here


@app.options(PROJECT_ACTION_ID)
def project_options(ack, payload):
    """Options for the external_select variant of the project dropdown (>100 projects)."""
    q = (payload.get("value") or "").lower()
    matches = [p for p in get_projects() if q in p["name"].lower() or q in p["identifier"].lower()]
    ack(options=[project_option(p) for p in matches[:MAX_STATIC_OPTIONS]])


@app.view("submit_redmine_ticket")
def handle_submit(ack, view, client, body):
    ack()
    meta = json.loads(view["private_metadata"])
    channel_id, msg_ts = meta["channel"], meta["ts"]
    user_id = body["user"]["id"]
    values = view["state"]["values"]

    def field(block_id):
        return input_value(values[block_id]["v"]) if block_id in values else None

    language = field("language")
    msgs = MESSAGES.get(language, MESSAGES[DEFAULT_LANGUAGE])

    project_ident = view_field(view, "project")
    subject = field("subject")
    description = field("description") or ""
    remember = bool(view_field(view, "remember"))

    issue = {"project_id": project_ident, "subject": subject}
    if field("tracker"):
        issue["tracker_id"] = int(field("tracker"))
    if field("assignee"):
        issue["assigned_to_id"] = int(field("assignee"))
    custom_fields = [
        {"id": int(block_id[len("cf_"):]), "value": field(block_id)}
        for block_id in values if block_id.startswith("cf_") and field(block_id) not in (None, "", [])
    ]
    if custom_fields:
        issue["custom_fields"] = custom_fields

    try:
        # Link back to the original Slack message so the ticket can be traced.
        permalink = client.chat_getPermalink(channel=channel_id, message_ts=msg_ts)["permalink"]
        issue["description"] = description + f"\n\n---\nFrom Slack: {permalink}"

        # Impersonation: create the ticket as the submitting user (matched by
        # email) so they become its author. Soft fallback: no email match, or
        # Redmine rejects the switch (403 no permission / 412 invalid user)
        # -> retry as the API-key user.
        headers = {"X-Redmine-API-Key": REDMINE_API_KEY}
        email = slack_user_email(client, user_id)
        ruser = find_redmine_user_by_email(email) if email else None
        if ruser and ruser.get("login"):
            headers["X-Redmine-Switch-User"] = ruser["login"]

        def create_issue():
            return requests.post(
                f"{REDMINE_URL}/issues.json",
                headers=headers,
                json={"issue": issue},
                timeout=15,
            )

        r = create_issue()
        if r.status_code in (403, 412) and "X-Redmine-Switch-User" in headers:
            del headers["X-Redmine-Switch-User"]
            r = create_issue()
        if not r.ok:
            raise RedmineError(redmine_error_message(r))
        created = r.json()["issue"]
        issue_id = created["id"]
        tracker_name = created.get("tracker", {}).get("name", "Ticket")
        project_name = created.get("project", {}).get("name", project_ident)

        remembered = False
        if remember and project_ident != meta["mapped_project"]:
            try:
                save_mapping(channel_id, project_ident, slack_channel_name(client, channel_id))
                remembered = True
            except Exception as e:
                app.logger.warning("Could not save mapping %s -> %s: %s", channel_id, project_ident, e)

        # Same visual format as redmine-ticket-copy-markdown: [Tracker #ID: Subject](url)
        link = f"<{REDMINE_URL}/issues/{issue_id}|{tracker_name} #{issue_id}: {subject}>"
        text = msgs["created"].format(link=link, project=project_name)
        if remembered:
            text += msgs["remembered"]
        elif not meta["mapped_project"]:
            text += msgs["unmapped"]
        try:
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=msg_ts,
                text=text,
                unfurl_links=False,
            )
        except Exception as e:
            # Ticket exists; make sure the user knows (else they retry -> duplicates).
            app.logger.warning("Post-back to %s failed (%s); notifying user directly", channel_id, e)
            notify_user(client, channel_id, user_id, msg_ts, text, msgs)
    except Exception as e:
        notify_user(client, channel_id, user_id, msg_ts, msgs["failed"].format(error=e), msgs)
        raise


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
