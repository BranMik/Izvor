# Slack App Setup & Configuration

This guide provides instructions for configuring and installing the **Izvor** Slack Application in your Slack workspace.

---

## 🛠️ Creating the Slack App

1. Go to the [Slack API App Console](https://api.slack.com/apps).
2. Click **Create New App**.
3. Choose **From an app manifest** (recommended) or **From scratch**.

---

## 📄 App Manifest Configuration (Recommended)

You can paste the following YAML/JSON manifest directly into the Slack App creator:

```json
{
  "display_information": {
    "name": "Izvor",
    "description": "Multi-agent asynchronous deliberation platform",
    "background_color": "#1e293b"
  },
  "features": {
    "bot_user": {
      "display_name": "Izvor",
      "always_online": true
    },
    "app_home": {
      "home_tab_enabled": true,
      "messages_tab_enabled": true,
      "messages_tab_read_only_enabled": false
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": [
        "app_mentions:read",
        "channels:history",
        "channels:manage",
        "channels:read",
        "chat:write",
        "chat:write.customize",
        "files:read",
        "files:write",
        "groups:history",
        "groups:read",
        "im:history",
        "im:write",
        "mpim:history",
        "pins:write"
      ]
    }
  },
  "settings": {
    "event_subscriptions": {
      "request_url": "https://<your-domain>/slack/events",
      "bot_events": [
        "app_home_opened",
        "app_mention",
        "message.channels",
        "message.groups",
        "message.im",
        "message.mpim"
      ]
    },
    "interactivity": {
      "is_enabled": true,
      "request_url": "https://<your-domain>/slack/interactivity"
    },
    "org_deploy_enabled": false,
    "socket_mode_enabled": false,
    "token_rotation_enabled": false
  }
}
```

> [!NOTE]
> Replace `https://<your-domain>` with your public tunnel or deployment domain (e.g. `https://abcdef123.ngrok-free.app` or `https://izvor.yourdomain.com`).

---

## 🔐 Required OAuth Scopes

If setting up scopes manually under **OAuth & Permissions**:

| Bot Token Scope | Purpose |
|---|---|
| `app_mentions:read` | Allows Izvor to detect when the bot is directly mentioned (`@Izvor`) to initiate debates. |
| `channels:history` | Reads thread context and discussion history in public channels. |
| `channels:manage` | Manages channels for dedicated discussion threads or exports. |
| `channels:read` | Inspects channel metadata and membership. |
| `groups:history` | Reads thread context and discussion history in private channels. |
| `groups:read` | Inspects private channel metadata. |
| `im:history` | Reads direct message conversations with the bot. |
| `im:write` | Initiates direct message conversations (e.g., admin notifications, approval alerts). |
| `mpim:history` | Reads group direct messages. |
| `chat:write` | Posts replies, warnings, summaries, and debate turns. |
| `chat:write.customize` | Enables posting messages under individual persona names and custom avatars (e.g., *Visionary*, *Tactician*, *Skeptic*). |
| `files:read` | Inspects and processes attached files (PDFs, Markdown, text) using the document reader skill. |
| `files:write` | Uploads generated flat discussion exports and reports directly to Slack threads. |
| `pins:write` | Pins live synthesis notes and conclusion notes to discussion threads. |

---

## 📡 Event Subscriptions & Interactivity URLs

### 1. Events API
- Navigate to **Event Subscriptions** in the left sidebar.
- Toggle **Enable Events** to **On**.
- In **Request URL**, enter:
  ```
  https://<your-domain>/slack/events
  ```
- Slack will immediately send an HTTP POST with a `challenge` parameter. Izvor's [`adapters/slack/events.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/adapters/slack/events.py) automatically validates the signature and responds with the challenge value.
- Under **Subscribe to bot events**, ensure the following events are added:
  - `app_home_opened`
  - `app_mention`
  - `message.channels`
  - `message.groups`
  - `message.im`
  - `message.mpim`
- Click **Save Changes**.

### 2. Interactivity & Shortcuts
- Navigate to **Interactivity & Shortcuts** in the left sidebar.
- Toggle **Interactivity** to **On**.
- In **Request URL**, enter:
  ```
  https://<your-domain>/slack/interactivity
  ```
- This URL handles App Home configuration modal submissions, persona toggles, and dropdown interactions ([`adapters/slack/interactivity.py`](file:///c:/Users/Hp/Documents/agy2-projects/Izvor/adapters/slack/interactivity.py)).
- Click **Save Changes**.

---

## 🏠 App Home Configuration

1. In the left sidebar, navigate to **App Home**.
2. Under **Show Tabs**:
   - Check **Home Tab** (required for the interactive configuration dashboard).
   - Check **Messages Tab** and allow users to send messages from the tab.
3. Under **Show your bot's presence**:
   - Ensure bot user is always shown as online.

---

## 📥 Installing the App to Your Workspace

1. In the left sidebar, click **Install App**.
2. Click **Install to Workspace** and authorize the requested permissions.
3. Copy the **Bot User OAuth Token** (starts with `xoxb-...`). Set this as `SLACK_BOT_TOKEN` in your `.env`.
4. Go to **Basic Information**, scroll to **App Credentials**, and copy the **Signing Secret**. Set this as `SLACK_SIGNING_SECRET` in your `.env`.

Restart the Izvor application. You can now invite `@Izvor` to any Slack channel or send a direct message to start deliberating!
