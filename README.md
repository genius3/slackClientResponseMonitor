# Slack Response-Time Monitor

A simple Slack bot script that checks client channels for unanswered messages and DM-reminds the responsible owner when a reply is overdue (default 4h during business hours, 24h overall). It also logs response times for visibility.

## Prerequisites
- Python 3.10+
- Slack Bot/User with token (`SLACK_BOT_TOKEN`) that has these scopes:
  - `channels:history` (or `conversations.history`)
  - `conversations:read`
  - `chat:write`
  - `users:read`
- Ability to create a bot app and install it to your workspace.

## Setup
1) Install dependencies
   ```bash
   pip install slack_sdk pyyaml
   ```

2) Create configuration
   - Copy `config.example.yaml` to `config.yaml`:
     ```bash
     cp config.example.yaml config.yaml
     ```
   - Edit `config.yaml`:
     - `channel_owners`: map each channel ID **or channel name** to the responsible team member’s user ID. Channels without an owner are still logged but reminders are skipped.
     - `team_member_ids`: list all internal team user IDs (used to distinguish client vs team messages).
     - `business_hours`: adjust start/end/timezone/weekdays.
     - `business_reply_hours` and `overall_reply_hours`: thresholds in hours.
     - `check_interval_minutes`: how often to poll.
     - `log_path`: CSV path for response-time history summary.
     - `trail_log_path`: CSV/LOG path capturing latest client message + reply text and timing for auditing.
     - `reminder_text_template`: tweak reminder wording.

3) Export environment variables
   ```bash
   export SLACK_BOT_TOKEN=xoxb-...your-bot-token...
   # Optional: point to a different config file
   export CONFIG_PATH=/absolute/path/to/your_config.yaml
   ```

## Running locally
```bash
python bot.py            # normal info-level logging
python bot.py --debug    # verbose logging that lists every fetched message
```
The script runs indefinitely, polling every `check_interval_minutes` and sending DMs to channel owners when thresholds are exceeded. Debug mode is helpful when you want to verify the raw messages being processed for a channel.

## What it does
- Fetches every Slack channel the bot is a member of and lines it up with `channel_owners` (matching by channel ID first, then by name).
- Finds the most recent client message and whether a team member has replied.
- During business hours uses `business_reply_hours`; otherwise uses `overall_reply_hours`.
- Sends a DM reminder to the channel owner if the limit is exceeded.
- Appends a row to `logs/response_report.csv` with `channel_name`, `conversation_id`, client/reply timestamps, and status (answered, waiting, remind).
- Appends a row to `logs/trail.log` with the actual client/reply message text (sanitized), timestamps, and hours between messages for detailed auditing.

## Notes & tips
- Ensure the bot is a member of each channel you want to monitor.
- Channel and user IDs can be found with Slack’s UI (`Channel details > About > Channel ID`) or via the API.
- If you prefer to configure by channel name, make sure names stay unique; IDs are safer because renaming a channel won’t break the mapping.
- The bot writes to two log files by default under `logs/`: `response_report.csv` (summary) and `trail.log` (message text + timing). Rotate/ship them to your logging platform if needed.
- To change timezone or hours, edit `business_hours` in `config.yaml`; uses IANA tz names (e.g., `America/New_York`).
- If you prefer reminders in-channel threads instead of DM, adjust `_send_reminder` in `bot.py`.
- For testing without live Slack, you can mock the Slack client in a small harness to feed fake conversations into `SlackMonitor`.
