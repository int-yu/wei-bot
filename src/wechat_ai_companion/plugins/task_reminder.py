from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import date, datetime, time as time_type, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import utc_now_iso
from ..wechat_openclaw import WeChatInboundMessage
from .base import CompanionPlugin, PluginContext, PluginResult


WEEKDAY_ALIASES = {
    "周一": 1,
    "星期一": 1,
    "礼拜一": 1,
    "周1": 1,
    "星期1": 1,
    "周二": 2,
    "星期二": 2,
    "礼拜二": 2,
    "周2": 2,
    "星期2": 2,
    "周三": 3,
    "星期三": 3,
    "礼拜三": 3,
    "周3": 3,
    "星期3": 3,
    "周四": 4,
    "星期四": 4,
    "礼拜四": 4,
    "周4": 4,
    "星期4": 4,
    "周五": 5,
    "星期五": 5,
    "礼拜五": 5,
    "周5": 5,
    "星期5": 5,
    "周六": 6,
    "星期六": 6,
    "礼拜六": 6,
    "周6": 6,
    "星期6": 6,
    "周日": 7,
    "周天": 7,
    "星期日": 7,
    "星期天": 7,
    "礼拜日": 7,
    "礼拜天": 7,
    "周7": 7,
    "星期7": 7,
}

WEEKDAY_NAMES = {
    1: "周一",
    2: "周二",
    3: "周三",
    4: "周四",
    5: "周五",
    6: "周六",
    7: "周日",
}

TIME_RE = re.compile(
    r"(?:(?P<period>凌晨|早上|上午|中午|下午|晚上|今晚|明早|明晚)\s*)?"
    r"(?P<hour>\d{1,2})(?:(?:[:：](?P<minute_colon>\d{1,2}))|(?:点(?P<minute_point>\d{1,2})?分?))"
)
DATE_RE = re.compile(
    r"(?P<date>今天|明天|后天|(?:20\d{2})[/-]\d{1,2}[/-]\d{1,2}|(?:20\d{2})年\d{1,2}月\d{1,2}日?|\d{1,2}[/-]\d{1,2})"
)
WEEKDAY_RE = re.compile("|".join(sorted((re.escape(key) for key in WEEKDAY_ALIASES), key=len, reverse=True)))


class TaskReminderPlugin(CompanionPlugin):
    name = "task_reminder"
    description = "Stores tasks, reminders, and class schedules, then sends due reminders independently of proactive quotas."
    default_config = {
        "check_interval_seconds": 60,
        "timezone": "Asia/Shanghai",
        "allow_context_token_reuse": True,
        "schedule_remind_minutes": 15,
        "max_due_messages_per_check": 5,
    }
    config_schema = {
        "type": "object",
        "properties": {
            "check_interval_seconds": {
                "type": "integer",
                "label": "检查间隔秒",
                "default": 60,
                "minimum": 15,
                "maximum": 86400,
            },
            "timezone": {"type": "string", "label": "时区", "default": "Asia/Shanghai"},
            "allow_context_token_reuse": {"type": "boolean", "label": "允许复用 context_token 提醒", "default": True},
            "schedule_remind_minutes": {
                "type": "integer",
                "label": "课表提前提醒分钟",
                "default": 15,
                "minimum": 0,
                "maximum": 1440,
            },
            "max_due_messages_per_check": {
                "type": "integer",
                "label": "每次最多发送提醒数",
                "default": 5,
                "minimum": 1,
                "maximum": 100,
            },
        }
    }

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._last_tokens: dict[str, str] = {}

    async def on_start(self, context: PluginContext) -> list[PluginResult]:
        return [
            PluginResult(
                self.name,
                "started",
                "commands=/remind /todo /tasks /schedule add /done /cancel",
            )
        ]

    async def on_message_received(
        self,
        context: PluginContext,
        message: WeChatInboundMessage,
    ) -> list[PluginResult]:
        self._last_tokens[message.from_user_id] = message.context_token
        context.memory.set_plugin_state(self.name, message.from_user_id, "last_context_token", message.context_token)
        try:
            sent = await self._send_due_for_user(context, message.from_user_id, message.context_token)
        except Exception:
            logging.exception("[plugin:%s] due check on message failed for %s", self.name, message.from_user_id)
            return [PluginResult(self.name, "tracked_context_token", message.from_user_id)]
        results = [PluginResult(self.name, "tracked_context_token", message.from_user_id)]
        if sent:
            results.append(PluginResult(self.name, "sent_due_on_contact", f"{message.from_user_id} count={sent}"))
        return results

    async def handle_command(self, context: PluginContext, message: WeChatInboundMessage) -> str | None:
        text = message.text.strip()
        user_id = message.from_user_id
        now = self._now()

        if ("任务" in text or "提醒" in text) and any(
            trigger in text for trigger in ("查看", "列表", "我的", "有什么", "有哪些", "还有什么")
        ):
            return self._format_tasks(context, user_id)
        if "课表" in text and any(trigger in text for trigger in ("查看", "列表", "我的", "有什么", "有哪些")):
            return self._format_schedule(context, user_id)
        if text in {"/tasks", "/task list", "/todo list", "查看任务", "我的任务", "任务列表"}:
            return self._format_tasks(context, user_id)
        if text in {"/schedule", "/schedule list", "查看课表", "我的课表", "课表"}:
            return self._format_schedule(context, user_id)
        if text.startswith(("/done ", "/finish ")):
            item_id = text.split(maxsplit=1)[1].strip()
            return self._set_status(context, user_id, item_id, "done")
        if text.startswith(("/cancel ", "/delete ", "/del ")):
            item_id = text.split(maxsplit=1)[1].strip()
            return self._set_status(context, user_id, item_id, "cancelled")
        if text.startswith("/todo "):
            title = text[len("/todo ") :].strip()
            if not title:
                return "用法：/todo 任务内容"
            item = self._create_item(context, user_id, item_type="task", title=title)
            return f"已记录任务：{item['title']}\nID：{item['id']}"
        if text.startswith("/task "):
            body = text[len("/task ") :].strip()
            return self._create_task_from_text(context, user_id, body, now)
        if text.startswith("/remind "):
            body = text[len("/remind ") :].strip()
            return self._create_reminder_from_text(context, user_id, body, now)
        if text.startswith("/schedule add "):
            body = text[len("/schedule add ") :].strip()
            return self._create_schedule_from_text(context, user_id, body)

        schedule = _parse_schedule(text)
        if schedule and any(trigger in text for trigger in ("课表", "课程", "上课", "帮我记", "记一下")):
            item = self._create_item(context, user_id, item_type="schedule", **schedule)
            return _schedule_confirmation(item)

        if "提醒我" in text or "到点提醒" in text or "到时候提醒" in text:
            return self._create_reminder_from_text(context, user_id, text, now)

        remember_triggers = ("帮我记住", "帮我记一下", "帮我记个", "记一下")
        if any(trigger in text for trigger in remember_triggers):
            due_at, title = _parse_due_datetime(text, now)
            title = _strip_remember_triggers(title)
            if due_at:
                item = self._create_item(
                    context,
                    user_id,
                    item_type="reminder",
                    title=title or "未命名提醒",
                    due_at=due_at.isoformat(timespec="minutes"),
                )
                return _reminder_confirmation(item)
            item = self._create_item(context, user_id, item_type="task", title=title or text)
            return f"已记录任务：{item['title']}\nID：{item['id']}"

        return None

    async def background_loop(self, context: PluginContext, stop_event: asyncio.Event) -> None:
        interval = max(15, int(self.config.get("check_interval_seconds", 60)))
        while not stop_event.is_set():
            try:
                for wx_user_id in context.memory.list_known_users():
                    try:
                        await self._send_due_for_user(context, wx_user_id)
                    except Exception:
                        logging.exception("[plugin:%s] due reminder check failed for %s", self.name, wx_user_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[plugin:%s] background loop failed", self.name)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def _create_task_from_text(self, context: PluginContext, user_id: str, body: str, now: datetime) -> str:
        if not body:
            return "用法：/task 任务内容，或 /task 明天 18:00 任务内容"
        due_at, title = _parse_due_datetime(body, now)
        item_type = "reminder" if due_at else "task"
        item = self._create_item(
            context,
            user_id,
            item_type=item_type,
            title=title or body,
            due_at=due_at.isoformat(timespec="minutes") if due_at else None,
        )
        return _reminder_confirmation(item) if due_at else f"已记录任务：{item['title']}\nID：{item['id']}"

    def _create_reminder_from_text(self, context: PluginContext, user_id: str, body: str, now: datetime) -> str:
        if not body:
            return "用法：/remind 时间 内容，例如 /remind 明天 09:00 交作业"
        due_at, title = _parse_due_datetime(body, now)
        title = _strip_remind_triggers(title)
        if not due_at:
            return "我看到了提醒意图，但没有识别到明确时间。可以这样写：/remind 明天 09:00 交作业"
        item = self._create_item(
            context,
            user_id,
            item_type="reminder",
            title=title or "未命名提醒",
            due_at=due_at.isoformat(timespec="minutes"),
        )
        return _reminder_confirmation(item)

    def _create_schedule_from_text(self, context: PluginContext, user_id: str, body: str) -> str:
        schedule = _parse_schedule(body)
        if not schedule:
            return "用法：/schedule add 周一 08:00-09:40 课程名"
        item = self._create_item(context, user_id, item_type="schedule", **schedule)
        return _schedule_confirmation(item)

    def _create_item(self, context: PluginContext, user_id: str, *, item_type: str, title: str, **extra: Any) -> dict[str, Any]:
        item = {
            "id": _new_id(item_type),
            "type": item_type,
            "title": title.strip(),
            "status": "open",
            "created_at": utc_now_iso(),
        }
        item.update({key: value for key, value in extra.items() if value is not None})
        self._save_item(context, user_id, item)
        logging.info("[plugin:%s] saved_item user=%s type=%s id=%s", self.name, user_id, item_type, item["id"])
        return item

    async def _send_due_for_user(self, context: PluginContext, user_id: str, context_token: str | None = None) -> int:
        if not bool(self.config.get("allow_context_token_reuse", True)):
            return 0
        token = context_token or self._last_tokens.get(user_id) or context.memory.get_plugin_state(
            self.name, user_id, "last_context_token"
        )
        if not token:
            return 0

        now = self._now()
        due_items = self._due_items(context, user_id, now)
        if not due_items:
            return 0
        max_count = max(1, int(self.config.get("max_due_messages_per_check", 5)))
        sent = 0
        for item in due_items[:max_count]:
            text = self._reminder_message(item)
            await context.wechat.send_text(user_id, token, text)
            context.memory.add_message(user_id, "assistant", f"[reminder] {text}")
            self._mark_reminded(context, user_id, item, now)
            sent += 1
            logging.info("[plugin:%s] sent_reminder user=%s id=%s", self.name, user_id, item["id"])
        return sent

    def _due_items(self, context: PluginContext, user_id: str, now: datetime) -> list[dict[str, Any]]:
        items = [item for item in self._load_items(context, user_id) if item.get("status") == "open"]
        due: list[dict[str, Any]] = []
        today = now.date().isoformat()
        for item in items:
            if item.get("type") in {"task", "reminder"}:
                due_at_raw = item.get("due_at")
                if not due_at_raw or item.get("reminded_at"):
                    continue
                try:
                    due_at = datetime.fromisoformat(str(due_at_raw))
                except ValueError:
                    continue
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=now.tzinfo)
                if due_at <= now:
                    due.append(item)
            elif item.get("type") == "schedule":
                if item.get("last_reminded_date") == today:
                    continue
                if int(item.get("weekday", 0)) != now.isoweekday():
                    continue
                start = _parse_plain_time(str(item.get("start_time", "")))
                if not start:
                    continue
                start_at = datetime.combine(now.date(), start, tzinfo=now.tzinfo)
                lead = timedelta(minutes=int(self.config.get("schedule_remind_minutes", 15)))
                if start_at - lead <= now <= start_at + timedelta(minutes=10):
                    due.append(item)
        return due

    def _mark_reminded(self, context: PluginContext, user_id: str, item: dict[str, Any], now: datetime) -> None:
        if item.get("type") == "schedule":
            item["last_reminded_date"] = now.date().isoformat()
        else:
            item["reminded_at"] = now.isoformat(timespec="minutes")
        self._save_item(context, user_id, item)

    def _reminder_message(self, item: dict[str, Any]) -> str:
        if item.get("type") == "schedule":
            return (
                f"课表提醒：{item.get('title', '未命名课程')}\n"
                f"时间：{WEEKDAY_NAMES.get(int(item.get('weekday', 0)), '未知')} "
                f"{item.get('start_time', '')}-{item.get('end_time', '')}"
            )
        due_at = item.get("due_at")
        suffix = f"\n时间：{_display_dt(str(due_at))}" if due_at else ""
        return f"提醒：{item.get('title', '未命名提醒')}{suffix}"

    def _format_tasks(self, context: PluginContext, user_id: str) -> str:
        items = [
            item
            for item in self._load_items(context, user_id)
            if item.get("type") in {"task", "reminder"} and item.get("status") == "open"
        ]
        if not items:
            return "暂无未完成任务或提醒。"
        lines = ["未完成任务 / 提醒："]
        for item in sorted(items, key=lambda value: str(value.get("due_at") or "9999")):
            due = f" | {_display_dt(str(item['due_at']))}" if item.get("due_at") else ""
            reminded = " | 已提醒" if item.get("reminded_at") else ""
            lines.append(f"- {item['id']} | {item['title']}{due}{reminded}")
        return "\n".join(lines)

    def _format_schedule(self, context: PluginContext, user_id: str) -> str:
        items = [
            item
            for item in self._load_items(context, user_id)
            if item.get("type") == "schedule" and item.get("status") == "open"
        ]
        if not items:
            return "暂无课表。"
        lines = ["课表："]
        for item in sorted(items, key=lambda value: (int(value.get("weekday", 9)), str(value.get("start_time", "")))):
            lines.append(
                f"- {item['id']} | {WEEKDAY_NAMES.get(int(item.get('weekday', 0)), '未知')} "
                f"{item.get('start_time', '')}-{item.get('end_time', '')} | {item.get('title', '')}"
            )
        return "\n".join(lines)

    def _set_status(self, context: PluginContext, user_id: str, item_id: str, status: str) -> str:
        item = self._find_item(context, user_id, item_id)
        if not item:
            return f"没有找到 ID 为 {item_id} 的任务、提醒或课表。"
        item["status"] = status
        item["updated_at"] = utc_now_iso()
        self._save_item(context, user_id, item)
        action = "完成" if status == "done" else "取消"
        return f"已{action}：{item.get('title', item['id'])}"

    def _find_item(self, context: PluginContext, user_id: str, item_id: str) -> dict[str, Any] | None:
        needle = item_id.strip()
        for item in self._load_items(context, user_id):
            if str(item.get("id", "")).startswith(needle):
                return item
        return None

    def _load_items(self, context: PluginContext, user_id: str) -> list[dict[str, Any]]:
        rows = context.memory.list_plugin_state(self.name)
        items: list[dict[str, Any]] = []
        for row in rows:
            if row["wx_user_id"] != user_id or not str(row["state_key"]).startswith("item:"):
                continue
            try:
                item = json.loads(row["state_value"])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                items.append(item)
        return items

    def _save_item(self, context: PluginContext, user_id: str, item: dict[str, Any]) -> None:
        context.memory.set_plugin_state(
            self.name,
            user_id,
            f"item:{item['id']}",
            json.dumps(item, ensure_ascii=False),
        )

    def _now(self) -> datetime:
        timezone_name = str(self.config.get("timezone", "Asia/Shanghai"))
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            tz = None
        return datetime.now(tz)


def _parse_due_datetime(text: str, now: datetime) -> tuple[datetime | None, str]:
    date_match = DATE_RE.search(text)
    if date_match:
        time_match = TIME_RE.search(text, date_match.end())
        if time_match:
            target_date = _parse_date(date_match.group("date"), now.date())
            target_time = _time_from_match(time_match)
            if target_date and target_time:
                due_at = datetime.combine(target_date, target_time, tzinfo=now.tzinfo)
                title = _remove_spans(text, [(date_match.start(), time_match.end())])
                return due_at, _clean_title(title)

    time_match = TIME_RE.search(text)
    if not time_match:
        return None, _clean_title(text)

    target_time = _time_from_match(time_match)
    if not target_time:
        return None, _clean_title(text)
    day_offset = 1 if time_match.group("period") in {"明早", "明晚"} else 0
    target_date = now.date() + timedelta(days=day_offset)
    due_at = datetime.combine(target_date, target_time, tzinfo=now.tzinfo)
    if day_offset == 0 and due_at <= now:
        due_at += timedelta(days=1)
    title = _remove_spans(text, [(time_match.start(), time_match.end())])
    return due_at, _clean_title(title)


def _parse_schedule(text: str) -> dict[str, Any] | None:
    weekday_match = WEEKDAY_RE.search(text)
    if not weekday_match:
        return None
    time_matches = list(TIME_RE.finditer(text))
    if len(time_matches) < 2:
        return None
    between = text[time_matches[0].end() : time_matches[1].start()]
    if not re.search(r"[-~—到至]", between):
        return None
    start_time = _time_from_match(time_matches[0])
    end_time = _time_from_match(time_matches[1])
    if not start_time or not end_time:
        return None
    title = _remove_spans(
        text,
        [
            (weekday_match.start(), weekday_match.end()),
            (time_matches[0].start(), time_matches[1].end()),
        ],
    )
    title = _clean_title(title)
    title = re.sub(r"^(课表|课程|上课|帮我记住|帮我记一下|记一下|[:：,，。；;]\s*)+", "", title).strip()
    return {
        "title": title or "未命名课程",
        "weekday": WEEKDAY_ALIASES[weekday_match.group(0)],
        "start_time": start_time.strftime("%H:%M"),
        "end_time": end_time.strftime("%H:%M"),
    }


def _parse_date(value: str, today: date) -> date | None:
    if value == "今天":
        return today
    if value == "明天":
        return today + timedelta(days=1)
    if value == "后天":
        return today + timedelta(days=2)
    normalized = value.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
    parts = [part for part in normalized.split("-") if part]
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            candidate = date(today.year, int(parts[0]), int(parts[1]))
            if candidate < today:
                candidate = date(today.year + 1, int(parts[0]), int(parts[1]))
            return candidate
    except ValueError:
        return None
    return None


def _time_from_match(match: re.Match[str]) -> time_type | None:
    try:
        hour = int(match.group("hour"))
        minute = int(match.group("minute_colon") or match.group("minute_point") or 0)
    except (TypeError, ValueError):
        return None
    period = match.group("period") or ""
    if period in {"下午", "晚上", "今晚", "明晚"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return time_type(hour=hour, minute=minute)


def _parse_plain_time(value: str) -> time_type | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    try:
        return time_type(hour=int(match.group(1)), minute=int(match.group(2)))
    except ValueError:
        return None


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    result = text
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + " " + result[end:]
    return result


def _clean_title(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^[/\w]+\s+", "", cleaned)
    cleaned = re.sub(r"^(请|麻烦|帮我|替我|给我)\s*", "", cleaned)
    cleaned = cleaned.strip(" ：:，,。.;；")
    return re.sub(r"\s+", " ", cleaned).strip()


def _strip_remind_triggers(text: str) -> str:
    cleaned = text
    for trigger in ("提醒我", "到点提醒我", "到时候提醒我", "帮我提醒", "请提醒我"):
        cleaned = cleaned.replace(trigger, "")
    return _clean_title(cleaned)


def _strip_remember_triggers(text: str) -> str:
    cleaned = text
    for trigger in ("帮我记住", "帮我记一下", "帮我记个", "记一下"):
        cleaned = cleaned.replace(trigger, "")
    return _clean_title(cleaned)


def _new_id(item_type: str) -> str:
    prefix = {"task": "T", "reminder": "R", "schedule": "S"}.get(item_type, "I")
    return f"{prefix}{datetime.now().strftime('%m%d%H%M')}{uuid.uuid4().hex[:4]}"


def _display_dt(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%d %H:%M")


def _reminder_confirmation(item: dict[str, Any]) -> str:
    return f"已记录提醒：{item['title']}\n时间：{_display_dt(str(item['due_at']))}\nID：{item['id']}"


def _schedule_confirmation(item: dict[str, Any]) -> str:
    return (
        f"已记录课表：{item['title']}\n"
        f"时间：{WEEKDAY_NAMES.get(int(item['weekday']), '未知')} {item['start_time']}-{item['end_time']}\n"
        f"ID：{item['id']}"
    )
