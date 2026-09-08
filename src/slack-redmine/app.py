"""Slack app: create a Redmine ticket from a Slack message.

Flow:
1. User picks "Create Redmine ticket" from a message's shortcut menu (More actions).
2. A modal opens: Subject/Description prefilled from the message text,
   Tracker/Assignee dropdowns loaded from Redmine.
3. On submit, an issue is created in the Redmine project mapped from the channel,
   and a link to the ticket is posted back into the message's thread.

See docs/spec.md for the full specification.
"""

import json
import os
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

# Mapping: Slack channel ID -> Redmine project identifier.
# Each value is either the identifier itself, or an object like
# {"project": "project-a", "note": "#dev channel"} — "note" is a free-form
# human annotation (JSON has no comments) and is ignored by the app.
CHANNEL_PROJECT_MAP = {
    channel: v["project"] if isinstance(v, dict) else v
    for channel, v in _config["slack_channel_redmine_project_map"].items()
}
DEFAULT_PROJECT = _config.get("default_project")

app = App(token=os.environ["SLACK_BOT_TOKEN"])

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


def get_project(identifier):
    """Project info incl. name and trackers."""
    return _cached(
        f"project:{identifier}",
        lambda: redmine_get(f"/projects/{identifier}.json", {"include": "trackers"})["project"],
    )


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


# --- Slack handlers --------------------------------------------------------

@app.shortcut("create_redmine_ticket")
def open_modal(ack, shortcut, client):
    ack()
    msg = shortcut["message"].get("text", "")
    channel_id = shortcut["channel"]["id"]
    user_id = shortcut["user"]["id"]

    project_ident = CHANNEL_PROJECT_MAP.get(channel_id)
    mapped = project_ident is not None
    if not mapped:
        project_ident = DEFAULT_PROJECT
    if not project_ident:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"Channel này chưa được map tới project Redmine nào và chưa cấu hình "
                 f"default_project (channel ID: {channel_id}). Hãy thêm vào config.json.",
        )
        return

    project = get_project(project_ident)
    members = get_members(project_ident)

    # Default assignee: match the Slack user's email against Redmine users.
    initial_assignee_id = None
    email = slack_user_email(client, user_id)
    if email:
        ruser = find_redmine_user_by_email(email)
        if ruser and any(u["id"] == ruser["id"] for u in members):
            initial_assignee_id = ruser["id"]

    project_note = "" if mapped else " — _channel chưa map, dùng default project_"
    blocks = [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Project: *{project['name']}*{project_note}"}],
        },
    ]

    trackers = project.get("trackers", [])
    if trackers:
        tracker_options = [
            {"text": {"type": "plain_text", "text": t["name"]}, "value": str(t["id"])}
            for t in trackers
        ]
        blocks.append({
            "type": "input",
            "block_id": "tracker",
            "label": {"type": "plain_text", "text": "Tracker"},
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
            "label": {"type": "plain_text", "text": "Subject"},
            "element": {
                "type": "plain_text_input",
                "action_id": "v",
                "initial_value": msg.splitlines()[0][:100] if msg else "",
            },
        },
        {
            "type": "input",
            "block_id": "description",
            "label": {"type": "plain_text", "text": "Description"},
            "element": {
                "type": "plain_text_input",
                "action_id": "v",
                "multiline": True,
                "initial_value": msg,
            },
        },
    ]

    if members:
        # static_select is limited to 100 options
        assignee_options = [
            {"text": {"type": "plain_text", "text": u["name"]}, "value": str(u["id"])}
            for u in members[:100]
        ]
        assignee_element = {
            "type": "static_select",
            "action_id": "v",
            "options": assignee_options,
        }
        if initial_assignee_id is not None:
            assignee_element["initial_option"] = next(
                o for o in assignee_options if o["value"] == str(initial_assignee_id)
            )
        blocks.append({
            "type": "input",
            "block_id": "assignee",
            "optional": True,
            "label": {"type": "plain_text", "text": "Assignee"},
            "element": assignee_element,
        })

    client.views_open(
        trigger_id=shortcut["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "submit_redmine_ticket",
            "private_metadata": json.dumps({
                "channel": channel_id,
                "ts": shortcut["message"]["ts"],
                "project": project_ident,
                "project_name": project["name"],
                "mapped": mapped,
            }),
            "title": {"type": "plain_text", "text": "Create Redmine ticket"},
            "submit": {"type": "plain_text", "text": "Create"},
            "blocks": blocks,
        },
    )


@app.view("submit_redmine_ticket")
def handle_submit(ack, view, client, body):
    ack()
    meta = json.loads(view["private_metadata"])
    channel_id, msg_ts = meta["channel"], meta["ts"]
    user_id = body["user"]["id"]
    values = view["state"]["values"]

    subject = values["subject"]["v"]["value"]
    description = values["description"]["v"]["value"] or ""

    issue = {"project_id": meta["project"], "subject": subject}
    if "tracker" in values:
        selected = values["tracker"]["v"].get("selected_option")
        if selected:
            issue["tracker_id"] = int(selected["value"])
    if "assignee" in values:
        selected = values["assignee"]["v"].get("selected_option")
        if selected:
            issue["assigned_to_id"] = int(selected["value"])

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
        r.raise_for_status()
        created = r.json()["issue"]
        issue_id = created["id"]
        tracker_name = created.get("tracker", {}).get("name", "Ticket")

        # Same visual format as redmine-ticket-copy-markdown: [Tracker #ID: Subject](url)
        link = f"<{REDMINE_URL}/issues/{issue_id}|{tracker_name} #{issue_id}: {subject}>"
        text = f"Đã tạo Redmine ticket {link} trong project *{meta['project_name']}*"
        if not meta["mapped"]:
            text += " (channel này chưa được map project)"
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=msg_ts,
            text=text,
            unfurl_links=False,
        )
    except Exception as e:
        try:
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                thread_ts=msg_ts,
                text=f"⚠️ Tạo ticket Redmine thất bại: {e}",
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
