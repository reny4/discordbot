import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("scheduler-bot")

DEFAULT_STATE: dict[str, Any] = {
    "project_status": {
        "last_meeting": None,
        "overall_momentum": "idle",
    },
    "tasks": [],
    "schedules": {
        "pending_proposal": None,
        "confirmed_events": [],
    },
    "preferences": {
        "avoid_weekends": True,
        "best_hour": 21,
    },
}


@dataclass
class BotConfig:
    discord_token: str
    openai_api_key: str
    db_channel_id: int
    summary_channel_id: int
    schedule_channel_id: int
    timezone: str = "Asia/Tokyo"
    model: str = "gpt-4o-mini"

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            openai_api_key=os.environ["OPENAI_API_KEY"],
            db_channel_id=int(os.environ["DB_CHANNEL_ID"]),
            summary_channel_id=int(os.environ["SUMMARY_CHANNEL_ID"]),
            schedule_channel_id=int(os.environ["SCHEDULE_CHANNEL_ID"]),
            timezone=os.getenv("TIMEZONE", "Asia/Tokyo"),
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )


class DiscordJsonStore:
    """Stores one JSON document in a dedicated Discord channel message."""

    def __init__(self, channel_id: int):
        self.channel_id = channel_id

    async def load_or_init(self, bot: commands.Bot) -> tuple[dict[str, Any], discord.Message]:
        channel = bot.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("DB channel not found or not a text channel")

        async for message in channel.history(limit=50, oldest_first=True):
            try:
                payload = json.loads(message.content)
                return payload, message
            except json.JSONDecodeError:
                continue

        created = await channel.send(json.dumps(DEFAULT_STATE, ensure_ascii=False, indent=2))
        return json.loads(created.content), created

    async def save(self, message: discord.Message, payload: dict[str, Any]) -> None:
        await message.edit(content=json.dumps(payload, ensure_ascii=False, indent=2))


class AiBrain:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = OpenAI(api_key=api_key)

    def suggest_next_date(self, state: dict[str, Any], now_iso: str) -> dict[str, str]:
        prompt = {
            "role": "system",
            "content": (
                "あなたはプロジェクトスケジューラーです。"
                "与えられた状態を分析し、次回活動候補日と理由をJSONで出力してください。"
                "出力形式: {\"suggested_date\": \"YYYY-MM-DD HH:mm\", \"reason\": \"...\"}"
            ),
        }
        user = {
            "role": "user",
            "content": json.dumps({"state": state, "now": now_iso}, ensure_ascii=False),
        }
        res = self.client.responses.create(model=self.model, input=[prompt, user])
        text = res.output_text.strip()
        return json.loads(text)

    def summarize_context(self, state: dict[str, Any]) -> str:
        prompt = {
            "role": "system",
            "content": (
                "あなたはプロジェクト再開支援のサマライザーです。"
                "未完了タスクと最新状態から、今日やるべきことを短く箇条書きで提案してください。"
            ),
        }
        user = {
            "role": "user",
            "content": json.dumps(state, ensure_ascii=False),
        }
        res = self.client.responses.create(model=self.model, input=[prompt, user])
        return res.output_text.strip()


intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="/", intents=intents)
CONFIG = BotConfig.from_env()
STORE = DiscordJsonStore(CONFIG.db_channel_id)
BRAIN = AiBrain(CONFIG.openai_api_key, CONFIG.model)

state_message: discord.Message | None = None
state_cache: dict[str, Any] = {}


@bot.event
async def on_ready() -> None:
    global state_message, state_cache
    state_cache, state_message = await STORE.load_or_init(bot)
    LOGGER.info("Logged in as %s", bot.user)
    if not daily_scheduler.is_running():
        daily_scheduler.start()


def _next_task_id(tasks_data: list[dict[str, Any]]) -> str:
    if not tasks_data:
        return "T001"
    num = max(int(t["id"][1:]) for t in tasks_data if t.get("id", "").startswith("T"))
    return f"T{num + 1:03d}"


async def persist_state() -> None:
    if state_message is None:
        raise RuntimeError("State message is not initialized")
    await STORE.save(state_message, state_cache)


@bot.command(name="add_task")
async def add_task(ctx: commands.Context, *, content: str) -> None:
    task_id = _next_task_id(state_cache["tasks"])
    state_cache["tasks"].append(
        {
            "id": task_id,
            "content": content,
            "status": "todo",
            "progress": 0,
            "tags": ["未分類"],
            "assignee": str(ctx.author),
        }
    )
    await persist_state()
    await ctx.send(f"タスクを追加しました: {task_id}")


@bot.command(name="update_task")
async def update_task(ctx: commands.Context, task_id: str, status: str, progress: int) -> None:
    for task in state_cache["tasks"]:
        if task["id"] == task_id:
            task["status"] = status
            task["progress"] = max(0, min(100, progress))
            await persist_state()
            await ctx.send(f"更新しました: {task_id}")
            return
    await ctx.send(f"タスクが見つかりません: {task_id}")


@bot.command(name="summary")
async def summary(ctx: commands.Context) -> None:
    text = BRAIN.summarize_context(state_cache)
    await ctx.send(f"## 今日の再開ガイド\n{text}")


@tasks.loop(hours=24)
async def daily_scheduler() -> None:
    await bot.wait_until_ready()
    pending = state_cache["schedules"].get("pending_proposal")
    unfinished = [t for t in state_cache["tasks"] if t.get("status") != "done"]
    if pending or not unfinished:
        return

    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M")
    proposal = BRAIN.suggest_next_date(state_cache, now_iso)

    schedule_ch = bot.get_channel(CONFIG.schedule_channel_id)
    if not isinstance(schedule_ch, discord.TextChannel):
        LOGGER.warning("Schedule channel not found")
        return

    msg = await schedule_ch.send(
        "次回の活動候補: **{date}**\n理由: {reason}\n"
        "参加可能なら✅、難しければ❌を押してください。".format(
            date=proposal["suggested_date"],
            reason=proposal["reason"],
        )
    )
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

    state_cache["schedules"]["pending_proposal"] = {
        "message_id": str(msg.id),
        "suggested_date": proposal["suggested_date"],
        "reactions": {"✅": [], "❌": []},
    }
    await persist_state()


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    pending = state_cache["schedules"].get("pending_proposal")
    if not pending:
        return
    if str(payload.message_id) != pending["message_id"]:
        return

    emoji = str(payload.emoji)
    if emoji not in ["✅", "❌"]:
        return

    user_id = str(payload.user_id)
    for k in ["✅", "❌"]:
        if user_id in pending["reactions"][k]:
            pending["reactions"][k].remove(user_id)
    pending["reactions"][emoji].append(user_id)

    yes = len(pending["reactions"]["✅"])
    no = len(pending["reactions"]["❌"])
    await persist_state()

    if yes >= 2 and yes > no:
        state_cache["schedules"]["confirmed_events"].append(
            {
                "date": pending["suggested_date"],
                "confirmed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        state_cache["project_status"]["last_meeting"] = pending["suggested_date"].split(" ")[0]
        state_cache["project_status"]["overall_momentum"] = "active"
        state_cache["schedules"]["pending_proposal"] = None
        await persist_state()

        channel = bot.get_channel(CONFIG.schedule_channel_id)
        if isinstance(channel, discord.TextChannel):
            await channel.send(f"✅ 日程確定: {pending['suggested_date']}")


if __name__ == "__main__":
    bot.run(CONFIG.discord_token)
