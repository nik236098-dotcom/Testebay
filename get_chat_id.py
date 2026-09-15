#!/usr/bin/env python3
"""
Показывает chat_id для TG_CHAT_ID.

1. Создайте бота у @BotFather и запишите токен в TG_BOT_TOKEN (.env или окружение).
2. Напишите боту любое сообщение в Telegram (или добавьте его в группу и напишите там).
3. Запустите: python get_chat_id.py
"""

import os
import sys

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def main() -> int:
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    if not token:
        print("Задайте TG_BOT_TOKEN в .env или окружении", file=sys.stderr)
        return 1

    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    data = resp.json()
    if not data.get("ok"):
        print(f"Ошибка Telegram: {data}", file=sys.stderr)
        return 1

    chats = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat

    if not chats:
        print("Обновлений нет. Сначала напишите боту сообщение и запустите скрипт снова.")
        return 1

    print("Найденные чаты:")
    for chat_id, chat in chats.items():
        name = chat.get("title") or " ".join(
            filter(None, [chat.get("first_name"), chat.get("last_name")])
        ) or chat.get("username") or "?"
        print(f"  TG_CHAT_ID={chat_id}   ({chat.get('type')}: {name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
