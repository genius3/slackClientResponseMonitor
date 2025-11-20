"""
Slack response-time monitor.

Features
- Poll configured client channels during business hours (default 08:00-17:00 in configured timezone).
- Identify last client message and whether a team member has replied.
- Trigger reminders after configurable thresholds (4h during business hours, 24h overall).
- Log response times for visibility.

Prerequisites
- Python 3.10+
- Install deps: pip install slack_sdk pyyaml
- Env var SLACK_BOT_TOKEN must be set (bot token with channels:history, chat:write, conversations:read, users:read scopes).
- Optional env var CONFIG_PATH to point to a YAML config; defaults to config.yaml.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


@dataclasses.dataclass
class BusinessHours:
    start: dtime = dtime.fromisoformat("08:00")
    end: dtime = dtime.fromisoformat("17:00")
    timezone: str = "UTC"
    weekdays_only: bool = True

    def is_open(self, moment: datetime) -> bool:
        local_now = moment.astimezone(ZoneInfo(self.timezone))
        if self.weekdays_only and local_now.weekday() >= 5:
            return False
        return self.start <= local_now.time() <= self.end


@dataclasses.dataclass
class MonitorConfig:
    channel_owners: Dict[str, str]
    team_member_ids: List[str]
    business_hours: BusinessHours = BusinessHours()
    business_reply_hours: int = 4
    overall_reply_hours: int = 24
    check_interval_minutes: int = 10
    log_path: Path = Path("logs/response_report.csv")
    trail_log_path: Path = Path("logs/trail.log")
    reminder_text_template: str = (
        "Reminder: Client message in <#{}> has been waiting {} hours for a reply. "
        "Please respond."
    )

    @classmethod
    def from_yaml(cls, path: Path) -> "MonitorConfig":
        with path.open("r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp) or {}
        business_data = data.get("business_hours", {})
        business = BusinessHours(
            start=dtime.fromisoformat(business_data.get("start", "08:00")),
            end=dtime.fromisoformat(business_data.get("end", "17:00")),
            timezone=business_data.get("timezone", "UTC"),
            weekdays_only=business_data.get("weekdays_only", True),
        )
        log_path = Path(data.get("log_path", "logs/response_report.csv"))
        return cls(
            channel_owners=data.get("channel_owners", {}),
            team_member_ids=data.get("team_member_ids", []),
            business_hours=business,
            business_reply_hours=int(data.get("business_reply_hours", 4)),
            overall_reply_hours=int(data.get("overall_reply_hours", 24)),
            check_interval_minutes=int(data.get("check_interval_minutes", 10)),
            log_path=log_path,
            trail_log_path=Path(data.get("trail_log_path", "logs/trail.log")),
            reminder_text_template=data.get("reminder_text_template")
            or cls.reminder_text_template,
        )


class SlackMonitor:
    def __init__(self, config: MonitorConfig, client: WebClient) -> None:
        self.config = config
        self.client = client
        self._ensure_log_header()

    def _ensure_log_header(self) -> None:
        self._ensure_file(
            self.config.log_path,
            "channel_name,conversation_id,client_ts,team_reply_ts,hours_to_reply,status\n",
        )
        self._ensure_file(
            self.config.trail_log_path,
            "channel_name,conversation_id,client_ts,client_text,team_reply_ts,team_reply_text,"
            "hours_between,status\n",
        )

    def _ensure_file(self, path: Path, header: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(header, encoding="utf-8")

    def run_forever(self) -> None:
        interval = max(self.config.check_interval_minutes, 1) * 60
        while True:
            try:
                self.check_channels()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Monitor iteration failed: %s", exc)
            time.sleep(interval)

    def check_channels(self) -> None:
        now = datetime.now(tz=ZoneInfo(self.config.business_hours.timezone))
        channels = self._fetch_monitored_channels()
        for channel in channels:
            channel_id = channel["id"]
            channel_name = channel["name"]
            owner_id = channel.get("owner_id")
            logging.info("Checking channel %s (%s)", channel_name, channel_id)
            messages = self._fetch_messages(channel_id, channel_name)
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                self._debug_log_messages(channel_id, messages)
            record = self._evaluate_channel(messages)
            if not record:
                logging.info("No client messages found for %s, %s", channel_id, channel_name)
                continue
            status = self._decide_status(record, now)
            self._log_record(channel_name, channel_id, record, status)
            self._log_trail(channel_name, channel_id, record, status)
            if status == "remind" and owner_id:
                self._send_reminder(channel_id, owner_id, record, now)
            elif status == "remind" and not owner_id:
                logging.warning(
                    "Channel %s (%s) exceeded threshold but no owner configured; skipping reminder",
                    channel_name,
                    channel_id,
                )

    def _fetch_messages(self, channel_id: str, channel_name: str, limit: int = 200) -> List[dict]:
        try:
            resp = self.client.conversations_history(channel=channel_id, limit=limit)
            messages = resp.get("messages", [])
            logging.debug("Fetched %s messages for %s", len(messages), channel_name)
            return messages
        except SlackApiError as exc:
            logging.exception("Failed to fetch messages for %s: %s", channel_id, exc)
            return []

    def _fetch_monitored_channels(self) -> List[dict]:
        channels: List[dict] = []
        cursor: Optional[str] = None
        types = "public_channel,private_channel"
        while True:
            try:
                resp = self.client.conversations_list(types=types, cursor=cursor, limit=200)
            except SlackApiError as exc:
                logging.exception("Failed to list channels: %s", exc)
                break

            for channel in resp.get("channels", []):
                if not channel.get("is_member"):
                    continue
                owner_id = self._resolve_owner(channel)
                channel_name = channel.get("name") or channel.get("name_normalized") or channel.get("id")
                channels.append({"id": channel["id"], "name": channel_name, "owner_id": owner_id})

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        logging.debug("Monitoring %s channels", len(channels))
        return channels

    def _resolve_owner(self, channel: dict) -> Optional[str]:
        channel_id = channel.get("id")
        channel_name = channel.get("name") or channel.get("name_normalized")
        if channel_id in self.config.channel_owners:
            return self.config.channel_owners[channel_id]
        if channel_name and channel_name in self.config.channel_owners:
            return self.config.channel_owners[channel_name]
        return None

    def _debug_log_messages(self, channel_id: str, messages: List[dict]) -> None:
        for msg in messages:
            user = msg.get("user") or msg.get("bot_id", "unknown")
            subtype = msg.get("subtype", "standard")
            ts = msg.get("ts")
            text = (msg.get("text") or "").replace("\n", " ")
            snippet = text[:80] + ("..." if len(text) > 80 else "")
            logging.debug(
                "Channel %s message ts=%s user=%s subtype=%s text=%s, msg=%s",
                channel_id,
                ts,
                user,
                subtype,
                snippet,
                msg
            )

    def _evaluate_channel(self, messages: List[dict]) -> Optional[dict]:
        if not messages:
            return None
        sorted_messages = sorted(messages, key=lambda m: float(m.get("ts", 0.0)))
        last_client_msg = None
        last_team_reply = None

        for msg in sorted_messages:
            if not self._is_valid_message(msg):
                continue
            if self._is_client_message(msg):
                last_client_msg = msg
                last_team_reply = None
            elif last_client_msg:
                last_team_reply = msg

        if not last_client_msg:
            return None
        return {
            "client_ts": float(last_client_msg["ts"]),
            "client_text": last_client_msg.get("text", ""),
            "team_reply_ts": float(last_team_reply["ts"]) if last_team_reply else None,
            "team_reply_text": last_team_reply.get("text", "") if last_team_reply else None,
        }

    def _is_valid_message(self, msg: dict) -> bool:
        if msg.get("subtype") in {"channel_join", "bot_message", "channel_topic", "channel_purpose"}:
            return False
        return "user" in msg or "bot_id" in msg

    def _is_client_message(self, msg: dict) -> bool:
        user_id = msg.get("user")
        if not user_id:
            return False
        return user_id not in self.config.team_member_ids

    def _decide_status(self, record: dict, now: datetime) -> str:
        client_dt = datetime.fromtimestamp(record["client_ts"], tz=ZoneInfo(self.config.business_hours.timezone))
        reply_dt = (
            datetime.fromtimestamp(record["team_reply_ts"], tz=ZoneInfo(self.config.business_hours.timezone))
            if record.get("team_reply_ts")
            else None
        )
        if reply_dt and reply_dt > client_dt:
            return "answered"

        hours_since_client = (now - client_dt).total_seconds() / 3600
        within_business = self.config.business_hours.is_open(now)
        threshold = (
            self.config.business_reply_hours if within_business else self.config.overall_reply_hours
        )
        return "remind" if hours_since_client >= threshold else "waiting"

    def _send_reminder(self, channel_id: str, owner_id: str, record: dict, now: datetime) -> None:
        hours_since = round((now.timestamp() - record["client_ts"]) / 3600, 1)
        text = self.config.reminder_text_template.format(channel_id, hours_since)
        try:
            dm_resp = self.client.conversations_open(users=owner_id)
            dm_channel = dm_resp["channel"]["id"]
            self.client.chat_postMessage(channel=dm_channel, text=text)
            logging.info("Reminder sent to %s for channel %s", owner_id, channel_id)
        except SlackApiError as exc:
            logging.exception("Failed to send reminder for %s: %s", channel_id, exc)

    def _log_record(self, channel_name: str, channel_id: str, record: dict, status: str) -> None:
        client_ts = datetime.fromtimestamp(record["client_ts"], tz=ZoneInfo(self.config.business_hours.timezone))
        reply_ts = (
            datetime.fromtimestamp(record["team_reply_ts"], tz=ZoneInfo(self.config.business_hours.timezone))
            if record.get("team_reply_ts")
            else None
        )
        hours_to_reply = (
            (reply_ts - client_ts).total_seconds() / 3600 if reply_ts else None
        )
        line = (
            f"{channel_name},{channel_id},{client_ts.isoformat()},"
            f"{reply_ts.isoformat() if reply_ts else ''},{hours_to_reply or ''},{status}\n"
        )
        with self.config.log_path.open("a", encoding="utf-8") as fp:
            fp.write(line)

    def _log_trail(self, channel_name: str, channel_id: str, record: dict, status: str) -> None:
        client_ts = datetime.fromtimestamp(record["client_ts"], tz=ZoneInfo(self.config.business_hours.timezone))
        reply_ts = (
            datetime.fromtimestamp(record["team_reply_ts"], tz=ZoneInfo(self.config.business_hours.timezone))
            if record.get("team_reply_ts")
            else None
        )
        hours_between = (
            (reply_ts - client_ts).total_seconds() / 3600 if reply_ts else None
        )
        client_text = _csv_escape(record.get("client_text", ""))
        reply_raw = record.get("team_reply_text") or ""
        reply_text = _csv_escape(reply_raw) if reply_raw else ""
        reply_ts_str = reply_ts.isoformat() if reply_ts else ""
        line = (
            f"{channel_name},{channel_id},{client_ts.isoformat()},{client_text},"
            f"{reply_ts_str},{reply_text},{hours_between or ''},{status}\n"
        )
        with self.config.trail_log_path.open("a", encoding="utf-8") as fp:
            fp.write(line)


def _csv_escape(text: str) -> str:
    sanitized = (text or "").replace("\n", " ").replace("\r", " ")
    sanitized = sanitized.replace("\"", "\"\"")
    return f'"{sanitized}"'


def load_config() -> MonitorConfig:
    config_path = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file {config_path} not found. Copy config.example.yaml and adjust settings."
        )
    return MonitorConfig.from_yaml(config_path)


def build_client() -> WebClient:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise EnvironmentError("SLACK_BOT_TOKEN env var is required.")
    return WebClient(token=token)


def configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Slack response-time monitor")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging (includes raw Slack message summaries)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.debug)
    config = load_config()
    client = build_client()
    monitor = SlackMonitor(config, client)
    monitor.run_forever()


if __name__ == "__main__":
    main()
