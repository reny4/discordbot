# Discord AI Project Scheduler Bot

不定期チーム向けに、以下を自動化する Discord Bot です。

- 自律スケジュール提案
- リアクション学習（✅/❌）
- AIタスク管理
- 活動再開時のコンテキスト要約
- Discord チャンネル上の JSON 永続化

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` の値を設定後、実行します。

```bash
python bot.py
```

## コマンド

- `/add_task <内容>`: タスク追加
- `/update_task <task_id> <status> <progress>`: タスク更新
- `/summary`: 再開用サマリー生成

## データ構造

Bot は `DB_CHANNEL_ID` の先頭 JSON メッセージを単一データストアとして利用し、編集で更新します。

```json
{
  "project_status": {
    "last_meeting": null,
    "overall_momentum": "idle"
  },
  "tasks": [],
  "schedules": {
    "pending_proposal": null,
    "confirmed_events": []
  },
  "preferences": {
    "avoid_weekends": true,
    "best_hour": 21
  }
}
```
